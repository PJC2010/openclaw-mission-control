"""OpenClaw client + adapter against a scripted protocol-v4 fake gateway
(§6.1: live ingest, dedupe, reconnect with backoff, subagent linkage)."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from mission_control.adapters.openclaw import OpenClawAdapter
from mission_control.adapters.openclaw import adapter as adapter_module
from mission_control.adapters.openclaw.client import GatewayError, OpenClawClient
from mission_control.models import Agent, Event, Run
from mission_control.models.enums import AgentRuntime, EventKind, RunStatus, RunTrigger

from .conftest import make_settings
from .fake_gateway import FakeGateway, audit_row

pytestmark = pytest.mark.anyio


async def noop_event(_event) -> None:
    return None


@pytest.fixture
async def gateway():
    gw = FakeGateway()
    await gw.start()
    yield gw
    await gw.stop()


@pytest.fixture
async def oc_agent_id(normalizer):
    return await normalizer.register_agent(AgentRuntime.OPENCLAW, "openclaw:test", "OpenClaw Test")


# ── client-level ─────────────────────────────────────────────────────────


async def test_handshake_and_rpc(gateway):
    gateway.methods["sessions.list"] = lambda params: {"sessions": [{"key": "s1"}]}
    url = f"ws://127.0.0.1:{gateway.port}"
    client = OpenClawClient(url, "test-token", "0.0-test", noop_event)
    hello = await client.connect()
    assert hello["server"]["version"] == "2026.7.1-2"
    assert gateway.connect_params[0]["role"] == "operator"
    assert gateway.connect_params[0]["scopes"] == ["operator.read"]
    result = await client.call("sessions.list", {})
    assert result["sessions"][0]["key"] == "s1"
    with pytest.raises(GatewayError, match="UNKNOWN_METHOD"):
        await client.call("nope", {})
    await client.close()


async def test_bad_token_rejected(gateway):
    client = OpenClawClient(
        f"ws://127.0.0.1:{gateway.port}", "wrong", "0.0-test", noop_event
    )
    with pytest.raises(GatewayError, match="UNAUTHORIZED"):
        await client.connect()


# ── adapter-level ────────────────────────────────────────────────────────


def scripted_audit(gateway: FakeGateway, rows: list[dict]) -> None:
    def handler(params: dict) -> dict:
        after = params.get("after")
        selected = [
            r for r in rows if after is None or int(r["occurredAt"]) >= int(after)
        ]
        return {"events": sorted(selected, key=lambda r: -r["sequence"])}

    gateway.methods["audit.activity.list"] = handler


async def start_adapter(normalizer, oc_agent_id, gateway, monkeypatch) -> OpenClawAdapter:
    monkeypatch.setattr(adapter_module, "BACKOFF_BASE_S", 0.05)
    monkeypatch.setattr(adapter_module, "BACKOFF_CAP_S", 0.2)
    settings = make_settings(
        openclaw_url=f"ws://127.0.0.1:{gateway.port}",
        openclaw_token="test-token",
        openclaw_poll_interval_s=0.05,
    )
    adapter = OpenClawAdapter(oc_agent_id, settings, normalizer)
    await adapter.start()
    return adapter


async def wait_for(predicate, timeout=5.0, interval=0.05):
    async with asyncio.timeout(timeout):
        while True:
            result = await predicate()
            if result:
                return result
            await asyncio.sleep(interval)


async def test_adapter_ingests_audit_ledger(normalizer, oc_agent_id, gateway, monkeypatch):
    gateway.methods["sessions.list"] = lambda params: {
        "sessions": [
            {"key": "sess-A"},
            {"key": "sess-B", "spawnedBy": "sess-A"},
        ]
    }
    scripted_audit(
        gateway,
        [
            audit_row(1, status="started", run_id="run-1", session_key="sess-A"),
            audit_row(
                2, event_type="tool_action", status="started",
                run_id="run-1", session_key="sess-A", toolName="browser", toolCallId="t1",
            ),
            audit_row(
                3, event_type="tool_action", status="succeeded",
                run_id="run-1", session_key="sess-A", toolName="browser", toolCallId="t1",
            ),
            # subagent run in the spawned session
            audit_row(4, status="started", run_id="run-sub", session_key="sess-B"),
            audit_row(5, status="succeeded", run_id="run-sub", session_key="sess-B"),
            audit_row(6, status="failed", run_id="run-1", session_key="sess-A", errorCode="run_failed"),
            audit_row(
                7, event_type="inbound_message", status="succeeded", run_id="run-1",
                session_key=None, direction="inbound", channel="telegram",
                conversationKind="direct", outcome="completed",
            ),
        ],
    )

    adapter = await start_adapter(normalizer, oc_agent_id, gateway, monkeypatch)
    try:
        async def runs_ready():
            async with normalizer._sessions() as session:  # noqa: SLF001
                runs = {r.external_id: r for r in (await session.scalars(select(Run))).all()}
            return runs if len(runs) >= 2 and runs.get("run-1", None) and runs["run-1"].status is RunStatus.FAILED else None

        runs = await wait_for(runs_ready)
        assert runs["run-1"].status is RunStatus.FAILED
        assert runs["run-1"].error_summary == "run_failed"
        # inbound message carrying the run id classified the trigger
        assert runs["run-1"].trigger is RunTrigger.MESSAGE
        # subagent linkage via sessions.list lineage (§6.1)
        assert runs["run-sub"].parent_run_id == runs["run-1"].id
        assert runs["run-sub"].trigger is RunTrigger.SUBAGENT

        async with normalizer._sessions() as session:  # noqa: SLF001
            events = (await session.scalars(select(Event))).all()
            agent = await session.get(Agent, oc_agent_id)
        kinds = [e.kind for e in events]
        assert EventKind.TOOL_CALL in kinds and EventKind.TOOL_RESULT in kinds
        assert EventKind.MESSAGE in kinds
        assert agent.status == "up"

        # idempotent under continued polling (dedupe on eventId)
        await asyncio.sleep(0.2)
        async with normalizer._sessions() as session:  # noqa: SLF001
            count_after = len((await session.scalars(select(Event))).all())
        assert count_after == len(events)
    finally:
        await adapter.stop()


async def test_adapter_reconnects_after_drop(normalizer, oc_agent_id, gateway, monkeypatch):
    gateway.methods["sessions.list"] = lambda params: {"sessions": []}
    scripted_audit(gateway, [audit_row(1, status="started", run_id="r1")])
    adapter = await start_adapter(normalizer, oc_agent_id, gateway, monkeypatch)
    try:
        async def agent_up():
            async with normalizer._sessions() as session:  # noqa: SLF001
                agent = await session.get(Agent, oc_agent_id)
            return agent.status == "up" or None

        await wait_for(agent_up)
        await gateway.drop_all()

        async def agent_down():
            async with normalizer._sessions() as session:  # noqa: SLF001
                agent = await session.get(Agent, oc_agent_id)
            return agent.status == "down" or None

        await wait_for(agent_down)

        # New rows appear while disconnected; reconnect must backfill them.
        scripted_audit(
            gateway,
            [
                audit_row(1, status="started", run_id="r1"),
                audit_row(2, status="succeeded", run_id="r1"),
            ],
        )
        await wait_for(agent_up)

        async def run_finished():
            async with normalizer._sessions() as session:  # noqa: SLF001
                run = await session.scalar(select(Run).where(Run.external_id == "r1"))
            return run if run is not None and run.status is RunStatus.SUCCEEDED else None

        await wait_for(run_finished)
    finally:
        await adapter.stop()
