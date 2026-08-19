"""Normalizer persistence semantics against a real Postgres (§6.3)."""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy import func, select

from mission_control.models import Event, Run, ScheduledTask
from mission_control.models.enums import EventKind, RunStatus, RunTrigger
from mission_control.normalizer import (
    AgentStatus,
    NormalizedEvent,
    RunUpsert,
    ScheduledTaskSnapshot,
)

pytestmark = pytest.mark.anyio

NOW = datetime.datetime(2026, 8, 19, 12, 0, tzinfo=datetime.timezone.utc)


def ev(key: str | None, seq_hint: int, run: RunUpsert | None = None, **payload) -> NormalizedEvent:
    return NormalizedEvent(
        ts=NOW + datetime.timedelta(seconds=seq_hint),
        kind=EventKind.LOG,
        payload=payload or {"n": seq_hint},
        dedupe_key=key,
        source_seq=seq_hint,
        external_run_id=run.external_id if run else None,
        run=run,
    )


async def test_ingest_assigns_dense_per_agent_seq(normalizer, hermes_agent_id):
    inserted = await normalizer.ingest(
        hermes_agent_id, [ev("a", 1), ev("b", 2), ev(None, 3)]
    )
    assert inserted == 3
    async with normalizer._sessions() as session:  # noqa: SLF001 — asserting storage
        seqs = (await session.scalars(select(Event.seq).order_by(Event.seq))).all()
    assert seqs == [1, 2, 3]


async def test_dedupe_key_makes_replay_idempotent(normalizer, hermes_agent_id):
    await normalizer.ingest(hermes_agent_id, [ev("same", 1)])
    inserted = await normalizer.ingest(hermes_agent_id, [ev("same", 1), ev("new", 2)])
    assert inserted == 1
    async with normalizer._sessions() as session:  # noqa: SLF001
        count = await session.scalar(select(func.count()).select_from(Event))
    assert count == 2


async def test_run_lifecycle_upsert(normalizer, hermes_agent_id):
    start = RunUpsert(
        external_id="sess-1", started_at=NOW, status=RunStatus.RUNNING, trigger=RunTrigger.MESSAGE
    )
    end = RunUpsert(
        external_id="sess-1",
        ended_at=NOW + datetime.timedelta(minutes=5),
        status=RunStatus.SUCCEEDED,
        token_input=100,
        token_output=200,
        cost_usd=0.42,
    )
    await normalizer.ingest(hermes_agent_id, [ev("s", 1, run=start)])
    await normalizer.ingest(hermes_agent_id, [ev("e", 2, run=end)])
    async with normalizer._sessions() as session:  # noqa: SLF001
        run = await session.scalar(select(Run).where(Run.external_id == "sess-1"))
    assert run.status is RunStatus.SUCCEEDED
    assert run.trigger is RunTrigger.MESSAGE  # preserved from the start hint
    assert run.token_output == 200
    assert float(run.cost_usd) == pytest.approx(0.42)
    assert run.ended_at is not None


async def test_deduped_event_still_applies_run_hints(normalizer, hermes_agent_id):
    """Re-polled source rows re-emit the same event (deduped) but must still
    refresh run fields — sessions mutate in place."""
    first = RunUpsert(external_id="sess-2", started_at=NOW, status=RunStatus.RUNNING)
    await normalizer.ingest(hermes_agent_id, [ev("x", 1, run=first)])
    updated = RunUpsert(external_id="sess-2", status=RunStatus.FAILED, error_summary="boom")
    inserted = await normalizer.ingest(hermes_agent_id, [ev("x", 1, run=updated)])
    assert inserted == 0  # event deduped
    async with normalizer._sessions() as session:  # noqa: SLF001
        run = await session.scalar(select(Run).where(Run.external_id == "sess-2"))
    assert run.status is RunStatus.FAILED
    assert run.error_summary == "boom"


async def test_parent_run_linkage(normalizer, hermes_agent_id):
    parent = RunUpsert(external_id="parent", started_at=NOW)
    child = RunUpsert(
        external_id="child",
        started_at=NOW,
        parent_external_id="parent",
        trigger=RunTrigger.SUBAGENT,
    )
    await normalizer.ingest(hermes_agent_id, [ev("p", 1, run=parent), ev("c", 2, run=child)])
    async with normalizer._sessions() as session:  # noqa: SLF001
        parent_row = await session.scalar(select(Run).where(Run.external_id == "parent"))
        child_row = await session.scalar(select(Run).where(Run.external_id == "child"))
    assert child_row.parent_run_id == parent_row.id
    assert child_row.trigger is RunTrigger.SUBAGENT


async def test_agent_status_transition_emits_lifecycle_event(normalizer, hermes_agent_id):
    await normalizer.set_agent_status(hermes_agent_id, AgentStatus(status="up", heartbeat_at=NOW))
    await normalizer.set_agent_status(hermes_agent_id, AgentStatus(status="down", detail="gone"))
    # up (from unknown) + down (from up) = two transitions
    async with normalizer._sessions() as session:  # noqa: SLF001
        events = (
            await session.scalars(select(Event).where(Event.kind == EventKind.LIFECYCLE))
        ).all()
    transitions = [e.payload.get("to") for e in events]
    assert transitions == ["up", "down"]


async def test_scheduled_task_sync_disables_missing(normalizer, hermes_agent_id):
    snap = ScheduledTaskSnapshot(external_id="job-1", name="daily", cron_expr="0 9 * * *")
    await normalizer.sync_scheduled_tasks(hermes_agent_id, [snap])
    await normalizer.sync_scheduled_tasks(
        hermes_agent_id, [ScheduledTaskSnapshot(external_id="job-2", name="weekly")]
    )
    async with normalizer._sessions() as session:  # noqa: SLF001
        tasks = {t.external_id: t for t in (await session.scalars(select(ScheduledTask))).all()}
    assert tasks["job-1"].enabled is False  # vanished from the runtime → disabled, kept
    assert tasks["job-2"].enabled is True


async def test_cursor_roundtrip(normalizer, hermes_agent_id):
    assert await normalizer.get_cursor(hermes_agent_id, "hermes.messages") == {}
    await normalizer.set_cursor(hermes_agent_id, "hermes.messages", {"rowid": 42})
    await normalizer.set_cursor(hermes_agent_id, "hermes.messages", {"rowid": 99})
    assert await normalizer.get_cursor(hermes_agent_id, "hermes.messages") == {"rowid": 99}


async def test_broadcast_publishes_inserted_events(normalizer, hermes_agent_id):
    key, queue = normalizer.broadcast.subscribe()
    try:
        await normalizer.ingest(hermes_agent_id, [ev("bc", 1)])
        record = queue.get_nowait()
        assert record.payload == {"n": 1}
        assert record.seq == 1
    finally:
        normalizer.broadcast.unsubscribe(key)
