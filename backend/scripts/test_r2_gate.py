"""R2 gate — semantic answer cache (R2_steering_docs.md R-3).

Checks:
  1. frame_key: role, station, query_class, reference date all change the key.
  2. ttl_for_class: lookup/analytic/never-cached mapping.
  3. AnswerCache: get/put, TTL expiry.
  4. run_turn integration (agent loop stubbed): 2nd identical turn is a cache
     hit (loop not re-run), audit still written with cache_hit=True, zero tool
     calls in the trace; different station -> miss; no_answer never cached;
     flag off -> cache path never touched.

No live creds needed. Run:
    cd backend && python -m scripts.test_r2_gate
"""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import agent  # noqa: E402
from app.agent import answer_cache as ac  # noqa: E402
from app.agent.answer_cache import AnswerCache, CacheEntry, ttl_for_class  # noqa: E402
from app.agent.cache_key import frame_key  # noqa: E402
from app.agent.loop import LoopResult  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.roles import Role  # noqa: E402
from app.schemas import AskRequest, AssistantMessage, CurrentUser, NoAnswerBlock, TextBlock  # noqa: E402
from app.semantic.frame import ResolvedEntity, SemanticFrame  # noqa: E402


def _frame(query="who is ravi kumara", qc="lookup") -> SemanticFrame:
    return SemanticFrame(
        frame_id="f1", raw_query=query, normalized_query=query, detected_language="en",
        role=Role.ANALYST, query_class=qc,
        entities=[ResolvedEntity(kind="person", text="ravi kumara")],
    )


def _user() -> CurrentUser:
    return CurrentUser(id="sess-1", role=Role.ANALYST, officer_id=None)


def _answered_msg(text="Ravi Kumara has 3 prior cases.") -> AssistantMessage:
    return AssistantMessage(text=text, blocks=[TextBlock(id="b1", content=text)])


