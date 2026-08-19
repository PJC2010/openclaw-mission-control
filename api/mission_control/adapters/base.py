"""The `AgentAdapter` protocol (§6.4)."""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass
class AdapterHealth:
    ok: bool
    state: str  # 'starting' | 'running' | 'degraded' | 'failed' | 'stopped'
    detail: str = ""
    last_ingest_at: datetime.datetime | None = None
    errors: list[str] = field(default_factory=list)

    def as_json(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "state": self.state,
            "detail": self.detail,
            "last_ingest_at": self.last_ingest_at.isoformat() if self.last_ingest_at else None,
            "errors": list(self.errors),
        }


@runtime_checkable
class AgentAdapter(Protocol):
    """Lifecycle contract every runtime adapter implements."""

    name: str

    async def start(self) -> None:
        """Begin ingesting; returns once background work is scheduled."""

    async def stop(self) -> None:
        """Stop cleanly; safe to call twice."""

    async def backfill(self, since: datetime.datetime) -> int:
        """Re-ingest history from `since`; returns events emitted. Used for
        operator-triggered recovery beyond the automatic cursor resume."""

    def health(self) -> AdapterHealth:
        """Current adapter health for /v1/system/health."""
