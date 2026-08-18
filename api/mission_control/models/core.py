"""Core observation tables (§5.1–§5.5): objectives, agents, runs, events,
scheduled_tasks."""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin
from .enums import (
    AGENT_RUNTIME,
    EVENT_KIND,
    OBJECTIVE_SOURCE,
    OBJECTIVE_STATUS,
    OBJECTIVE_TRACK,
    RUN_STATUS,
    RUN_TRIGGER,
    AgentRuntime,
    EventKind,
    ObjectiveSource,
    ObjectiveStatus,
    RunStatus,
    RunTrigger,
    Track,
)

UUID_DEFAULT = text("gen_random_uuid()")


class Objective(TimestampMixin, Base):
    """§5.1 — the alignment spine. Soft cap of 5 active is UI-enforced only."""

    __tablename__ = "objectives"
    __table_args__ = (CheckConstraint("priority BETWEEN 1 AND 5", name="priority_range"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=UUID_DEFAULT)
    title: Mapped[str] = mapped_column(Text)
    track: Mapped[Track] = mapped_column(OBJECTIVE_TRACK)
    description: Mapped[str] = mapped_column(Text, default="", server_default="")
    horizon_start: Mapped[datetime.date | None] = mapped_column(Date)
    horizon_end: Mapped[datetime.date | None] = mapped_column(Date)
    status: Mapped[ObjectiveStatus] = mapped_column(
        OBJECTIVE_STATUS, default=ObjectiveStatus.ACTIVE, server_default="active"
    )
    priority: Mapped[int] = mapped_column(Integer, default=3, server_default="3")


class Agent(TimestampMixin, Base):
    """§5.2 — registered runtime instances."""

    __tablename__ = "agents"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=UUID_DEFAULT)
    runtime: Mapped[AgentRuntime] = mapped_column(AGENT_RUNTIME)
    display_name: Mapped[str] = mapped_column(Text)
    instance_key: Mapped[str] = mapped_column(Text, unique=True)
    # Free text ('up' | 'down' | 'unknown' | 'paused' expected): the spec does
    # not enumerate agent states, so this stays unconstrained until the
    # adapters (Phase 1) settle the real lifecycle. See docs/decisions.md D5.
    status: Mapped[str] = mapped_column(Text, default="unknown", server_default="unknown")
    last_heartbeat_at: Mapped[datetime.datetime | None]
    config_snapshot: Mapped[dict[str, Any] | None]


class Run(Base):
    """§5.3 — one agent session or task execution."""

    __tablename__ = "runs"
    __table_args__ = (
        UniqueConstraint("agent_id", "external_id"),
        Index("ix_runs_agent_started", "agent_id", text("started_at DESC")),
        Index("ix_runs_objective_started", "objective_id", text("started_at DESC")),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=UUID_DEFAULT)
    agent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agents.id", ondelete="RESTRICT"))
    external_id: Mapped[str] = mapped_column(Text)
    objective_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("objectives.id", ondelete="RESTRICT")
    )
    objective_source: Mapped[ObjectiveSource] = mapped_column(
        OBJECTIVE_SOURCE, default=ObjectiveSource.NONE, server_default="none"
    )
    trigger: Mapped[RunTrigger] = mapped_column(RUN_TRIGGER)
    parent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("runs.id", ondelete="RESTRICT")
    )
    started_at: Mapped[datetime.datetime]
    ended_at: Mapped[datetime.datetime | None]
    status: Mapped[RunStatus] = mapped_column(
        RUN_STATUS, default=RunStatus.RUNNING, server_default="running"
    )
    error_summary: Mapped[str | None] = mapped_column(Text)
    token_input: Mapped[int | None] = mapped_column(BigInteger)
    token_output: Mapped[int | None] = mapped_column(BigInteger)
    cost_usd: Mapped[float | None] = mapped_column(Numeric(12, 6))


class Event(Base):
    """§5.4 — append-only normalized event stream.

    Largest table by an order of magnitude. Declarative partitioning is
    deliberately deferred (docs/decisions.md D4): a global unique
    `dedupe_key` — load-bearing for adapter idempotency — cannot coexist
    with PG partition keys, so Phase 0 ships BRIN-on-ts + the §18.4
    retention roll-up instead.
    """

    __tablename__ = "events"
    __table_args__ = (
        Index("ix_events_agent_seq", "agent_id", "seq"),
        Index("ix_events_run_id_id", "run_id", "id"),
        Index("ix_events_ts_brin", "ts", postgresql_using="brin"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("runs.id", ondelete="SET NULL"))
    agent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agents.id", ondelete="RESTRICT"))
    # Monotonic per agent; SSE resume cursor (§13). Uniqueness is NOT
    # enforced — adapter restarts may replay; dedupe_key handles identity.
    seq: Mapped[int] = mapped_column(BigInteger)
    ts: Mapped[datetime.datetime]
    kind: Mapped[EventKind] = mapped_column(EVENT_KIND)
    payload: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    dedupe_key: Mapped[str | None] = mapped_column(Text, unique=True)


class ScheduledTask(TimestampMixin, Base):
    """§5.5 — unified agenda across runtimes."""

    __tablename__ = "scheduled_tasks"
    __table_args__ = (UniqueConstraint("agent_id", "external_id"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=UUID_DEFAULT)
    agent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agents.id", ondelete="RESTRICT"))
    external_id: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    # Nullable: not every runtime task is cron-shaped (one-shots, intervals);
    # next_fire_at carries the operative truth for the agenda view.
    cron_expr: Mapped[str | None] = mapped_column(Text)
    timezone: Mapped[str] = mapped_column(Text, default="UTC", server_default="UTC")
    next_fire_at: Mapped[datetime.datetime | None]
    last_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("runs.id", ondelete="SET NULL")
    )
    last_outcome: Mapped[str | None] = mapped_column(Text)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    objective_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("objectives.id", ondelete="RESTRICT")
    )
    staleness_flag: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    writeback_supported: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