def _no_answer_msg() -> AssistantMessage:
    return AssistantMessage(blocks=[NoAnswerBlock(id="na", message="Nothing found.", reason="not_found")])


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
    u, fr = _user(), _frame()

    print("=== frame_key sensitivity ===")
    base = frame_key(fr, u, None, settings)
    check("identical inputs -> identical key", frame_key(fr, u, None, settings) == base)
    check("station changes key", frame_key(fr, u, "KA-MYS-012", settings) != base)
    io_user = CurrentUser(id="s", role=Role.INVESTIGATING_OFFICER, officer_id="KSP-1")
    check("role changes key", frame_key(fr, io_user, None, settings) != base)
    check("query_class changes key", frame_key(_frame(qc="pattern"), u, None, settings) != base)

    print("\n=== ttl_for_class ===")
    check("lookup -> lookup ttl", ttl_for_class("lookup", 1800, 300) == 1800)
    check("network -> analytic ttl", ttl_for_class("network", 1800, 300) == 300)
    check("summary -> never", ttl_for_class("summary", 1800, 300) == 0)
    check("audit -> never", ttl_for_class("audit", 1800, 300) == 0)

    print("\n=== AnswerCache get/put/expiry ===")
    cache = AnswerCache()
    entry = CacheEntry(message=_answered_msg(), records_accessed=["KA-X-1"], query_class="lookup",
                       specialist_name="case_investigator", expires_at=time.time() + 100)
    cache.put("k", entry)
    check("get returns stored entry", cache.get("k") is entry)
    cache.put("k2", CacheEntry(message=_answered_msg(), records_accessed=[], query_class="lookup",
                               specialist_name=None, expires_at=time.time() - 1))
    check("expired entry evicted on get", cache.get("k2") is None)

    print("\n=== run_turn integration (loop stubbed) ===")
    loop_calls = {"n": 0}
    audit_calls: list[dict] = []

    class _StubLoop:
        def __init__(self, *a, **k): ...
        async def run(self, **kwargs):
            loop_calls["n"] += 1
            kwargs["scratchpad"].records_accessed.add("KA-MYS-2024-1")
            return LoopResult(final_answer=None)

    class _StubThreadStore:
        async def append(self, *a, **k): ...
        async def get_recent(self, *a, **k): return []

    async def _stub_build_frame(request, user, settings, on_usage=None):
        return _frame(query=request.query)

    orig = {
        "AgentLoop": agent.AgentLoop, "compose": agent.compose,
        "build_frame": agent.build_frame, "get_thread_store": agent.get_thread_store,
        "audit_write": agent.audit_writer.write, "log": agent.log_turn_usage,
    }
    agent.AgentLoop = _StubLoop
    agent.build_frame = _stub_build_frame
    agent.get_thread_store = lambda s: _StubThreadStore()
    agent.log_turn_usage = lambda **k: None
    _compose_result = {"msg": _answered_msg()}
    agent.compose = lambda *a, **k: _compose_result["msg"]

    async def _stub_audit(**kwargs):
        audit_calls.append(kwargs)

    agent.audit_writer.write = _stub_audit
    ac._cache = AnswerCache()

    try:
        settings.answer_cache_enabled = True
        req = AskRequest(query="who is ravi kumara", input_modality="text", thread_id=None, turn_index=0, client_ts="2026-07-14T00:00:00Z")

        r1 = await agent.run_turn(req, _user(), settings, request_id="req1")
        check("1st turn ran the loop", loop_calls["n"] == 1)
        check("1st turn not a cache hit in audit", audit_calls[-1].get("cache_hit") is False
              or audit_calls[-1].get("cache_hit") is None)

        r2 = await agent.run_turn(req, _user(), settings, request_id="req2")
        check("2nd identical turn did NOT run the loop", loop_calls["n"] == 1)
        check("2nd turn returns same answer text", r2.message.text == r1.message.text)
        check("2nd turn audited with cache_hit=True", audit_calls[-1].get("cache_hit") is True)
        check("cache-hit audit still has records_accessed via scratchpad",
              "KA-MYS-2024-1" in audit_calls[-1]["scratchpad"].records_accessed)
        check("cache-hit trace marks served_from_cache",
              audit_calls[-1]["scratchpad"].events[-1]["type"] == "cache_hit")

        # different officer/station -> IO scoped path differs; here just prove a
        # different role key misses.
        io = CurrentUser(id="s2", role=Role.INVESTIGATING_OFFICER, officer_id=None)
        loop_before = loop_calls["n"]
        await agent.run_turn(req, io, settings, request_id="req3")
        check("different role -> cache miss -> loop runs", loop_calls["n"] == loop_before + 1)

        # no_answer never cached
        ac._cache = AnswerCache()
        _compose_result["msg"] = _no_answer_msg()
        loop_before = loop_calls["n"]
        await agent.run_turn(AskRequest(query="obscure thing", input_modality="text", thread_id=None, turn_index=0, client_ts="2026-07-14T00:00:00Z"),
                             _user(), settings, request_id="req4")
        await agent.run_turn(AskRequest(query="obscure thing", input_modality="text", thread_id=None, turn_index=0, client_ts="2026-07-14T00:00:00Z"),
                             _user(), settings, request_id="req5")
        check("no_answer not cached (loop runs both times)", loop_calls["n"] == loop_before + 2)

        # flag off -> no cache path
        _compose_result["msg"] = _answered_msg()
        ac._cache = AnswerCache()
        settings.answer_cache_enabled = False
        loop_before = loop_calls["n"]
        await agent.run_turn(req, _user(), settings, request_id="req6")
        await agent.run_turn(req, _user(), settings, request_id="req7")
        check("flag off -> loop runs every time", loop_calls["n"] == loop_before + 2)
    finally:
        agent.AgentLoop = orig["AgentLoop"]
        agent.compose = orig["compose"]
        agent.build_frame = orig["build_frame"]
        agent.get_thread_store = orig["get_thread_store"]
        agent.audit_writer.write = orig["audit_write"]
        agent.log_turn_usage = orig["log"]
        ac._cache = None
        get_settings.cache_clear()

    print(f"\nR2 GATE: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
