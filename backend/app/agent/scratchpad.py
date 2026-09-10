"""TurnScratchpad (ORCHESTRATOR_STEERING.md §8).

One instance per turn, request-scoped, never shared (§6.3). Holds registered
payloads (final wire-shape blocks the model references by payload_id),
provenance (citation-key registry), auto-accumulated records_accessed (feeds
audit), LLM usage totals, and the ordered event list (feeds ask_turn_traces).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from pydantic import TypeAdapter

from app.schemas import Citation, ContentBlock

ProvenanceKind = Literal["fir", "field", "sentence"]

_FIR_ID_RE = re.compile(r"^KA-[A-Z0-9]+-[A-Z0-9]+-\d{4}-\d+$")
_content_block_adapter: TypeAdapter[Any] = TypeAdapter(ContentBlock)


@dataclass
class ProvenanceEntry:
    kind: ProvenanceKind
    ref: dict[str, Any]

    def to_citation(self) -> Citation:
        if self.kind == "fir":
            return Citation(level="fir", fir_id=self.ref["fir_id"])
        if self.kind == "field":
            return Citation(level="field", fir_id=self.ref["fir_id"], field=self.ref.get("field"))
        return Citation(
            level="sentence",
            fir_id=self.ref["fir_id"],
            field=self.ref.get("field"),
            sentence_id=self.ref.get("sentence_id"),
            excerpt=self.ref.get("excerpt"),
        )


@dataclass
class TurnScratchpad:
    payloads: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, ProvenanceEntry] = field(default_factory=dict)
    records_accessed: set[str] = field(default_factory=set)
    usage: dict[str, int] = field(default_factory=lambda: {
        "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
        "cache_read_tokens": 0, "cache_write_tokens": 0,
    })
    events: list[dict[str, Any]] = field(default_factory=list)
    tools_used: list[str] = field(default_factory=list)
    # Optional live sink for `/ask/stream` (ASK_STREAM_ENDPOINT_CONTRACT.md) —
    # called synchronously from emit_event, in addition to the normal
    # append-to-`events` bookkeeping used by audit. None for the non-streaming
    # `/ask` path, so behavior there is unchanged.
    on_event: Callable[[dict[str, Any]], None] | None = None
    _payload_seq: int = 0
    _provenance_seq: int = 0

    def register_payload(self, block: Any) -> str:
        """Validate `block` against the ContentBlock union (wire shape final at
        registration) and store it under a new p1, p2, ... id."""
        validated = _content_block_adapter.validate_python(
            block if isinstance(block, dict) else block.model_dump()
        )
        self._payload_seq += 1
        payload_id = f"p{self._payload_seq}"
        self.payloads[payload_id] = validated
        self._accumulate_records_from_block(validated)
        return payload_id

    def register_provenance(self, kind: ProvenanceKind, ref: dict[str, Any]) -> str:
        self._provenance_seq += 1
        key = f"c{self._provenance_seq}"
        self.provenance[key] = ProvenanceEntry(kind=kind, ref=ref)
        fir_id = ref.get("fir_id")
        if fir_id:
            self.records_accessed.add(fir_id)
        return key

    def record_accessed(self, record_id: str) -> None:
        """Directly note a record as accessed this turn, for tools whose
        results aren't registered as a payload/citation (e.g. resolve_entity
        candidate person_ids — needed for the Bulk_Lookup audit flag rule,
        §12) but still count toward the audit trail's records_accessed."""
        if record_id:
            self.records_accessed.add(record_id)

    def citations_for(self, keys: list[str]) -> list[Citation]:
        """Materialize Citation objects for known keys; unknown keys are
        dropped silently (composer.py §7 rule)."""
        return [self.provenance[k].to_citation() for k in keys if k in self.provenance]

    def get_payload(self, payload_id: str) -> Any | None:
        return self.payloads.get(payload_id)

    def add_usage(self, usage: dict[str, int]) -> None:
        for key in ("prompt_tokens", "completion_tokens", "total_tokens",
                    "cache_read_tokens", "cache_write_tokens"):
            self.usage[key] += usage.get(key, 0)

    def record_tool_use(self, tool_name: str) -> None:
        self.tools_used.append(tool_name)

    def emit_event(self, event: dict[str, Any]) -> None:
        self.events.append(event)
        if self.on_event is not None:
            self.on_event(event)

    def provenance_summary(self) -> list[dict[str, str]]:
        """Live list of registered keys for the '## Provenance keys' prompt
        section (§6.1), re-rendered each iteration."""
        out = []
        for key, entry in self.provenance.items():
            label = entry.ref.get("fir_id", "")
            if entry.kind == "field":
                label = f"{label}.{entry.ref.get('field', '')}"
            out.append({"key": key, "kind": entry.kind, "label": label})
        return out

    def _accumulate_records_from_block(self, block: Any) -> None:
        """Auto-accumulate every fir_id/person_id/vehicle registration
        appearing in a registered payload (§8 records_accessed)."""
        data = block if isinstance(block, dict) else block.model_dump()
        for value in _walk_values(data):
            if isinstance(value, str) and _FIR_ID_RE.match(value):
                self.records_accessed.add(value)


def _walk_values(obj: Any):
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _walk_values(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_values(v)
    else:
        yield obj
