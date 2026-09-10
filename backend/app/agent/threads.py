"""ThreadStore (ORCHESTRATOR_STEERING.md O-13, §6.3).

Holds up to ASK_THREAD_TTL_S worth of per-thread turn summaries (query,
specialist, one-line answer summary, entity IDs — §6.1's '## Conversation
context' prompt section pulls the last 6).

Two backends:
- `memory` — process-global dict, per-thread asyncio lock. Why the backend
  must run `uvicorn --workers 1` (O-13): this state doesn't cross processes.
- `catalyst` — Zoho Catalyst Cache. Durable and multi-worker safe (each worker
  hits the same remote cache). One cache key per thread_id holding a
  JSON-serialized list of turns. Verified live against a real segment before
  being wired in here (insert/retrieve/TTL all confirmed against
  api.catalyst.zoho.<tld>/baas/v1/project/{id}/segment/{id}/cache).
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

import httpx

from app.catalyst import CatalystError, cache_url, catalyst_headers
from app.config import Settings

# R2 R-5: thread memory is best-effort. A backend outage (HTTP error, or an
# OAuth refresh failure surfacing as CatalystError) degrades to no context,
# never a failed turn.
_THREAD_STORE_ERRORS = (httpx.HTTPError, CatalystError)


@dataclass
class TurnSummary:
    query: str
    specialist: str
    answer_summary: str
    entity_ids: list[str] = field(default_factory=list)
    ts: float = field(default_factory=time.time)


class ThreadStore(Protocol):
    async def append(self, thread_id: str, turn: TurnSummary) -> None: ...
    async def get_recent(self, thread_id: str, limit: int = 6) -> list[TurnSummary]: ...


class InMemoryThreadStore:
    def __init__(self, ttl_s: int) -> None:
        self._ttl_s = ttl_s
        self._threads: dict[str, list[TurnSummary]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    async def _lock_for(self, thread_id: str) -> asyncio.Lock:
        async with self._locks_guard:
            lock = self._locks.get(thread_id)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[thread_id] = lock
            return lock

    def _evict_expired(self, thread_id: str) -> None:
        cutoff = time.time() - self._ttl_s
        turns = self._threads.get(thread_id)
        if not turns:
            return
        self._threads[thread_id] = [t for t in turns if t.ts >= cutoff]

    async def append(self, thread_id: str, turn: TurnSummary) -> None:
        lock = await self._lock_for(thread_id)
        async with lock:
            self._evict_expired(thread_id)
            self._threads.setdefault(thread_id, []).append(turn)

    async def get_recent(self, thread_id: str, limit: int = 6) -> list[TurnSummary]:
        lock = await self._lock_for(thread_id)
        async with lock:
            self._evict_expired(thread_id)
            return self._threads.get(thread_id, [])[-limit:]


# Turns stored per thread, independent of get_recent's requested `limit`
# (which is the ≤6 prompt-context window) — a wider history is kept so a
# later turn can still see further back if some future caller asks for more.
_CATALYST_MAX_STORED_TURNS = 20


class CatalystCacheThreadStore:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._segment_id = settings.catalyst_cache_segment_id
        self._ttl_hours = max(1, round(settings.ask_thread_ttl_s / 3600))
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    async def _lock_for(self, thread_id: str) -> asyncio.Lock:
        async with self._locks_guard:
            lock = self._locks.get(thread_id)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[thread_id] = lock
            return lock

    async def _get_raw(self, thread_id: str) -> list[dict[str, Any]]:
        headers = await catalyst_headers(self._settings, json_body=False)
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                cache_url(self._settings, self._segment_id),
                headers=headers,
                params={"cacheKey": thread_id},
            )
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        raw_value = (resp.json().get("data") or {}).get("cache_value")
        if not raw_value:
            return []
        try:
            return json.loads(raw_value)
        except json.JSONDecodeError:
            return []

    async def _put_raw(self, thread_id: str, items: list[dict[str, Any]]) -> None:
        headers = await catalyst_headers(self._settings)
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                cache_url(self._settings, self._segment_id),
                headers=headers,
                json={
                    "cache_name": thread_id,
                    "cache_value": json.dumps(items),
                    "expiry_in_hours": self._ttl_hours,
                },
            )
        resp.raise_for_status()

    async def append(self, thread_id: str, turn: TurnSummary) -> None:
        lock = await self._lock_for(thread_id)
        async with lock:
            try:
                items = await self._get_raw(thread_id)
                items.append(asdict(turn))
                items = items[-_CATALYST_MAX_STORED_TURNS:]
                await self._put_raw(thread_id, items)
            except _THREAD_STORE_ERRORS:
                # Conversation memory is a quality-of-life feature, not
                # critical path — never fail the user's turn over it.
                pass

    async def get_recent(self, thread_id: str, limit: int = 6) -> list[TurnSummary]:
        try:
            items = await self._get_raw(thread_id)
        except _THREAD_STORE_ERRORS:
            return []
        turns = [TurnSummary(**item) for item in items]
        return turns[-limit:]


_memory_store: InMemoryThreadStore | None = None
_catalyst_store: CatalystCacheThreadStore | None = None


def get_thread_store(settings: Settings) -> ThreadStore:
    """Process-global singleton (O-13) — never construct a store per-request."""
    global _memory_store, _catalyst_store
    if settings.thread_store_backend == "catalyst":
        if _catalyst_store is None:
            _catalyst_store = CatalystCacheThreadStore(settings)
        return _catalyst_store
    if _memory_store is None:
        _memory_store = InMemoryThreadStore(settings.ask_thread_ttl_s)
    return _memory_store
