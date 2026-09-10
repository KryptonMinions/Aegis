"""R0 gate — startup worker-count safety (R2_steering_docs.md R-1/§2.4).

Checks:
  1. InMemoryThreadStore round-trip behavior is unchanged (regression only).
  2. The FastAPI lifespan hook refuses to start when THREAD_STORE_BACKEND=memory
     and WEB_CONCURRENCY>1, and starts fine otherwise.

Needs live Supabase creds in backend/.env (app.main imports app.config at
module load, same as every other router). Run:
    cd backend && python -m scripts.test_r0_gate
"""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent.threads import InMemoryThreadStore, TurnSummary  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.main import lifespan  # noqa: E402


class _FakeApp:
    pass


def _reconfigure(*, thread_store_backend: str, worker_concurrency: str) -> None:
    os.environ["THREAD_STORE_BACKEND"] = thread_store_backend
    os.environ["WEB_CONCURRENCY"] = worker_concurrency
    get_settings.cache_clear()


async def main() -> int:
    passed = failed = 0

    def check(label: str, cond: bool, detail: str = "") -> None:
        nonlocal passed, failed
        if cond:
            passed += 1
            print(f"  PASS  {label}  {detail}")
        else:
            failed += 1
            print(f"  FAIL  {label}  {detail}")

    print("=== InMemoryThreadStore round-trip (unchanged behavior) ===")
    store = InMemoryThreadStore(ttl_s=3600)
    await store.append("t1", TurnSummary(query="q1", specialist="case_investigator", answer_summary="a1"))
    await store.append("t1", TurnSummary(query="q2", specialist="case_investigator", answer_summary="a2"))
    recent = await store.get_recent("t1", limit=6)
    check("get_recent returns appended turns in order", [t.query for t in recent] == ["q1", "q2"])
    check("other thread stays isolated", await store.get_recent("t2") == [])

    orig_backend = os.environ.get("THREAD_STORE_BACKEND")
    orig_concurrency = os.environ.get("WEB_CONCURRENCY")
    try:
        print("\n=== Startup: memory backend + WEB_CONCURRENCY>1 refuses to start ===")
        _reconfigure(thread_store_backend="memory", worker_concurrency="4")
        try:
            async with lifespan(_FakeApp()):
                pass
            check("memory + workers>1 raises on startup", False, "startup did not raise")
        except RuntimeError as exc:
            check("memory + workers>1 raises on startup", True, str(exc)[:80])

        print("\n=== Startup: memory backend + single worker starts fine ===")
        _reconfigure(thread_store_backend="memory", worker_concurrency="1")
        try:
            async with lifespan(_FakeApp()):
                pass
            check("memory + single worker starts", True)
        except RuntimeError as exc:
            check("memory + single worker starts", False, str(exc)[:80])

        print("\n=== Startup: catalyst backend ignores worker count ===")
        _reconfigure(thread_store_backend="catalyst", worker_concurrency="8")
        try:
            async with lifespan(_FakeApp()):
                pass
            check("catalyst backend starts regardless of worker count", True)
        except RuntimeError as exc:
            check("catalyst backend starts regardless of worker count", False, str(exc)[:80])
    finally:
        for key, val in (("THREAD_STORE_BACKEND", orig_backend), ("WEB_CONCURRENCY", orig_concurrency)):
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val
        get_settings.cache_clear()

    print(f"\nR0 GATE: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
