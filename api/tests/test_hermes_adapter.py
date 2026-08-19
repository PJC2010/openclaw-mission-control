"""Hermes adapter against an on-disk fixture replicating the verified
0.20.4 layout (§6.2)."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from mission_control.adapters.hermes import HermesAdapter
from mission_control.models import Agent, Event, Run, ScheduledTask
from mission_control.models.enums import EventKind, RunStatus, RunTrigger

from .conftest import make_settings
from .hermes_fixture import build_home

pytestmark = pytest.mark.anyio


@pytest.fixture
def hermes_home(tmp_path):
    return build_home(tmp_path)


async def make_adapter(normalizer, hermes_agent_id, home, **overrides):
    settings = make_settings(hermes_home=str(home), **overrides)
    adapter = HermesAdapter(hermes_agent_id, settings, normalizer)
    await adapter.load_cursors()
    return adapter


async def test_full_poll_ingests_runs_events_and_schedule(
    normalizer, hermes_agent_id, hermes_home
):
    adapter = await make_adapter(normalizer, hermes_agent_id, hermes_home)
    inserted = await adapter.poll_once()
    assert inserted > 0

    async with normalizer._sessions() as session:  # noqa: SLF001
        runs = {r.external_id: r for r in (await session.scalars(select(Run))).all()}
        events = (await session.scalars(select(Event))).all()
        tasks = {t.external_id: t for t in (await session.scalars(select(ScheduledTask))).all()}
        agent = await session.get(Agent, hermes_agent_id)

    # sessions → runs
    assert runs["s-done"].status is RunStatus.SUCCEEDED
    assert runs["s-done"].token_input == 1200
    assert float(runs["s-done"].cost_usd) == pytest.approx(0.031)
    assert runs["s-live"].status is RunStatus.RUNNING
    assert runs["s-live"].trigger is RunTrigger.SCHEDULE
    # subagent linkage (§6.1 concern, honored by the hermes source too)
    assert runs["s-sub"].parent_run_id == runs["s-live"].id
    assert runs["s-sub"].trigger is RunTrigger.SUBAGENT
    # cron executions → runs in their own namespace
    assert runs["cron:e-ok"].status is RunStatus.SUCCEEDED
    assert runs["cron:e-run"].status is RunStatus.RUNNING

    # messages → typed events
    kinds = {e.kind for e in events}
    assert {EventKind.TOOL_CALL, EventKind.TOOL_RESULT, EventKind.MESSAGE, EventKind.LIFECYCLE} <= kinds
    tool_calls = [e for e in events if e.kind is EventKind.TOOL_CALL]
    assert tool_calls[0].payload["tool_calls"][0]["name"] == "web_search"

    # jobs.json → scheduled_tasks (§6.2)
    assert tasks["job-digest"].cron_expr == "0 6 * * *"
    assert tasks["job-poll"].cron_expr is None  # interval job — next_fire_at carries truth
    assert tasks["job-poll"].consecutive_failures == 2
    assert tasks["job-digest"].writeback_supported is False

    # heartbeat file is fresh → up
    assert agent.status == "up"


async def test_repolls_are_idempotent_and_incremental(
    normalizer, hermes_agent_id, hermes_home
):
    adapter = await make_adapter(normalizer, hermes_agent_id, hermes_home)
    first = await adapter.poll_once()
    second = await adapter.poll_once()
    assert first > 0
    assert second == 0  # cursors + dedupe: nothing new, nothing duplicated

    # A fresh adapter instance (process restart) resumes from durable cursors.
    restarted = await make_adapter(normalizer, hermes_agent_id, hermes_home)
    assert await restarted.poll_once() == 0


async def test_schema_version_mismatch_fails_loudly(normalizer, hermes_agent_id, tmp_path):
    home = build_home(tmp_path, schema_version=27)
    adapter = await make_adapter(normalizer, hermes_agent_id, home)
    from mission_control.adapters.hermes import HermesSchemaError

    with pytest.raises(HermesSchemaError, match="schema_version=27"):
        await adapter.poll_once()
    async with normalizer._sessions() as session:  # noqa: SLF001
        events = (await session.scalars(select(Event))).all()
        runs = (await session.scalars(select(Run))).all()
    # §6.2: no schema-derived data ingested. (The agent-liveness lifecycle
    # event is file-mtime-derived and legitimately present — the daemon is
    # up even though our adapter refuses its schema.)
    assert runs == []
    assert [e for e in events if e.kind is not EventKind.LIFECYCLE] == []
    assert all(e.payload.get("transition") == "agent_status" for e in events)


async def test_stale_heartbeat_marks_agent_down(normalizer, hermes_agent_id, tmp_path):
    import os
    import time

    home = build_home(tmp_path)
    old = time.time() - 3600
    for name in ("cron/ticker_heartbeat", "state.db", "state.db-wal"):
        path = home / name
        if path.exists():
            os.utime(path, (old, old))
    adapter = await make_adapter(normalizer, hermes_agent_id, home)
    await adapter.poll_once()
    async with normalizer._sessions() as session:  # noqa: SLF001
        agent = await session.get(Agent, hermes_agent_id)
    assert agent.status == "down"
