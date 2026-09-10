"""LLMClient — the single seam for every LLM call in the backend.

OpenAI-compatible `/v1/chat/completions` wire (SEMANTIC_LAYER.md Part 1). Named
profiles are resolved from Settings: `fast` (gemini-2.5-flash, semantic-layer
default), `smart` (gemini-2.5-pro, orchestrator default), and optional `intent`
(Zoho Catalyst QuickML). Kept to plain httpx — no vendor SDK — matching this
backend's convention (app/supabase.py).

Model IDs are never hardcoded; they come from env via Settings (O-9).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

from app.catalyst import catalyst_headers
from app.config import Settings

from .errors import (
    LLMConfigError,
    LLMOutputError,
    LLMProviderError,
    LLMTimeoutError,
)


@dataclass(frozen=True)
class Profile:
    name: str
    base_url: str
    model: str
    api_key: str


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    # Provider-specific per-call metadata that MUST be echoed back verbatim in
    # the reconstructed assistant message on the next turn, or the call fails.
    # Gemini 3.x: {"google": {"thought_signature": "..."}} — required for
    # multi-turn tool-calling continuity; a missing signature 400s the next
    # request. None for providers that don't use this (e.g. Groq/OpenAI).
    extra_content: dict[str, Any] | None = None


@dataclass
class LLMResponse:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    finish_reason: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


def _resolve_profile(name: str, settings: Settings) -> Profile:
    base_url = getattr(settings, f"llm_profile_{name}_base_url", "")
    model = getattr(settings, f"llm_profile_{name}_model", "")
    api_key = getattr(settings, f"llm_profile_{name}_api_key", "")
    if not (base_url and model):
        raise LLMConfigError(
            f"LLM profile '{name}' is not configured "
            f"(need LLM_PROFILE_{name.upper()}_BASE_URL and _MODEL)."
        )
    return Profile(name=name, base_url=base_url.rstrip("/"), model=model, api_key=api_key)


class LLMClient:
    """Thin OpenAI-compatible chat client with retry/backoff.

    No prompt or response bodies are logged by default (SEMANTIC_LAYER.md Part 1
    — case narratives and PII must not leak into logs).
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._max_retries = settings.llm_max_retries
        self._timeout = settings.llm_request_timeout_s

    async def _post(self, profile: Profile, payload: dict[str, Any]) -> dict[str, Any]:
        if profile.name == "intent":
            # Zoho Catalyst QuickML (O-9): the configured base_url is already the
            # complete model endpoint (e.g. .../quickml/v1/project/{id}/glm/chat) —
            # no /chat/completions suffix — and auth is the same Catalyst OAuth
            # (Zoho-oauthtoken, refreshed via the project's self-client) used by
            # Cache/SmartBrowz, not a static Bearer API key. Verified live against
            # a real GLM-4.7-Flash deployment before being wired in here — request/
            # response bodies otherwise match OpenAI's chat-completions shape.
            url = profile.base_url
            headers = await catalyst_headers(self._settings)
            headers["CATALYST-ORG"] = self._settings.catalyst_org_id
        else:
            url = f"{profile.base_url}/chat/completions"
            headers = {"Content-Type": "application/json"}
            if profile.api_key:
                headers["Authorization"] = f"Bearer {profile.api_key}"

        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.post(url, json=payload, headers=headers)
            except httpx.TimeoutException as exc:
                last_exc = LLMTimeoutError(f"{profile.name} timed out: {exc}")
            except httpx.HTTPError as exc:
                last_exc = LLMProviderError(f"{profile.name} transport error: {exc}")
            else:
                if resp.status_code == 200:
                    body = resp.json()
                    if profile.name == "intent" and "choices" not in body and "response" in body:
                        # QuickML's actual live response shape is flat
                        # ({"response": text, "usage": {...}}), not the
                        # choices[]-wrapped shape its console's sample
                        # response showed — normalize so _parse() (shared
                        # with the OpenAI-compatible profiles) still works.
                        body = {
                            "choices": [{"message": {"content": body.get("response"), "tool_calls": []},
                                         "finish_reason": "stop"}],
                            "usage": body.get("usage", {}),
                        }
                    return body
                # Retry only on 429/5xx; 4xx (bad request) fails fast.
                if resp.status_code not in (429, 500, 502, 503, 504):
                    raise LLMProviderError(
                        f"{profile.name} returned {resp.status_code}: {resp.text[:300]}"
                    )
                last_exc = LLMProviderError(
                    f"{profile.name} returned {resp.status_code}"
                )
            if attempt < self._max_retries:
                time.sleep(min(2**attempt, 8) * 0.5)
        assert last_exc is not None
        raise last_exc

    @staticmethod
    def _parse(raw: dict[str, Any]) -> LLMResponse:
        choice = (raw.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        tool_calls: list[ToolCall] = []
        for tc in message.get("tool_calls") or []:
            fn = tc.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            tool_calls.append(
                ToolCall(
                    id=tc.get("id", ""), name=fn.get("name", ""), arguments=args,
                    extra_content=tc.get("extra_content"),
                )
            )
        usage = raw.get("usage") or {}
        # Read from prompt cache (OpenAI/Gemini OpenAI-compat shape). Absent on
        # providers/turns without caching -> 0. No "cache write" field exists on
        # this transport (that is Anthropic-specific), so cache_write_tokens
        # stays 0 until an Anthropic profile is added.
        cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
        return LLMResponse(
            content=message.get("content"),
            tool_calls=tool_calls,
            usage={
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
                "cache_read_tokens": cached or 0,
                "cache_write_tokens": 0,
            },
            finish_reason=choice.get("finish_reason"),
            raw=raw,
        )

    async def complete(
        self,
        profile: str,
        messages: list[dict[str, Any]],
        *,
        temperature: float = 0.2,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        p = _resolve_profile(profile, self._settings)
        payload: dict[str, Any] = {
            "model": p.model,
            "messages": messages,
            "temperature": temperature,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if p.name == "intent":
            # QuickML/GLM defaults to emitting a visible chain-of-thought
            # reasoning block before the final content (verified live) — we
            # only want the final answer for the classify call, so disable it.
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        return self._parse(await self._post(p, payload))

    async def complete_with_tools(
        self,
        profile: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        temperature: float = 0.2,
        tool_choice: str = "auto",
    ) -> LLMResponse:
        """One tool-calling round trip. The agent loop drives iteration; this
        method performs a single request/response exchange."""
        p = _resolve_profile(profile, self._settings)
        payload: dict[str, Any] = {
            "model": p.model,
            "messages": messages,
            "temperature": temperature,
            "tools": tools,
            "tool_choice": tool_choice,
        }
        return self._parse(await self._post(p, payload))

    async def complete_json(
        self,
        profile: str,
        messages: list[dict[str, Any]],
        *,
        temperature: float = 0.0,
        on_usage: Callable[[dict[str, int]], None] | None = None,
    ) -> dict[str, Any]:
        """Return a parsed JSON object, with one repair retry on parse failure.

        Falls back to the `fast` profile when the requested profile raises a
        config/transport error (used for the optional QuickML `intent` profile).

        `on_usage`, if given, is called with each underlying LLMResponse's
        usage dict (once per actual API call — initial + repair, if it
        happens) so callers can accumulate token counts even though this
        method's return value is just the parsed dict.
        """
        try:
            resp = await self.complete(
                profile, [*messages, {"role": "system", "content": _JSON_ONLY}],
                temperature=temperature,
            )
        except (LLMConfigError, LLMProviderError, LLMTimeoutError):
            if profile != "fast":
                resp = await self.complete(
                    "fast", [*messages, {"role": "system", "content": _JSON_ONLY}],
                    temperature=temperature,
                )
            else:
                raise
        if on_usage:
            on_usage(resp.usage)

        parsed = _try_json(resp.content)
        if parsed is not None:
            return parsed

        # One repair retry (mirrors complete_json behavior in the spec).
        repair = [
            *messages,
            {"role": "assistant", "content": resp.content or ""},
            {"role": "system", "content": _JSON_REPAIR},
        ]
        resp2 = await self.complete(profile if profile == "fast" else "fast", repair,
                                    temperature=temperature)
        if on_usage:
            on_usage(resp2.usage)
        parsed2 = _try_json(resp2.content)
        if parsed2 is None:
            raise LLMOutputError("model did not return valid JSON after repair retry")
        return parsed2


_JSON_ONLY = "Respond with a single valid JSON object and nothing else."
_JSON_REPAIR = (
    "Your previous message was not valid JSON. Reply again with ONLY a single "
    "valid JSON object, no prose, no code fences."
)


def _try_json(content: str | None) -> dict[str, Any] | None:
    if not content:
        return None
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lstrip().lower().startswith("json"):
            text = text.lstrip()[4:]
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None
