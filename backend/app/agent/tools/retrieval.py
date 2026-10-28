"""search_narratives (ORCHESTRATOR_STEERING.md §9.3) — semantic search over the
free-text narratives in document_chunks (FIR narratives, case diary entries,
victim statements, chargesheet summaries, ...).

Embeds the query via the embeddings service, then calls the
match_document_chunks RPC (db/migrations/009_agent_executor.sql). Chunks are
mapped back to their FIR so every hit carries a sentence-level citation.
"""

from __future__ import annotations

from typing import Any

from app.embeddings import EmbeddingsError, embed_query
from app.roles import Role
from app.supabase import SupabaseError, call_rpc

from .base import ToolContext, ToolResult, is_safe_code, is_valid_fir_id, is_valid_uuid, sql_escape

_MAX_TOP_K = 8
_MAX_QUERY_CHARS = 1000

# source_table -> primary-key column, for tables whose rows carry a fir_id.
# Chunks from tables outside this map (mo_codes, gangs, events) have no FIR to
# cite and are dropped. "firs" chunks use the FIR id as their source_id.
_FIR_LINKED_PK = {
    "case_diary_entries": "entry_id",
    "fir_victims": "fir_victim_id",
    "chargesheets": "chargesheet_id",
    "ncr_petitions": "petition_id",
    "seizures": "seizure_id",
    "history_sheet_entries": "hs_entry_id",
    "stolen_property": "property_id",
    "missing_persons": "mp_id",
}

# Victim PII is stripped for everyone but investigating officers (References/
# KSP_Trust_Design.pdf, "Layer 2 — Result Filtering"): victim statements and
# the complainant's own account never reach a supervisor/analyst turn.
_NON_IO_RESTRICTED = {("fir_victims", "victim_statement"), ("firs", "complaint_text")}


class SearchNarrativesTool:
    name = "search_narratives"
    label = "Searching narratives"
    # Covers an embeddings-service cold start (it scales to zero); the loop
    # uses this instead of the global ASK_TOOL_TIMEOUT_S.
    timeout_s = 30
    description = (
        "Semantic search over case narrative text (FIR narratives, case diary entries, "
        "chargesheet summaries, MO descriptions). Use for questions about what happened, "
        "how a crime was committed, or what a diary entry says — NOT for counts, lists or "
        "exact lookups (use run_sql). Returns the best-matching passages with similarity "
        "scores and citation keys. The optional fir_id filter limits results to that FIR's "
        "own narrative fields."
    )

    @property
    def params_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query_text": {"type": "string", "description": "What to look for, in natural language."},
                "top_k": {"type": "integer", "minimum": 1, "maximum": _MAX_TOP_K, "default": 5},
                "filters": {
                    "type": "object",
                    "properties": {
                        "fir_id": {"type": "string"},
                        "source_field": {
                            "type": "string",
                            "description": "e.g. fir_narrative, mo_description_free, entry_text, summary_text",
                        },
                        "district_id": {"type": "string"},
                    },
                },
            },
            "required": ["query_text"],
        }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        query = (args.get("query_text") or "").strip()[:_MAX_QUERY_CHARS]
        if not query:
            return ToolResult(ok=False, error="no query_text provided")
        try:
            top_k = max(1, min(int(args.get("top_k") or 5), _MAX_TOP_K))
        except (TypeError, ValueError):
            top_k = 5

        raw_filters = args.get("filters") or {}
        filters: dict[str, str] = {}
        for key in ("fir_id", "source_field", "district_id"):
            value = raw_filters.get(key)
            if not value:
                continue
            value = str(value).strip()
            if not (is_valid_fir_id(value) if key == "fir_id" else is_safe_code(value)):
                return ToolResult(ok=False, error=f"invalid {key} filter")
            filters[key] = value

        try:
            vector = await embed_query(query, ctx.settings)
            # Over-fetch so dropping restricted/unciteable chunks still leaves top_k.
            rows = await call_rpc(
                "match_document_chunks",
                {"p_embedding": vector, "p_filters": filters, "p_k": min(top_k * 3, 24)},
                ctx.settings,
            )
            rows = rows or []
            if ctx.user.role != Role.INVESTIGATING_OFFICER:
                rows = [r for r in rows if (r["source_table"], r["source_field"]) not in _NON_IO_RESTRICTED]
            fir_ids = await _resolve_fir_ids(rows, ctx)
        except EmbeddingsError as exc:
            return ToolResult(ok=False, error=f"narrative search unavailable: {exc}")
        except SupabaseError as exc:
            return ToolResult(ok=False, error=f"search_narratives failed: {exc}")

        chunks: list[dict[str, Any]] = []
        for row in rows:
            fir_id = fir_ids.get((row["source_table"], row["source_id"]))
            if not fir_id:
                continue
            snippet = " ".join((row.get("snippet") or "").split())
            key = None
            if ctx.scratchpad is not None:
                key = ctx.scratchpad.register_provenance(
                    "sentence",
                    {
                        "fir_id": fir_id,
                        "field": row["source_field"],
                        "sentence_id": row["chunk_id"],
                        "excerpt": snippet[:160],
                    },
                )
            chunks.append({
                "citation_key": key,
                "fir_id": fir_id,
                "source_field": row["source_field"],
                "similarity": row["similarity"],
                "snippet": snippet,
            })
            if len(chunks) == top_k:
                break
        return ToolResult(ok=True, data={"chunks": chunks, "count": len(chunks)})


async def _resolve_fir_ids(rows: list[dict[str, Any]], ctx: ToolContext) -> dict[tuple[str, str], str]:
    """Map (source_table, source_id) -> fir_id for the returned chunks."""
    out: dict[tuple[str, str], str] = {}
    ids_by_table: dict[str, set[str]] = {}
    for row in rows:
        table, source_id = row["source_table"], row["source_id"]
        if table == "firs":
            out[(table, source_id)] = source_id
        elif table in _FIR_LINKED_PK and is_valid_uuid(source_id):
            ids_by_table.setdefault(table, set()).add(source_id)
    if not ids_by_table:
        return out

    parts = []
    for table, ids in ids_by_table.items():
        pk = _FIR_LINKED_PK[table]
        in_list = ", ".join(f"'{sql_escape(i)}'" for i in sorted(ids))
        parts.append(
            f"SELECT '{table}' AS t, {pk}::text AS id, fir_id FROM {table} "
            f"WHERE {pk}::text IN ({in_list}) AND fir_id IS NOT NULL"
        )
    result = await call_rpc(
        "execute_agent_select",
        {"p_sql": " UNION ALL ".join(parts), "p_scope": "case", "p_row_cap": 100},
        ctx.settings,
    )
    for r in (result or {}).get("rows") or []:
        out[(r["t"], r["id"])] = r["fir_id"]
    return out
