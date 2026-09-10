"""R1 gate — prompt caching (R2_steering_docs.md R-2).

Checks:
  1. The static system-prompt prefix is byte-identical across iterations of a
     turn (§3.2 — no interpolated timestamps/counters).
  2. prefix + dynamic suffix reproduces the pre-split system prompt exactly
     (answer-unchanged guarantee).
  3. Cache-token usage from the provider flows through the scratchpad into the
     ask_turn_traces usage blob.

No live creds needed. Run:
    cd backend && python -m scripts.test_r1_gate
"""

import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent.loop import _build_dynamic_suffix, _build_static_prefix  # noqa: E402
from app.agent.scratchpad import TurnScratchpad  # noqa: E402
from app.agent.specialists import CASE_INVESTIGATOR  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.llm.client import LLMClient  # noqa: E402
from app.roles import Role  # noqa: E402
from app.schemas import CurrentUser  # noqa: E402
from app.semantic.frame import ResolvedEntity, SemanticFrame  # noqa: E402


def _frame() -> SemanticFrame:
    return SemanticFrame(
        frame_id="frame-1",
        raw_query="who is ravi kumara",
        normalized_query="who is ravi kumara",
        detected_language="en",
        role=Role.INVESTIGATING_OFFICER,
        query_class="lookup",
        entities=[ResolvedEntity(kind="person", text="ravi kumara")],
    )


def _user() -> CurrentUser:
    return CurrentUser(id="sess-1", role=Role.INVESTIGATING_OFFICER, officer_id="KSP-23417")


def main() -> int:
    passed = failed = 0

    def check(label: str, cond: bool, detail: str = "") -> None:
        nonlocal passed, failed
        if cond:
            passed += 1
            print(f"  PASS  {label}  {detail}")
        else:
            failed += 1
            print(f"  FAIL  {label}  {detail}")

    settings = get_settings()
    user, frame = _user(), _frame()

    print("=== Static prefix byte-identical across iterations ===")
    hashes = {
        hashlib.sha256(
            _build_static_prefix(CASE_INVESTIGATOR, user, settings, "Station KA-MYS-012 only.").encode()
        ).hexdigest()
        for _ in range(5)
    }
    check("prefix stable across 5 rebuilds", len(hashes) == 1, next(iter(hashes))[:16])

    print("\n=== prefix + suffix == pre-split system prompt ===")
    prefix = _build_static_prefix(CASE_INVESTIGATOR, user, settings, None)
    suffix = _build_dynamic_suffix(frame, [])
    combined = prefix if not suffix else f"{prefix}\n\n{suffix}"
    # Reconstruct the old flat join from the same pieces.
    from app.agent.loop import _available_data_section, _load_prompt

    old_sections = [
        f"## Your operator\nRole: {user.role.value}; officer_id: {user.officer_id}",
        f"## Reference date\n{settings.ask_reference_date or 'server date (not pinned)'}",
        f"## Available data\n{_available_data_section(CASE_INVESTIGATOR.sql_scope)}",
        '## Resolved entities (candidate mentions — not yet DB-canonical)\n'
        '- person: "ravi kumara"\nCall resolve_entity to get canonical IDs before relying on these.',
    ]
    old = "\n\n".join(
        [_load_prompt("shared.v1.md"), _load_prompt(CASE_INVESTIGATOR.prompt_file), *old_sections]
    )
    check("combined prompt matches legacy join", combined == old,
          "" if combined == old else "MISMATCH")

    print("\n=== Cache-token usage instrumentation ===")
    sp = TurnScratchpad()
    sp.add_usage({"prompt_tokens": 4000, "completion_tokens": 200, "total_tokens": 4200,
                  "cache_read_tokens": 3800, "cache_write_tokens": 0})
    sp.add_usage({"prompt_tokens": 4100, "completion_tokens": 150, "total_tokens": 4250,
                  "cache_read_tokens": 3900})
    check("cache_read_tokens accumulates", sp.usage["cache_read_tokens"] == 7700,
          str(sp.usage["cache_read_tokens"]))
    check("cache_write_tokens present", "cache_write_tokens" in sp.usage)

    parsed = LLMClient._parse({
        "choices": [{"message": {"content": "hi", "tool_calls": []}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110,
                  "prompt_tokens_details": {"cached_tokens": 80}},
    })
    check("_parse extracts cached_tokens from prompt_tokens_details",
          parsed.usage["cache_read_tokens"] == 80, str(parsed.usage["cache_read_tokens"]))

    parsed_none = LLMClient._parse({
        "choices": [{"message": {"content": "hi", "tool_calls": []}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
    })
    check("_parse defaults cache_read_tokens to 0 when absent",
          parsed_none.usage["cache_read_tokens"] == 0)

    print(f"\nR1 GATE: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
