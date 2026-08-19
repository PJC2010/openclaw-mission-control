"""Normalizer — the only writer of runs/events (§6.3).

Adapters emit `NormalizedEvent`s and snapshots; this package owns
persistence, dedupe, per-agent sequence assignment, and live broadcast.
A buggy adapter can therefore emit garbage but never corrupt the
approval path or write tables directly.
"""

from .contract import (
    AgentStatus,
    NormalizedEvent,
    RunUpsert,
    ScheduledTaskSnapshot,
)
from .service import EventBroadcast, EventRecord, Normalizer

__all__ = [
    "AgentStatus",
    "NormalizedEvent",
    "RunUpsert",
    "ScheduledTaskSnapshot",
    "EventBroadcast",
    "EventRecord",
    "Normalizer",
]
