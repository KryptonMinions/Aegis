"""ThreadStore (ORCHESTRATOR_STEERING.md O-13, §6.3).

Holds up to ASK_THREAD_TTL_S worth of per-thread turn summaries (query,
specialist, one-line answer summary, entity IDs — §6.1's '## Conversation
context' prompt section pulls the last 6).

Two backends:
- `memory` — process-global dict, per-thread asyncio lock. Why the backend
  must run `uvicorn --workers 1` (O-13): this state doesn't cross processes.
- `firestore` — Cloud Firestore. Durable and multi-instance safe. One doc per
  thread_id in `ask_threads` holding the turn list plus an `expires_at` field
  (enable a Firestore TTL policy on it; reads also ignore expired docs).
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from google.api_core.exceptions import GoogleAPICallError
from google.auth.exceptions import GoogleAuthError

from app.config import Settings

# R2 R-5: thread memory is best-effort. A backend outage (Firestore API error
# or credential failure) degrades to no context, never a failed turn.
_THREAD_STORE_ERRORS = (GoogleAPICallError, GoogleAuthError)


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
_MAX_STORED_TURNS = 20
_COLLECTION = "ask_threads"


class FirestoreThreadStore:
    def __init__(self, settings: Settings) -> None:
        from google.cloud import firestore  # lazy: memory mode needs no GCP creds

        self._ttl_s = settings.ask_thread_ttl_s
        self._db = firestore.AsyncClient(project=settings.gcp_project_id or None)
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    async def _lock_for(self, thread_id: str) -> asyncio.Lock:
        async with self._locks_guard:
            lock = self._locks.get(thread_id)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[thread_id] = lock
            return lock

    def _doc(self, thread_id: str):
        return self._db.collection(_COLLECTION).document(thread_id)

    async def _get_raw(self, thread_id: str) -> list[dict[str, Any]]:
        snap = await self._doc(thread_id).get()
        if not snap.exists:
            return []
        data = snap.to_dict() or {}
        # TTL deletion in Firestore lags by up to ~24h, so enforce expiry here too.
        exp = data.get("expires_at")
        if exp is not None and exp.timestamp() < time.time():
            return []
        return data.get("turns") or []

    async def append(self, thread_id: str, turn: TurnSummary) -> None:
        lock = await self._lock_for(thread_id)
        async with lock:
            try:
                items = await self._get_raw(thread_id)
                items.append(asdict(turn))
                await self._doc(thread_id).set({
                    "turns": items[-_MAX_STORED_TURNS:],
                    "expires_at": datetime.fromtimestamp(time.time() + self._ttl_s, tz=timezone.utc),
                })
            except _THREAD_STORE_ERRORS:
                # Conversation memory is a quality-of-life feature, not
                # critical path — never fail the user's turn over it.
                pass

    async def get_recent(self, thread_id: str, limit: int = 6) -> list[TurnSummary]:
        try:
            items = await self._get_raw(thread_id)
        except _THREAD_STORE_ERRORS:
            return []
        return [TurnSummary(**item) for item in items][-limit:]


_memory_store: InMemoryThreadStore | None = None
_firestore_store: FirestoreThreadStore | None = None


def get_thread_store(settings: Settings) -> ThreadStore:
    """Process-global singleton (O-13) — never construct a store per-request."""
    global _memory_store, _firestore_store
    if settings.thread_store_backend == "firestore":
        if _firestore_store is None:
            _firestore_store = FirestoreThreadStore(settings)
        return _firestore_store
    if _memory_store is None:
        _memory_store = InMemoryThreadStore(settings.ask_thread_ttl_s)
    return _memory_store
