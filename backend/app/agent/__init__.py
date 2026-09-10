"""Agentic Ask engine — turn pipeline (ORCHESTRATOR_STEERING.md §5):

AskRequest + CurrentUser
  -> build_frame()
  -> gate refused? -> NoAnswerBlock(reason) -> audit -> return
  -> router.select(frame.query_class)
  -> §4.1 jurisdiction scope resolution (IO scoped classes only)
  -> AgentLoop.run(specialist, frame, thread_context)
  -> composer -> AssistantMessage
  -> ThreadStore.append(thread_id, turn summary)
  -> audit.write(turn)
  -> AskResponse
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from app.config import Settings
from app.schemas import AskRequest, AskResponse, AssistantMessage, CurrentUser
from app.semantic.frame import build_frame
from app.semantic.gating import gate, is_scoped
from app.supabase import SupabaseError, select_rows

from . import audit as audit_writer
from . import router
from .answer_cache import CacheEntry, get_answer_cache, ttl_for_class
from .cache_key import frame_key
from .composer import compose, refusal_message
from .loop import AgentLoop
from .scratchpad import TurnScratchpad
from .specialists import SpecialistConfig, intel_analyst_scoped_tools
from .threads import TurnSummary, get_thread_store
from .usage_log import log_turn_usage

_STATION_CACHE_TTL_S = 600
_station_cache: dict[str, tuple[float, dict | None]] = {}


async def _resolve_operator_station(user: CurrentUser, settings: Settings) -> dict | None:
    """§4.1 base station derivation, process-cached <=10 min. Returns None if
    officer_id is null, the officer row is missing, is_active=false, or has no
    station_id — any of which means every scoped query_class must refuse."""
    if not user.officer_id:
        return None
    cached = _station_cache.get(user.officer_id)
    now = time.time()
    if cached and now - cached[0] < _STATION_CACHE_TTL_S:
        return cached[1]

    result: dict | None = None
    try:
        rows = await select_rows(
            "officers",
            {
                "officer_id": f"eq.{user.officer_id}",
                "select": "officer_id,station_id,is_active,police_stations(station_name)",
            },
            settings,
        )
    except SupabaseError:
        rows = []

    if rows and rows[0].get("is_active") and rows[0].get("station_id"):
        row = rows[0]
        ps = row.get("police_stations")
        station_name = None
        if isinstance(ps, dict):
            station_name = ps.get("station_name")
        elif isinstance(ps, list) and ps:
            station_name = ps[0].get("station_name")
        result = {"station_id": row["station_id"], "station_name": station_name or row["station_id"]}

    _station_cache[user.officer_id] = (now, result)
    return result


def _summarize(message: AssistantMessage) -> str:
    summary = message.text or ""
    if not summary and message.blocks:
        first = message.blocks[0]
        if first.type == "text":
            summary = first.content
        elif first.type == "no_answer":
            summary = first.message
        else:
            summary = f"{first.type} block"
    return summary


def _build_response(thread_id: str, message: AssistantMessage) -> AskResponse:
    return AskResponse(
        thread_id=thread_id,
        message_id=str(uuid.uuid4()),
        server_ts=datetime.now(timezone.utc).isoformat(),
        message=message,
    )


async def run_turn(
    request: AskRequest,
    user: CurrentUser,
    settings: Settings,
    *,
    request_id: str,
    on_event: Callable[[dict[str, Any]], None] | None = None,
) -> AskResponse:
    turn_start = time.monotonic()
    thread_id = request.thread_id or str(uuid.uuid4())
    scratchpad = TurnScratchpad(on_event=on_event)
    frame = await build_frame(request, user, settings, on_usage=scratchpad.add_usage)

    def _log_usage(specialist_name: str | None, message: AssistantMessage) -> None:
        status, reason = "answered", None
        if message.blocks and message.blocks[0].type == "no_answer":
            status, reason = "no_answer", message.blocks[0].reason
        log_turn_usage(
            request_id=request_id, thread_id=thread_id, role=user.role.value,
            query_class=frame.query_class, specialist=specialist_name,
            fast_model=settings.llm_profile_fast_model, smart_model=settings.llm_profile_smart_model,
            prompt_tokens=scratchpad.usage["prompt_tokens"],
            completion_tokens=scratchpad.usage["completion_tokens"],
            total_tokens=scratchpad.usage["total_tokens"],
            turnaround_ms=int((time.monotonic() - turn_start) * 1000),
            status=status, reason=reason,
        )

    decision = gate(user.role, frame.query_class)
    if not decision.allow:
        message = refusal_message(decision.reason or "low_confidence")
        await audit_writer.write(
            request=request, user=user, query_class=frame.query_class, message=message,
            scratchpad=scratchpad, specialist_name=None, request_id=request_id,
            thread_id=thread_id, settings=settings,
        )
        _log_usage(None, message)
        return _build_response(thread_id, message)

    specialist: SpecialistConfig = router.select(frame.query_class)

    jurisdiction_note: str | None = None
    scoped_station_id: str | None = None
    tools = specialist.tools
    if is_scoped(user.role, frame.query_class):
        station = await _resolve_operator_station(user, settings)
        if station is None:
            message = refusal_message("out_of_scope")
            await audit_writer.write(
                request=request, user=user, query_class=frame.query_class, message=message,
                scratchpad=scratchpad, specialist_name=specialist.name, request_id=request_id,
                thread_id=thread_id, settings=settings,
            )
            _log_usage(specialist.name, message)
            return _build_response(thread_id, message)
        scoped_station_id = station["station_id"]
        jurisdiction_note = (
            f"Your analyses for this turn are limited to cases filed at "
            f"{station['station_name']} (station {station['station_id']})."
        )
        if specialist.name == "intel_analyst":
            tools = intel_analyst_scoped_tools()

    # R2 R-3: pre-loop semantic answer cache. Keyed on role + station so a
    # scoped answer never crosses stations/roles. Only status=answered results
    # are stored (checked at store time via block type).
    cache_ttl = 0
    cache_key: str | None = None
    if settings.answer_cache_enabled:
        cache_ttl = ttl_for_class(
            frame.query_class,
            settings.answer_cache_ttl_lookup_s,
            settings.answer_cache_ttl_analytic_s,
        )
    if cache_ttl > 0:
        cache_key = frame_key(frame, user, scoped_station_id, settings)
        hit = get_answer_cache().get(cache_key)
        if hit is not None:
            scratchpad.emit_event({"type": "cache_hit", "label": "Answered from recent results"})
            scratchpad.records_accessed = set(hit.records_accessed)
            await get_thread_store(settings).append(
                thread_id,
                TurnSummary(
                    query=frame.raw_query,
                    specialist=hit.specialist_name or specialist.name,
                    answer_summary=_summarize(hit.message)[:200],
                    entity_ids=hit.records_accessed[:10],
                ),
            )
            await audit_writer.write(
                request=request, user=user, query_class=frame.query_class, message=hit.message,
                scratchpad=scratchpad, specialist_name=hit.specialist_name, request_id=request_id,
                thread_id=thread_id, settings=settings, cache_hit=True,
            )
            _log_usage(hit.specialist_name, hit.message)
            return _build_response(thread_id, hit.message)

    loop = AgentLoop(settings)
    result = await loop.run(
        specialist=specialist,
        tools=tools,
        frame=frame,
        user=user,
        scratchpad=scratchpad,
        thread_id=thread_id,
        sql_scope=specialist.sql_scope,
        jurisdiction_note=jurisdiction_note,
        scoped_station_id=scoped_station_id,
    )

    message = compose(result.final_answer, scratchpad, specialist)

    answered = not (message.blocks and message.blocks[0].type == "no_answer")
    if cache_key is not None and answered:
        get_answer_cache().put(
            cache_key,
            CacheEntry(
                message=message,
                records_accessed=sorted(scratchpad.records_accessed),
                query_class=frame.query_class,
                specialist_name=specialist.name,
                expires_at=time.time() + cache_ttl,
            ),
        )

    answer_summary = _summarize(message)
    thread_store = get_thread_store(settings)
    await thread_store.append(
        thread_id,
        TurnSummary(
            query=frame.raw_query,
            specialist=specialist.name,
            answer_summary=answer_summary[:200],
            entity_ids=sorted(scratchpad.records_accessed)[:10],
        ),
    )

    await audit_writer.write(
        request=request, user=user, query_class=frame.query_class, message=message,
        scratchpad=scratchpad, specialist_name=specialist.name, request_id=request_id,
        thread_id=thread_id, settings=settings,
    )
    _log_usage(specialist.name, message)

    return _build_response(thread_id, message)
