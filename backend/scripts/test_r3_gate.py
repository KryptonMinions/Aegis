"""R3 gate — parallel tool execution (R2_steering_docs.md R-4).

Checks:
  1. Flag off -> sequential, byte-identical event/message sequence.
  2. Flag on -> 2 independent calls both run concurrently; p*/c* ids assigned
     in call order (deterministic across 5 runs) even when the 2nd call
     finishes first.
  3. Forced failure in one of two parallel calls -> {ok:false} for that call
     only; sibling result intact.
  4. run_sql calls are never run concurrently with each other.

No live creds needed. Run:
    cd backend && python -m scripts.test_r3_gate
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent.loop import AgentLoop  # noqa: E402
from app.agent.scratchpad import TurnScratchpad  # noqa: E402
from app.agent.specialists import CASE_INVESTIGATOR  # noqa: E402
from app.agent.tools.base import ToolResult  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.llm.client import LLMResponse, ToolCall  # noqa: E402
from app.roles import Role  # noqa: E402
from app.schemas import CurrentUser  # noqa: E402
from app.semantic.frame import SemanticFrame  # noqa: E402

_FINAL = '{"status":"answered","blocks":[{"kind":"text","markdown":"done"}]}'


class StubLLM:
    def __init__(self, tool_calls):
        self._tc = tool_calls
        self.calls = 0

    async def complete_with_tools(self, profile, messages, tools, **kw):
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(content=None, tool_calls=self._tc, usage={})
        return LLMResponse(content=_FINAL, tool_calls=[], usage={})


class FakeTool:
    def __init__(self, name, fir, *, delay=0.0, raises=False, track=None):
        self.name = name
        self.label = name
        self._fir = fir
        self._delay = delay
        self._raises = raises
        self._track = track

    @property
    def params_schema(self):
        return {"type": "object", "properties": {}}

    async def run(self, args, ctx):
        if self._track is not None:
            self._track["active"] += 1
            self._track["max"] = max(self._track["max"], self._track["active"])
        try:
            if self._delay:
                await asyncio.sleep(self._delay)
            if self._raises:
                raise RuntimeError("boom")
            pid = ctx.scratchpad.register_payload(
                {"type": "text", "id": f"{self.name}-blk", "content": "x"}
            )
            ctx.scratchpad.register_provenance("fir", {"fir_id": self._fir})
            return ToolResult(ok=True, data={"tool": self.name}, payload_id=pid)
        finally:
            if self._track is not None:
                self._track["active"] -= 1


def _frame():
    return SemanticFrame(
        frame_id="f1", raw_query="q", normalized_query="q", detected_language="en",
        role=Role.ANALYST, query_class="lookup", entities=[],
    )


def _user():
    return CurrentUser(id="s", role=Role.ANALYST, officer_id=None)


async def _run(settings, tools, tool_calls):
    sp = TurnScratchpad()
    llm = StubLLM(tool_calls)
    loop = AgentLoop(settings, llm_client=llm)
    await loop.run(
        specialist=CASE_INVESTIGATOR, tools=tools, frame=_frame(), user=_user(),
        scratchpad=sp, thread_id="t1", sql_scope="case",
    )
    return sp


async def main() -> int:
    passed = failed = 0

    def check(label, cond, detail=""):
        nonlocal passed, failed
        if cond:
            passed += 1
            print(f"  PASS  {label}  {detail}")
        else:
            failed += 1
            print(f"  FAIL  {label}  {detail}")

    settings = get_settings()
    calls2 = [ToolCall(id="c1", name="a", arguments={}), ToolCall(id="c2", name="b", arguments={})]

    print("=== Flag off: sequential, byte-identical event sequence ===")
    settings.parallel_tools_enabled = False
    sp = await _run(settings, [FakeTool("a", "KA-MYS-A-2024-1"), FakeTool("b", "KA-MYS-B-2024-2")], calls2)
    seq_events = [(e["type"], e.get("tool")) for e in sp.events if e["type"] in ("tool_started", "tool_finished")]
    check("sequential event order started/finished per call",
          seq_events == [("tool_started", "a"), ("tool_finished", "a"),
                         ("tool_started", "b"), ("tool_finished", "b")], str(seq_events))
    check("sequential ids p1=a p2=b",
          list(sp.payloads) == ["p1", "p2"] and sp.payloads["p1"].id == "a-blk")

    print("\n=== Flag on: both run, ids in call order, deterministic x5 ===")
    settings.parallel_tools_enabled = True
    settings.parallel_tools_max = 4
    shapes = set()
    for _ in range(5):
        # b finishes FIRST (a is slow) -> completion order != call order
        sp = await _run(
            settings,
            [FakeTool("a", "KA-MYS-A-2024-1", delay=0.02), FakeTool("b", "KA-MYS-B-2024-2", delay=0.0)],
            calls2,
        )
        shapes.add((tuple(sp.payloads), sp.payloads["p1"].id, sp.payloads["p2"].id,
                    tuple(sp.provenance)))
    check("p*/c* ids identical across 5 runs", len(shapes) == 1, str(shapes))
    shape = next(iter(shapes))
    check("call-order id assignment (p1=a even though b finished first)", shape[1] == "a-blk", str(shape))
    check("both tools' provenance merged", shape[3] == ("c1", "c2"))

    started = [e["tool"] for e in sp.events if e["type"] == "tool_started"]
    finished = [e["tool"] for e in sp.events if e["type"] == "tool_finished"]
    check("all tool_started emitted up front", started == ["a", "b"], str(started))
    check("tool_finished for both, call order", finished == ["a", "b"], str(finished))

    print("\n=== Flag on: one parallel call fails, sibling unaffected ===")
    sp = await _run(
        settings,
        [FakeTool("a", "KA-MYS-A-2024-1", raises=True), FakeTool("b", "KA-MYS-B-2024-2")],
        calls2,
    )
    fin = {e["tool"]: e["ok"] for e in sp.events if e["type"] == "tool_finished"}
    check("failing call reported ok=False", fin.get("a") is False, str(fin))
    check("sibling call reported ok=True", fin.get("b") is True, str(fin))
    check("only sibling's payload registered", [p.id for p in sp.payloads.values()] == ["b-blk"])

    print("\n=== Flag on: two run_sql calls never overlap ===")
    track = {"active": 0, "max": 0}
    sql_calls = [ToolCall(id="c1", name="run_sql", arguments={}), ToolCall(id="c2", name="run_sql", arguments={})]
    await _run(
        settings,
        [FakeTool("run_sql", "KA-MYS-A-2024-9", delay=0.02, track=track)],  # one tool obj, name matches both calls
        sql_calls,
    )
    check("run_sql max concurrency == 1", track["max"] == 1, f"max={track['max']}")

    # cleanup
    settings.parallel_tools_enabled = False
    get_settings.cache_clear()

    print(f"\nR3 GATE: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
