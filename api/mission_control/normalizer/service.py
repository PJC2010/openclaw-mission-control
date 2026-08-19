"""Persistence + fan-out for normalized adapter output.

Concurrency model: one asyncio.Lock per agent serializes ingestion, so
per-agent `events.seq` assignment is race-free in-process (the API is a
single process by design — see the systemd unit). Dedupe rides on the
`events.dedupe_key` unique index via INSERT … ON CONFLICT DO NOTHING, so
replays after reconnect/backfill are harmless (§6.1/§6.2).
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import uuid
from dataclasses import dataclass
from typing import Any, AsyncIterator, Sequence

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..models import AdapterCursor, Agent, Event, Run, ScheduledTask
from ..models.enums import AgentRuntime, EventKind, RunStatus, RunTrigger
from .contract import AgentStatus, NormalizedEvent, RunUpsert, ScheduledTaskSnapshot

log = logging.getLogger("mission_control.normalizer")


@dataclass(frozen=True)
class EventRecord:
    """What subscribers (SSE) receive for each persisted event."""

    id: int
    agent_id: uuid.UUID
    run_id: uuid.UUID | None
    seq: int
    ts: datetime.datetime
    kind: str
    payload: dict[str, Any]

    def as_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "agent_id": str(self.agent_id),
            "run_id": str(self.run_id) if self.run_id else None,
            "seq": self.seq,
            "ts": self.ts.isoformat(),
            "kind": self.kind,
            "payload": self.payload,
        }


class EventBroadcast:
    """In-process pub/sub. Slow subscribers overflow and are marked stale;
    they resynchronize from the events table via their last seen id, so a
    dropped queue never means silent data loss."""

    _QUEUE_SIZE = 512

    def __init__(self) -> None:
        self._subscribers: dict[int, asyncio.Queue[EventRecord | None]] = {}
        self._next_key = 0

    def subscribe(self) -> tuple[int, asyncio.Queue[EventRecord | None]]:
        self._next_key += 1
        queue: asyncio.Queue[EventRecord | None] = asyncio.Queue(self._QUEUE_SIZE)
        self._subscribers[self._next_key] = queue
        return self._next_key, queue

    def unsubscribe(self, key: int) -> None:
        self._subscribers.pop(key, None)

    def publish(self, record: EventRecord) -> None:
        for key, queue in list(self._subscribers.items()):
            try:
                queue.put_nowait(record)
            except asyncio.QueueFull:
                # None = "you overflowed; resync from the DB".
                self._subscribers.pop(key, None)
                try:
                    queue.put_nowait(None)
                except asyncio.QueueFull:
                    pass


class Normalizer:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory
        self.broadcast = EventBroadcast()
        self._agent_locks: dict[uuid.UUID, asyncio.Lock] = {}
        self._seq: dict[uuid.UUID, int] = {}
        self._run_ids: dict[tuple[uuid.UUID, str], uuid.UUID] = {}
        self._agent_status: dict[uuid.UUID, str] = {}

    def _lock(self, agent_id: uuid.UUID) -> asyncio.Lock:
        return self._agent_locks.setdefault(agent_id, asyncio.Lock())

    # ── agent registry ───────────────────────────────────────────────────

    async def register_agent(
        self, runtime: AgentRuntime, instance_key: str, display_name: str
    ) -> uuid.UUID:
        """Get-or-create the agents row for a configured adapter (§6.4:
        'a new adapter class and a row in agents')."""
        async with self._sessions() as session:
            existing = await session.scalar(
                select(Agent).where(Agent.instance_key == instance_key)
            )
            if existing is not None:
                if existing.display_name != display_name:
                    existing.display_name = display_name
                    await session.commit()
                return existing.id
            agent = Agent(
                runtime=runtime,
                instance_key=instance_key,
                display_name=display_name,
                status="unknown",
            )
            session.add(agent)
            await session.commit()
            log.info("registered agent %s (%s / %s)", agent.id, runtime.value, instance_key)
            return agent.id

    async def set_agent_status(self, agent_id: uuid.UUID, status: AgentStatus) -> None:
        """Update liveness; a status *transition* also lands in the event
        stream as a lifecycle event so it is visible in the log tail."""
        async with self._lock(agent_id):
            previous = self._agent_status.get(agent_id)
            async with self._sessions() as session:
                if previous is None:
                    previous = await session.scalar(
                        select(Agent.status).where(Agent.id == agent_id)
                    )
                values: dict[str, Any] = {"status": status.status}
                if status.heartbeat_at is not None:
                    values["last_heartbeat_at"] = status.heartbeat_at
                await session.execute(
                    update(Agent).where(Agent.id == agent_id).values(**values)
                )
                await session.commit()
            self._agent_status[agent_id] = status.status
        if previous is not None and previous != status.status:
            now = status.heartbeat_at or datetime.datetime.now(datetime.timezone.utc)
            await self.ingest(
                agent_id,
                [
                    NormalizedEvent(
                        ts=now,
                        kind=EventKind.LIFECYCLE,
                        payload={
                            "transition": "agent_status",
                            "from": previous,
                            "to": status.status,
                            "detail": status.detail,
                        },
                    )
                ],
            )

    # ── event ingestion ──────────────────────────────────────────────────

    async def ingest(
        self, agent_id: uuid.UUID, events: Sequence[NormalizedEvent]
    ) -> int:
        """Persist a batch. Returns how many events were newly inserted."""
        if not events:
            return 0
        inserted_records: list[EventRecord] = []
        far_past = datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)
        ordered = sorted(
            events, key=lambda e: (e.ts or far_past, e.source_seq if e.source_seq is not None else 0)
        )
        async with self._lock(agent_id):
            async with self._sessions() as session:
                seq = await self._current_seq(session, agent_id)
                for event in ordered:
                    run_id = await self._resolve_run(session, agent_id, event)
                    candidate_seq = seq + 1
                    stmt = (
                        pg_insert(Event)
                        .values(
                            run_id=run_id,
                            agent_id=agent_id,
                            seq=candidate_seq,
                            ts=event.ts,
                            kind=event.kind,
                            payload=event.payload,
                            dedupe_key=event.dedupe_key,
                        )
                        .on_conflict_do_nothing(index_elements=["dedupe_key"])
                        .returning(Event.id)
                    )
                    event_id = await session.scalar(stmt)
                    if event_id is None:  # duplicate — dedupe did its job
                        continue
                    seq = candidate_seq
                    inserted_records.append(
                        EventRecord(
                            id=event_id,
                            agent_id=agent_id,
                            run_id=run_id,
                            seq=seq,
                            ts=event.ts,
                            kind=event.kind.value,
                            payload=event.payload,
                        )
                    )
                await session.commit()
                self._seq[agent_id] = seq
        for record in inserted_records:
            self.broadcast.publish(record)
        return len(inserted_records)

    async def _current_seq(self, session: AsyncSession, agent_id: uuid.UUID) -> int:
        cached = self._seq.get(agent_id)
        if cached is not None:
            return cached
        max_seq = await session.scalar(
            select(Event.seq).where(Event.agent_id == agent_id).order_by(Event.seq.desc()).limit(1)
        )
        seq = int(max_seq or 0)
        self._seq[agent_id] = seq
        return seq

    async def _resolve_run(
        self, session: AsyncSession, agent_id: uuid.UUID, event: NormalizedEvent
    ) -> uuid.UUID | None:
        """Apply the event's run hints; adapters never write runs (§6.3)."""
        external_id = event.external_run_id or (event.run.external_id if event.run else None)
        if external_id is None:
            return None
        cache_key = (agent_id, external_id)
        run_id = self._run_ids.get(cache_key)
        run: Run | None = None
        if run_id is None:
            run = await session.scalar(
                select(Run).where(Run.agent_id == agent_id, Run.external_id == external_id)
            )
            if run is None:
                hints = event.run or RunUpsert(external_id=external_id)
                run = Run(
                    agent_id=agent_id,
                    external_id=external_id,
                    trigger=hints.trigger or RunTrigger.MANUAL,
                    started_at=hints.started_at or event.ts,
                    status=hints.status or RunStatus.RUNNING,
                )
                session.add(run)
                await session.flush()
            self._run_ids[cache_key] = run.id
            run_id = run.id
        if event.run is not None:
            if run is None:
                run = await session.get(Run, run_id)
            if run is not None:
                await self._apply_run_hints(session, agent_id, run, event.run)
        return run_id

    async def _apply_run_hints(
        self, session: AsyncSession, agent_id: uuid.UUID, run: Run, hints: RunUpsert
    ) -> None:
        if hints.started_at is not None and hints.started_at < run.started_at:
            run.started_at = hints.started_at
        if hints.ended_at is not None:
            run.ended_at = hints.ended_at
        if hints.status is not None:
            run.status = hints.status
        if hints.trigger is not None:
            run.trigger = hints.trigger
        if hints.error_summary:
            run.error_summary = hints.error_summary
        if hints.token_input is not None:
            run.token_input = hints.token_input
        if hints.token_output is not None:
            run.token_output = hints.token_output
        if hints.cost_usd is not None:
            run.cost_usd = hints.cost_usd
        if hints.parent_external_id and run.parent_run_id is None:
            parent_key = (agent_id, hints.parent_external_id)
            parent_id = self._run_ids.get(parent_key)
            if parent_id is None:
                parent_id = await session.scalar(
                    select(Run.id).where(
                        Run.agent_id == agent_id,
                        Run.external_id == hints.parent_external_id,
                    )
                )
                if parent_id is not None:
                    self._run_ids[parent_key] = parent_id
            if parent_id is not None and parent_id != run.id:
                run.parent_run_id = parent_id

    # ── scheduled tasks (§6.2: definitions land in scheduled_tasks) ──────

    async def sync_scheduled_tasks(
        self,
        agent_id: uuid.UUID,
        snapshots: Sequence[ScheduledTaskSnapshot],
        disable_missing: bool = True,
    ) -> None:
        async with self._lock(agent_id):
            async with self._sessions() as session:
                existing = {
                    task.external_id: task
                    for task in (
                        await session.scalars(
                            select(ScheduledTask).where(ScheduledTask.agent_id == agent_id)
                        )
                    ).all()
                }
                seen: set[str] = set()
                for snap in snapshots:
                    seen.add(snap.external_id)
                    task = existing.get(snap.external_id)
                    if task is None:
                        task = ScheduledTask(agent_id=agent_id, external_id=snap.external_id, name=snap.name)
                        session.add(task)
                    task.name = snap.name
                    task.cron_expr = snap.cron_expr
                    task.timezone = snap.timezone
                    task.next_fire_at = snap.next_fire_at
                    task.last_outcome = snap.last_outcome
                    task.consecutive_failures = snap.consecutive_failures
                    task.enabled = snap.enabled
                    task.writeback_supported = snap.writeback_supported
                if disable_missing:
                    # A definition that vanished from the runtime is disabled,
                    # not deleted — history and objective links survive.
                    for external_id, task in existing.items():
                        if external_id not in seen and task.enabled:
                            task.enabled = False
                await session.commit()

    # ── adapter cursors (durable high-water marks) ───────────────────────

    async def get_cursor(self, agent_id: uuid.UUID, source: str) -> dict[str, Any]:
        async with self._sessions() as session:
            row = await session.get(AdapterCursor, (agent_id, source))
            return dict(row.cursor) if row is not None else {}

    async def set_cursor(self, agent_id: uuid.UUID, source: str, cursor: dict[str, Any]) -> None:
        async with self._sessions() as session:
            stmt = (
                pg_insert(AdapterCursor)
                .values(agent_id=agent_id, source=source, cursor=cursor)
                .on_conflict_do_update(
                    index_elements=["agent_id", "source"],
                    set_={"cursor": cursor, "updated_at": datetime.datetime.now(datetime.timezone.utc)},
                )
            )
            await session.execute(stmt)
            await session.commit()

    # ── read helpers for resume (SSE) ────────────────────────────────────

    async def events_after(
        self,
        after_id: int,
        limit: int = 500,
        agent_id: uuid.UUID | None = None,
        run_id: uuid.UUID | None = None,
    ) -> AsyncIterator[EventRecord]:
        async with self._sessions() as session:
            query = select(Event).where(Event.id > after_id).order_by(Event.id).limit(limit)
            if agent_id is not None:
                query = query.where(Event.agent_id == agent_id)
            if run_id is not None:
                query = query.where(Event.run_id == run_id)
            for event in (await session.scalars(query)).all():
                yield EventRecord(
                    id=event.id,
                    agent_id=event.agent_id,
                    run_id=event.run_id,
                    seq=event.seq,
                    ts=event.ts,
                    kind=event.kind.value if hasattr(event.kind, "value") else str(event.kind),
                    payload=event.payload,
                )
