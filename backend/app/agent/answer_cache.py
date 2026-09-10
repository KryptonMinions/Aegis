"""Pre-loop semantic answer cache (R2_steering_docs.md R-3).

In-process TTL cache only. This mirrors InMemoryThreadStore's "single worker"
constraint — the R0 startup guard already refuses to boot memory-backed shared
state with WEB_CONCURRENCY>1, and the catalyst deployment runs one worker.

# ponytail: in-process dict, no shared backend. Add a catalyst-backed impl
# (mirroring CatalystCacheThreadStore) only if a multi-worker deploy needs a
# shared answer cache — until then it is dead code.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from app.schemas import AssistantMessage

# query_class -> which TTL setting applies (§4.1 / §1). Classes not listed are
# never cached (summary, audit, unresolved).
_LOOKUP_CLASSES = {"lookup", "case_detail", "pattern"}
_ANALYTIC_CLASSES = {"geo_analytics", "trend", "network"}

_MAX_ENTRIES = 512


def ttl_for_class(query_class: str, ttl_lookup_s: int, ttl_analytic_s: int) -> int:
    if query_class in _LOOKUP_CLASSES:
        return ttl_lookup_s
    if query_class in _ANALYTIC_CLASSES:
        return ttl_analytic_s
    return 0


@dataclass
class CacheEntry:
    message: AssistantMessage
    records_accessed: list[str]
    query_class: str
    specialist_name: str | None
    expires_at: float


@dataclass
class AnswerCache:
    _entries: dict[str, CacheEntry] = field(default_factory=dict)

    def get(self, key: str) -> CacheEntry | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        if time.time() >= entry.expires_at:
            del self._entries[key]
            return None
        return entry

    def put(self, key: str, entry: CacheEntry) -> None:
        if len(self._entries) >= _MAX_ENTRIES:
            # cheapest eviction: drop the soonest-to-expire
            oldest = min(self._entries, key=lambda k: self._entries[k].expires_at)
            del self._entries[oldest]
        self._entries[key] = entry


_cache: AnswerCache | None = None


def get_answer_cache() -> AnswerCache:
    global _cache
    if _cache is None:
        _cache = AnswerCache()
    return _cache
