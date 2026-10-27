"""Per-client-IP sliding-window rate limiting for the expensive/public routes.

With passwordless login anyone who opens the app can call /ask (which spends
LLM budget), so this is the abuse brake. In-process only.
"""

from __future__ import annotations

import math
import time
from collections import deque
from typing import Callable, Sequence

from fastapi import Request

from app.config import Settings


class SlidingWindowLimiter:
    # ponytail: per-instance counters, so the effective cap is N x Cloud Run
    # instances (keep --max-instances low). Move to Firestore/Redis counters
    # if a hard global cap ever matters.
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}

    def hit(self, key: str, limits: Sequence[tuple[int, int]]) -> int | None:
        """Record a request for `key` against (window_seconds, max_requests)
        limits. Returns None if allowed, else the seconds until it would be.
        A rejected request is not recorded. Limits with max <= 0 are off."""
        active = [(w, m) for w, m in limits if m > 0]
        if not active:
            return None
        now = self._clock()
        hits = self._hits.setdefault(key, deque())
        longest = max(w for w, _ in active)
        while hits and now - hits[0] >= longest:
            hits.popleft()
        retry = 0
        for window, max_requests in active:
            recent = [t for t in hits if now - t < window]
            if len(recent) >= max_requests:
                retry = max(retry, math.ceil(window - (now - recent[0])))
        if retry:
            return max(1, retry)
        hits.append(now)
        if len(self._hits) > 10_000:  # drop idle keys so memory stays bounded
            self._hits = {k: v for k, v in self._hits.items() if v and now - v[-1] < longest}
        return None


def client_ip(request: Request) -> str:
    # Cloud Run's front end appends the connecting client to X-Forwarded-For,
    # so the last entry is the only one a caller can't forge. Without a proxy
    # (local dev) fall back to the socket peer.
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


_limiter = SlidingWindowLimiter()


def ask_retry_after(request: Request, settings: Settings) -> int | None:
    return _limiter.hit(
        f"ask:{client_ip(request)}",
        [(60, settings.ask_rate_limit_per_minute), (3600, settings.ask_rate_limit_per_hour)],
    )


def login_retry_after(request: Request, settings: Settings) -> int | None:
    return _limiter.hit(
        f"login:{client_ip(request)}", [(60, settings.login_rate_limit_per_minute)]
    )
