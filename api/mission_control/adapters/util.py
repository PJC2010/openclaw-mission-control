"""Shared adapter helpers."""

from __future__ import annotations

import datetime
import hashlib
from typing import Any


def clipped_text(value: str | None, limit: int) -> dict[str, Any] | None:
    """Bounded observability copy of runtime text.

    Events carry a head + full-content hash when clipped; the S7
    no-truncation rule is about `approvals.tool_args` (stored complete by
    the Phase 2 gateway), not these observation copies. The clip is always
    explicit — `truncated: true` plus the original length — never silent.
    """
    if value is None:
        return None
    if len(value) <= limit:
        return {"text": value, "truncated": False}
    return {
        "text": value[:limit],
        "truncated": True,
        "full_length": len(value),
        "full_sha256": hashlib.sha256(value.encode("utf-8", "replace")).hexdigest(),
    }


def ts_from_epoch(value: float | int | None) -> datetime.datetime | None:
    if value is None:
        return None
    return datetime.datetime.fromtimestamp(float(value), tz=datetime.timezone.utc)


def ts_from_iso(value: str | None) -> datetime.datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed


def ts_from_ms(value: int | None) -> datetime.datetime | None:
    if value is None:
        return None
    return datetime.datetime.fromtimestamp(value / 1000.0, tz=datetime.timezone.utc)
