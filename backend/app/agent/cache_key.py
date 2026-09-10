"""FrameKey — canonical cache key for the pre-loop semantic answer cache
(R2_steering_docs.md R-3 / §4.1).

Role and station are part of the key: a station-scoped or role-scoped answer
must never be served to a different station or role.
"""

from __future__ import annotations

import hashlib

from app.config import Settings
from app.schemas import CurrentUser
from app.semantic.frame import SemanticFrame


def frame_key(
    frame: SemanticFrame,
    user: CurrentUser,
    station_id: str | None,
    settings: Settings,
) -> str:
    # Entity mentions (not canonical IDs — those only exist after the loop
    # runs resolve_entity; identical query -> identical mentions -> identical
    # resolution, so the mentions are a sound proxy pre-loop).
    mentions = sorted(e.text.strip().lower() for e in frame.entities)
    parts = [
        frame.normalized_query.strip().lower(),
        "|".join(mentions),
        user.role.value,
        station_id or "",
        frame.query_class,
        settings.ask_reference_date or "",
    ]
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()
