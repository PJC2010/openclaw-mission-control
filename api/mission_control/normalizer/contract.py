"""The normalized event contract every adapter emits (§6.3).

Spec shape: { agent_id, external_run_id, seq, ts, kind, payload, dedupe_key }.
`source_seq` is the runtime-side ordering hint (audit-ledger sequence,
SQLite rowid, …) used to order events within a batch; the *stored*
`events.seq` — the SSE resume cursor — is assigned by the normalizer,
because runtime sequences reset across reconnects and interleave across
source streams.

`RunUpsert` is how adapters describe run lifecycle without touching the
runs table themselves.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Any

from ..models.enums import EventKind, RunStatus, RunTrigger


@dataclass
class RunUpsert:
    external_id: str
    started_at: datetime.datetime | None = None
    ended_at: datetime.datetime | None = None
    status: RunStatus | None = None
    trigger: RunTrigger | None = None
    parent_external_id: str | None = None  # same-agent subagent linkage (§6.1)
    error_summary: str | None = None
    token_input: int | None = None
    token_output: int | None = None
    cost_usd: float | None = None


@dataclass
class NormalizedEvent:
    ts: datetime.datetime
    kind: EventKind
    payload: dict[str, Any] = field(default_factory=dict)
    external_run_id: str | None = None
    dedupe_key: str | None = None
    source_seq: int | None = None
    run: RunUpsert | None = None  # lifecycle hints carried with the event


@dataclass
class ScheduledTaskSnapshot:
    external_id: str
    name: str
    cron_expr: str | None = None
    timezone: str = "UTC"
    next_fire_at: datetime.datetime | None = None
    last_outcome: str | None = None
    consecutive_failures: int = 0
    enabled: bool = True
    writeback_supported: bool = False


@dataclass
class AgentStatus:
    status: str  # 'up' | 'down' | 'unknown'
    heartbeat_at: datetime.datetime | None = None
    detail: str = ""
