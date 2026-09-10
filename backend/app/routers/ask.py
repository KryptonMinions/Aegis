import asyncio
import json
import sys
import time
import uuid
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError

from app import agent
from app.config import Settings
from app.schemas import AskRequest, AskResponse, AssistantMessage, CurrentUser, NoAnswerBlock
from app.security import SettingsDep, verify_jwt

router = APIRouter(tags=["ask"])

_FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"

# The demo fixtures are generated content (gitignored, per .gitignore), so a
# fresh checkout may not have them. Loading is lazy + graceful: the app always
# boots, and a missing/invalid fixture degrades to this no_answer rather than
# crashing the import. When ASK_ENGINE=agent (P2+), fixtures aren't consulted.
_FALLBACK_NO_ANSWER = AssistantMessage(
    blocks=[
        NoAnswerBlock(
            id="no-answer-fallback",
            message=(
                "Not enough verified information to answer this. Try rephrasing, "
                "or consult the case file directly."
            ),
            reason="not_found",
        )
    ]
)


def _normalize(query: str) -> str:
    """Same normalization as the frontend mock (trim/lowercase/collapse
    whitespace) so dispatch matching behaves identically either way."""
    return " ".join(query.strip().lower().split())


def _try_load(filename: str) -> AssistantMessage | None:
    try:
        data = json.loads((_FIXTURES_DIR / filename).read_text())
        return AssistantMessage.model_validate(data)
    except (FileNotFoundError, json.JSONDecodeError, ValidationError):
        return None


def _try_load_story1() -> dict[str, AssistantMessage]:
    try:
        data = json.loads((_FIXTURES_DIR / "story1_antecedents.json").read_text())
        return {k: AssistantMessage.model_validate(v) for k, v in data.items()}
    except (FileNotFoundError, json.JSONDecodeError, ValidationError):
        return {}


@lru_cache(maxsize=1)
def _no_answer_message() -> AssistantMessage:
    return _try_load("no_answer.json") or _FALLBACK_NO_ANSWER


# Dispatch keyed by the exact verbatim demo-script query strings (References/
# KSP_Demo_Script.pdf), matching frontend/src/lib/fixtures/story*.ts's
# STORY*_QUERY constants exactly so mock and real paths dispatch identically.
# Built lazily and cached; entries whose fixture is missing are simply omitted
# (that query then falls through to the no_answer message).
@lru_cache(maxsize=1)
def _dispatch() -> dict[str, tuple[str, AssistantMessage]]:
    table: dict[str, tuple[str, AssistantMessage]] = {}
    story1 = _try_load_story1()
    story1_keys = {
        "rajan gowda antecedents — any cases in karnataka?": "q1",
        "show me only the violent offences from those results": "q2",
        "is he history-sheeted anywhere?": "q3",
    }
    for query, key in story1_keys.items():
        if key in story1:
            table[_normalize(query)] = (f"story1_antecedents.{key}", story1[key])

    single: list[tuple[str, str, str]] = [
        (
            "show me everyone connected to him — co-accused, associates, shared addresses",
            "story4_network",
            "story4_network.json",
        ),
        (
            "find similar past cases: accused on two-wheeler approaches female victim at "
            "traffic signal, snatches gold chain, flees towards main road. how were the "
            "solved ones cracked?",
            "story2_mo_match",
            "story2_mo_match.json",
        ),
        (
            "what changed in mysuru district this month compared to last month — and "
            "what is driving the change?",
            "story10_review_pack",
            "story10_review_pack.json",
        ),
    ]
    for query, name, filename in single:
        message = _try_load(filename)
        if message is not None:
            table[_normalize(query)] = (name, message)
    return table


def _error(status_code: int, code: str, message: str, request_id: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message}},
        headers={"X-Request-Id": request_id},
    )


def _audit_log(**fields: object) -> None:
    # Stdout JSON, one line per record (ASK_ENDPOINT_CONTRACT.md §2.7).
    print(json.dumps(fields, default=str), file=sys.stdout, flush=True)


