"""R4 gate — load & failure drill (R2_steering_docs.md R-4 milestone / R-5).

Checks (agent loop stubbed; no live creds):
  1. 10 concurrent turns across distinct thread_ids -> no cross-thread bleed.
  2. Thread-store outage -> turn still returns (no 5xx).
  3. Answer-cache outage -> falls through to the loop, turn still returns.
  4. Cache-hit path latency: p50 well under 1s and far below the miss path.

Run:
    cd backend && python -m scripts.test_r4_gate
"""

import asyncio
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import agent  # noqa: E402
from app.agent import answer_cache as ac  # noqa: E402
from app.agent.answer_cache import AnswerCache  # noqa: E402
from app.agent.loop import LoopResult  # noqa: E402
from app.agent.threads import InMemoryThreadStore, TurnSummary  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.roles import Role  # noqa: E402
from app.schemas import AskRequest, AssistantMessage, CurrentUser, TextBlock  # noqa: E402
from app.semantic.frame import SemanticFrame  # noqa: E402


def _frame(q):
    return SemanticFrame(frame_id="f", raw_query=q, normalized_query=q, detected_language="en",
                         role=Role.ANALYST, query_class="lookup", entities=[])


def _user():
    return CurrentUser(id="s", role=Role.ANALYST, officer_id=None)


def _msg(t="ok"):
    return AssistantMessage(text=t, blocks=[TextBlock(id="b", content=t)])


def _req(q, tid=None):
    return AskRequest(query=q, input_modality="text", thread_id=tid, turn_index=0,
                      client_ts="2026-07-14T00:00:00Z")


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
    loop_delay = {"s": 0.0}

    class _StubLoop:
        def __init__(self, *a, **k): ...
        async def run(self, **kw):
            if loop_delay["s"]:
                await asyncio.sleep(loop_delay["s"])
            kw["scratchpad"].records_accessed.add(f"KA-MYS-2024-{kw['thread_id'][-1]}")
            return LoopResult(final_answer=None)

    orig = {k: getattr(agent, k) for k in ("AgentLoop", "compose", "build_frame",
                                           "get_thread_store", "get_answer_cache", "log_turn_usage")}
    agent.AgentLoop = _StubLoop
    agent.compose = lambda *a, **k: _msg()
    agent.log_turn_usage = lambda **k: None

    async def _bf(request, user, settings, on_usage=None):
        return _frame(request.query)
    agent.build_frame = _bf

    async def _audit_noop(**k):
        ...
    orig_audit = agent.audit_writer.write
    agent.audit_writer.write = _audit_noop

    try:
        # ---- 1. concurrent load, distinct threads ----
        print("=== 10 concurrent turns, distinct threads ===")
        store = InMemoryThreadStore(ttl_s=3600)
        agent.get_thread_store = lambda s: store
        ac._cache = AnswerCache()
        settings.answer_cache_enabled = False

        await asyncio.gather(*[
            agent.run_turn(_req(f"q{i}", tid=f"thread-{i}"), _user(), settings, request_id=f"r{i}")
            for i in range(10)
        ])
        bleed = []
        for i in range(10):
            turns = await store.get_recent(f"thread-{i}")
            if len(turns) != 1 or turns[0].query != f"q{i}":
                bleed.append((i, [t.query for t in turns]))
        check("no cross-thread context bleed", not bleed, str(bleed))

        # ---- 2. thread-store outage ----
        print("\n=== thread-store outage degrades, no exception ===")

        class _BrokenStore:
            async def append(self, *a, **k):
                raise RuntimeError("catalyst down")
            async def get_recent(self, *a, **k):
                raise RuntimeError("catalyst down")
        agent.get_thread_store = lambda s: _BrokenStore()
        try:
            resp = await agent.run_turn(_req("q", tid="thread-x"), _user(), settings, request_id="rx")
            check("turn returns despite thread-store outage", resp.message.text == "ok")
        except Exception as exc:  # noqa: BLE001
            check("turn returns despite thread-store outage", False, repr(exc))

        # ---- 3. answer-cache outage ----
        print("\n=== answer-cache outage falls through to loop ===")
        agent.get_thread_store = lambda s: store

        class _BrokenCache:
            def get(self, k):
                raise RuntimeError("cache down")
            def put(self, k, v):
                raise RuntimeError("cache down")
        agent.get_answer_cache = lambda: _BrokenCache()
        settings.answer_cache_enabled = True
        try:
            resp = await agent.run_turn(_req("q", tid="thread-c"), _user(), settings, request_id="rc")
            check("turn returns despite cache outage", resp.message.text == "ok")
        except Exception as exc:  # noqa: BLE001
            check("turn returns despite cache outage", False, repr(exc))

        # ---- 4. cache-hit latency ----
        print("\n=== cache-hit latency p50 < 1s and << miss path ===")
        agent.get_answer_cache = orig["get_answer_cache"]
        ac._cache = AnswerCache()
        loop_delay["s"] = 0.25  # make the miss path visibly slow

        t0 = time.monotonic()
        await agent.run_turn(_req("hot query", tid="thread-h"), _user(), settings, request_id="miss")
        miss_ms = (time.monotonic() - t0) * 1000

        hit_ms = []
        for i in range(5):
            t0 = time.monotonic()
            await agent.run_turn(_req("hot query", tid="thread-h"), _user(), settings, request_id=f"hit{i}")
            hit_ms.append((time.monotonic() - t0) * 1000)
        p50 = statistics.median(hit_ms)
        check("cache-hit p50 < 1000ms", p50 < 1000, f"p50={p50:.1f}ms")
        check("cache-hit p50 << miss path", p50 < miss_ms / 2, f"hit={p50:.1f}ms miss={miss_ms:.1f}ms")
    finally:
        for k, v in orig.items():
            setattr(agent, k, v)
        agent.audit_writer.write = orig_audit
        ac._cache = None
        settings.answer_cache_enabled = False
        loop_delay["s"] = 0.0
        get_settings.cache_clear()

    print(f"\nR4 GATE: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
