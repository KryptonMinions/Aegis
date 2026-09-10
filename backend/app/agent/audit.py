"""Per-turn audit persistence (ORCHESTRATOR_STEERING.md §12, O-12).

One query_audit_log row + one ask_turn_traces row per turn (answered or
refused). Failures to write audit must never fail the user's response — caught
and swallowed here, never raised to the caller.

Flag rules v1 (deterministic, evaluated at write time, §12):
- Bulk_Lookup: >=5 distinct person entities resolved for this officer's
  session within 60 min.
- Off_Hours_No_Case_Link: turn between 22:00-06:00 IST and records_accessed
  empty of the officer's own assigned FIRs (skipped when officer_id is null).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from app.config import Settings
from app.schemas import AskRequest, AssistantMessage, CurrentUser
from app.supabase import SupabaseError, insert_row, select_rows

from .scratchpad import TurnScratchpad

_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_FIR_ID_RE = re.compile(r"^KA-[A-Za-z0-9]+-[A-Za-z0-9]+-\d{4}-\d+$")

_IST_OFFSET = timedelta(hours=5, minutes=30)
_BULK_LOOKUP_THRESHOLD = 5
_BULK_LOOKUP_WINDOW_MIN = 60


async def _bulk_lookup_flagged(
    officer_id: str | None, current_person_ids: set[str], settings: Settings
) -> bool:
    if not officer_id:
        return False
    distinct_persons = set(current_person_ids)
    if len(distinct_persons) < _BULK_LOOKUP_THRESHOLD:
        since = (datetime.now(timezone.utc) - timedelta(minutes=_BULK_LOOKUP_WINDOW_MIN)).isoformat()
        try:
            rows = await select_rows(
                "query_audit_log",
                {"officer_id": f"eq.{officer_id}", "query_timestamp": f"gte.{since}", "select": "records_accessed"},
                settings,
            )
        except SupabaseError:
            return False
        for row in rows:
            for record_id in row.get("records_accessed") or []:
                if _UUID_RE.match(record_id):
                    distinct_persons.add(record_id)
    return len(distinct_persons) >= _BULK_LOOKUP_THRESHOLD


async def _off_hours_no_case_link_flagged(
    officer_id: str | None, current_fir_ids: set[str], settings: Settings
) -> bool:
    if not officer_id:
        return False
    now_ist = datetime.now(timezone.utc) + _IST_OFFSET
    if not (now_ist.hour >= 22 or now_ist.hour < 6):
        return False
    if not current_fir_ids:
        return True  # off-hours, touched no records at all -> no case link
    try:
        rows = await select_rows(
            "firs",
            {"fir_id": f"in.({','.join(sorted(current_fir_ids))})", "io_officer_id": f"eq.{officer_id}", "select": "fir_id"},
            settings,
        )
    except SupabaseError:
        return False
    return len(rows) == 0


async def _evaluate_flags(
    officer_id: str | None, records_accessed: set[str], settings: Settings
) -> tuple[bool, str | None]:
    person_ids = {r for r in records_accessed if _UUID_RE.match(r)}
    fir_ids = {r for r in records_accessed if _FIR_ID_RE.match(r)}
    if await _bulk_lookup_flagged(officer_id, person_ids, settings):
        return True, "Bulk_Lookup"
    if await _off_hours_no_case_link_flagged(officer_id, fir_ids, settings):
        return True, "Off_Hours_No_Case_Link"
    return False, None


async def write(
    *,
    request: AskRequest,
    user: CurrentUser,
    query_class: str,
    message: AssistantMessage,
    scratchpad: TurnScratchpad,
    specialist_name: str | None,
    request_id: str,
    thread_id: str,
    settings: Settings,
    cache_hit: bool = False,
) -> None:
    response_reason = "answered"
    response_summary = message.text or ""
    if message.blocks and message.blocks[0].type == "no_answer":
        na = message.blocks[0]
        response_reason = na.reason or "low_confidence"
        if not response_summary:
            response_summary = na.message
    elif not response_summary and message.blocks:
        first = message.blocks[0]
        if first.type == "text":
            response_summary = first.content

    components_used = ([specialist_name] if specialist_name else []) + sorted(set(scratchpad.tools_used))

    try:
        is_flagged, flag_reason = await _evaluate_flags(user.officer_id, scratchpad.records_accessed, settings)
    except Exception:  # noqa: BLE001 — flag evaluation must never block the audit write itself
        is_flagged, flag_reason = False, None

    log_row = {
        "officer_id": user.officer_id,
        "session_id": user.id,
        "query_text": request.query,
        "query_language": request.detected_language,
        "query_type": query_class if query_class != "unresolved" else None,
        "records_accessed": sorted(scratchpad.records_accessed)[:100],
        "response_summary": response_summary[:500],
        "components_used": components_used,
        "is_flagged": is_flagged,
        "flag_reason": flag_reason,
        "input_modality": request.input_modality,
        "response_reason": response_reason,
        "thread_id": thread_id,
        "request_id": request_id,
    }

    log_id: str | None = None
    try:
        inserted = await insert_row("query_audit_log", log_row, settings)
        log_id = inserted[0]["log_id"] if inserted else None
    except SupabaseError:
        pass

    trace = {
        "events": scratchpad.events,
        "usage": scratchpad.usage,
        "components_used": components_used,
    }
    if cache_hit:
        # R2 §4.4: a cache hit is still an access. records_accessed is
        # reconstructed on the log row above; the trace marks zero tool calls.
        trace["served_from_cache"] = True
    trace_row = {"log_id": log_id, "request_id": request_id, "trace": trace}
    try:
        await insert_row("ask_turn_traces", trace_row, settings)
    except SupabaseError:
        pass