async def _authenticate_and_validate(
    request: Request, settings: Settings, request_id: str
) -> tuple[CurrentUser, AskRequest] | JSONResponse:
    """Shared by `/ask` and `/ask/stream` so the two endpoints' auth/validation
    behavior (and error shapes, ASK_ENDPOINT_CONTRACT.md §2.5) can't drift.
    Runs to completion before any streaming response is opened, so 401/422
    are always plain JSON status codes -- never a mid-stream SSE error."""
    auth_header = request.headers.get("authorization", "")
    token = auth_header[7:].strip() if auth_header.lower().startswith("bearer ") else ""
    if not token:
        return _error(401, "unauthenticated", "Missing or invalid JWT", request_id)
    try:
        current_user = verify_jwt(token, settings)
    except HTTPException:
        return _error(401, "unauthenticated", "Missing or invalid JWT", request_id)

    try:
        raw_body = await request.json()
        body = AskRequest.model_validate(raw_body)
    except (json.JSONDecodeError, ValidationError, ValueError):
        return _error(422, "invalid_request", "Invalid request body", request_id)

    return current_user, body


def _build_fixture_response(
    body: AskRequest, current_user: CurrentUser, request_id: str, start: float
) -> AskResponse:
    """Fixture-engine dispatch + audit log, shared by `/ask` and
    `/ask/stream` so the two can't diverge on demo-mode behavior."""
    thread_id = body.thread_id or str(uuid.uuid4())
    normalized_query = _normalize(body.query)
    matched = _dispatch().get(normalized_query)
    matched_fixture, message = matched if matched else (None, _no_answer_message())

    server_ts = datetime.now(timezone.utc).isoformat()
    response = AskResponse(
        thread_id=thread_id,
        message_id=str(uuid.uuid4()),
        server_ts=server_ts,
        message=message,
    )

    latency_ms = int((time.monotonic() - start) * 1000)
    _audit_log(
        request_id=request_id,
        user_id=current_user.id,
        role=current_user.role.value,
        thread_id=thread_id,
        turn_index=body.turn_index,
        input_modality=body.input_modality,
        detected_language=body.detected_language,
        query_length=len(body.query),
        matched_fixture=matched_fixture,
        response_block_types=[block.type for block in message.blocks],
        latency_ms=latency_ms,
        server_ts=server_ts,
    )
    return response


@router.post("/ask")
async def ask(request: Request, settings: SettingsDep) -> Response:
    request_id = str(uuid.uuid4())
    start = time.monotonic()

    result = await _authenticate_and_validate(request, settings, request_id)
    if isinstance(result, JSONResponse):
        return result
    current_user, body = result

    if settings.ask_engine == "agent":
        response = await agent.run_turn(body, current_user, settings, request_id=request_id)
        return JSONResponse(
            status_code=200,
            content=jsonable_encoder(response),
            headers={"X-Request-Id": request_id},
        )

    response = _build_fixture_response(body, current_user, request_id, start)
    return JSONResponse(
        status_code=200,
        content=jsonable_encoder(response),
        headers={"X-Request-Id": request_id},
    )


# ---------------------------------------------------------------------------
# POST /ask/stream -- SSE variant (ASK_STREAM_ENDPOINT_CONTRACT.md). Purely
# additive: /ask above is untouched. Streams the agent loop's own activity
# trace (scratchpad events -- model working notes + tool-call lifecycle) as
# `thinking` frames, then a single terminal `message` (the same AskResponse
# shape /ask returns) or `error` frame. No raw LLM token streaming and no
# provider chain-of-thought -- see the contract doc for why.
# ---------------------------------------------------------------------------

_KEEPALIVE_S = 15.0
_FIXTURE_THINKING_DELAY_S = 0.6
_FIXTURE_THINKING_LABELS = ["Reviewing query intent", "Looking up matching records"]

_THINKING_LABELS: dict[str, str] = {
    "thought": "Thinking",
    "cache_hit": "Answered from recent results",
    "hard_refuse": "Refused",
    "llm_error": "Model error",
    "abort": "Stopped",
    "final_answer_parse_failed": "Formatting the answer",
}
_THINKING_ERROR_KINDS = {"hard_refuse", "llm_error", "abort", "final_answer_parse_failed"}


def _sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n".encode()


def _map_thinking_event(raw: dict[str, Any]) -> dict[str, Any]:
    """Raw scratchpad event (loop.py's `emit_event` payloads) -> the uniform
    wire shape `{id, kind, label, detail?, status, ts, tool?}` documented in
    ASK_STREAM_ENDPOINT_CONTRACT.md."""
    kind = raw.get("type", "thought")
    label = raw.get("label") or _THINKING_LABELS.get(kind, kind)
    if kind == "tool_finished":
        status = "done" if raw.get("ok") else "error"
    elif kind in _THINKING_ERROR_KINDS:
        status = "error"
    else:
        status = "in_progress"

    mapped: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "kind": kind,
        "label": label,
        "status": status,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    detail = raw.get("text") or raw.get("reason") or raw.get("error") or raw.get("repair_error")
    if detail:
        mapped["detail"] = detail
    if kind in ("tool_started", "tool_finished") and raw.get("tool"):
        mapped["tool"] = raw["tool"]
    return mapped


async def _stream_agent_turn(
    body: AskRequest,
    current_user: CurrentUser,
    settings: Settings,
    request_id: str,
    queue: "asyncio.Queue[tuple[str, dict]]",
) -> None:
    def on_event(event: dict[str, Any]) -> None:
        queue.put_nowait(("thinking", _map_thinking_event(event)))

    try:
        response = await agent.run_turn(
            body, current_user, settings, request_id=request_id, on_event=on_event,
        )
    except Exception:  # noqa: BLE001 -- any turn failure must still close the stream cleanly
        queue.put_nowait((
            "error",
            {"error": {"code": "internal_error", "message": "Something went wrong processing this request."}},
        ))
        return
    queue.put_nowait(("message", jsonable_encoder(response)))


async def _stream_fixture_turn(
    body: AskRequest,
    current_user: CurrentUser,
    request_id: str,
    start: float,
    queue: "asyncio.Queue[tuple[str, dict]]",
) -> None:
    try:
        matched = _dispatch().get(_normalize(body.query))
        fixture_name = matched[0] if matched else None
        for i, label in enumerate(_FIXTURE_THINKING_LABELS):
            event: dict[str, Any] = {
                "id": str(uuid.uuid4()),
                "kind": "thought",
                "label": label,
                "status": "in_progress",
                "ts": datetime.now(timezone.utc).isoformat(),
            }
            if fixture_name and i == len(_FIXTURE_THINKING_LABELS) - 1:
                event["detail"] = f"Matched {fixture_name}"
            queue.put_nowait(("thinking", event))
            await asyncio.sleep(_FIXTURE_THINKING_DELAY_S)
        response = _build_fixture_response(body, current_user, request_id, start)
    except Exception:  # noqa: BLE001 -- same contract as the agent path above
        queue.put_nowait((
            "error",
            {"error": {"code": "internal_error", "message": "Something went wrong processing this request."}},
        ))
        return
    queue.put_nowait(("message", jsonable_encoder(response)))


@router.post("/ask/stream")
async def ask_stream(request: Request, settings: SettingsDep) -> Response:
    request_id = str(uuid.uuid4())
    start = time.monotonic()

    result = await _authenticate_and_validate(request, settings, request_id)
    if isinstance(result, JSONResponse):
        return result
    current_user, body = result

    queue: "asyncio.Queue[tuple[str, dict]]" = asyncio.Queue()
    if settings.ask_engine == "agent":
        task = asyncio.create_task(
            _stream_agent_turn(body, current_user, settings, request_id, queue)
        )
    else:
        task = asyncio.create_task(
            _stream_fixture_turn(body, current_user, request_id, start, queue)
        )

    async def event_stream():
        try:
            while True:
                if await request.is_disconnected():
                    return
                try:
                    event_name, payload = await asyncio.wait_for(queue.get(), timeout=_KEEPALIVE_S)
                except asyncio.TimeoutError:
                    yield b": keepalive\n\n"
                    continue
                yield _sse(event_name, payload)
                if event_name in ("message", "error"):
                    return
        finally:
            # Client disconnected or the generator otherwise stopped early --
            # don't leave an agent loop running server-side for nobody.
            if not task.done():
                task.cancel()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "X-Request-Id": request_id,
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
