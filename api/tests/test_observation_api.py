"""Observation API: agents roster, run list/detail, SSE stream with resume."""

from __future__ import annotations

import asyncio
import datetime
import json

import httpx
import pytest
import uvicorn

from mission_control.app import create_app
from mission_control.models.enums import EventKind, RunStatus, RunTrigger
from mission_control.normalizer import AgentStatus, NormalizedEvent, RunUpsert

from .conftest import make_settings, ok_resolver, serve_headers

pytestmark = pytest.mark.anyio

NOW = datetime.datetime(2026, 8, 19, 12, 0, tzinfo=datetime.timezone.utc)


@pytest.fixture
async def app(test_db_url):
    application = create_app(
        settings=make_settings(database_url=test_db_url), whois_resolver=ok_resolver
    )
    # Clean slate per test (the app owns its engine; reuse it).
    from sqlalchemy import text

    async with application.state.db_engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE notifications, audit_log, outcomes, approval_policies, "
                "approvals, adapter_cursors, scheduled_tasks, events, runs, agents, "
                "objectives RESTART IDENTITY CASCADE"
            )
        )
    yield application
    await application.state.db_engine.dispose()


@pytest.fixture
async def seeded(app):
    """One agent, two runs (parent/child), a few events."""
    from mission_control.models.enums import AgentRuntime

    normalizer = app.state.normalizer
    agent_id = await normalizer.register_agent(AgentRuntime.HERMES, "hermes:t", "Hermes")
    await normalizer.set_agent_status(agent_id, AgentStatus(status="up", heartbeat_at=NOW))
    events = [
        NormalizedEvent(
            ts=NOW,
            kind=EventKind.LIFECYCLE,
            payload={"transition": "run_started"},
            external_run_id="r-parent",
            dedupe_key="t:1",
            run=RunUpsert(
                external_id="r-parent", started_at=NOW,
                status=RunStatus.RUNNING, trigger=RunTrigger.MESSAGE,
            ),
        ),
        NormalizedEvent(
            ts=NOW + datetime.timedelta(seconds=1),
            kind=EventKind.TOOL_CALL,
            payload={"tool_name": "web_search"},
            external_run_id="r-parent",
            dedupe_key="t:2",
        ),
        NormalizedEvent(
            ts=NOW + datetime.timedelta(seconds=2),
            kind=EventKind.LIFECYCLE,
            payload={"transition": "run_started"},
            external_run_id="r-child",
            dedupe_key="t:3",
            run=RunUpsert(
                external_id="r-child", started_at=NOW + datetime.timedelta(seconds=2),
                parent_external_id="r-parent", trigger=RunTrigger.SUBAGENT,
            ),
        ),
    ]
    await normalizer.ingest(agent_id, events)
    return agent_id


def client_for_app(app) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 41234))
    return httpx.AsyncClient(transport=transport, base_url="http://mc.test")


async def test_agents_and_runs_endpoints(app, seeded):
    async with client_for_app(app) as client:
        agents = (await client.get("/v1/agents", headers=serve_headers())).json()
        assert agents["agents"][0]["status"] == "up"
        assert agents["agents"][0]["runtime"] == "hermes"

        runs = (await client.get("/v1/runs", headers=serve_headers())).json()
        assert len(runs["runs"]) == 2
        by_ext = {r["external_id"]: r for r in runs["runs"]}
        assert by_ext["r-child"]["parent_run_id"] == by_ext["r-parent"]["id"]

        detail = (
            await client.get(f"/v1/runs/{by_ext['r-parent']['id']}", headers=serve_headers())
        ).json()
        assert detail["agent"]["display_name"] == "Hermes"
        assert [c["external_id"] for c in detail["children"]] == ["r-child"]
        assert detail["event_count"] == 2

        events = (
            await client.get(
                f"/v1/runs/{by_ext['r-parent']['id']}/events", headers=serve_headers()
            )
        ).json()
        assert [e["kind"] for e in events["events"]] == ["lifecycle", "tool_call"]

        missing = await client.get(
            "/v1/runs/00000000-0000-0000-0000-000000000000", headers=serve_headers()
        )
        assert missing.status_code == 404


async def test_observation_endpoints_require_identity(app, seeded):
    async with client_for_app(app) as client:
        for path in ("/v1/agents", "/v1/runs", "/v1/stream", "/v1/system/health"):
            response = await client.get(path)
            assert response.status_code == 401, path


async def test_system_health_reports_db(app):
    async with client_for_app(app) as client:
        body = (await client.get("/v1/system/health", headers=serve_headers())).json()
    assert body["database"]["ok"] is True
    assert body["adapters"] == {}


async def test_sse_stream_live_and_resume(app, seeded):
    """Real server, real EventSource semantics: live events arrive; a
    reconnect with Last-Event-ID replays what was missed (§13)."""
    # proxy_headers=False mirrors production (__main__.py): uvicorn must not
    # rewrite the peer address from X-Forwarded-For — the middleware needs
    # the true socket peer for its loopback check.
    config = uvicorn.Config(
        app, host="127.0.0.1", port=0, log_level="warning", proxy_headers=False
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        while not server.started:
            await asyncio.sleep(0.02)
        port = server.servers[0].sockets[0].getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        normalizer = app.state.normalizer
        agent_id = seeded

        async with httpx.AsyncClient(timeout=10) as client:
            received: list[dict] = []
            ids: list[int] = []

            async def read_stream(headers, count):
                async with client.stream("GET", f"{base}/v1/stream", headers=headers) as resp:
                    assert resp.status_code == 200
                    assert resp.headers["content-type"].startswith("text/event-stream")
                    current_id = None
                    async for line in resp.aiter_lines():
                        if line.startswith("id: "):
                            current_id = int(line[4:])
                        elif line.startswith("data: "):
                            received.append(json.loads(line[6:]))
                            ids.append(current_id)
                            if len(received) >= count:
                                return

            reader = asyncio.create_task(read_stream(serve_headers(), 1))
            await asyncio.sleep(0.3)  # let the subscription attach
            await normalizer.ingest(
                agent_id,
                [
                    NormalizedEvent(
                        ts=NOW + datetime.timedelta(seconds=10),
                        kind=EventKind.LOG,
                        payload={"line": "live-1"},
                        dedupe_key="t:live1",
                    )
                ],
            )
            await asyncio.wait_for(reader, timeout=5)
            assert received[-1]["payload"] == {"line": "live-1"}
            last_id = ids[-1]

            # Events land while "disconnected"…
            await normalizer.ingest(
                agent_id,
                [
                    NormalizedEvent(
                        ts=NOW + datetime.timedelta(seconds=11),
                        kind=EventKind.LOG,
                        payload={"line": "missed"},
                        dedupe_key="t:missed",
                    )
                ],
            )
            # …and replay on resume via Last-Event-ID.
            received.clear()
            await asyncio.wait_for(
                read_stream(serve_headers() | {"Last-Event-ID": str(last_id)}, 1), timeout=5
            )
            assert received[0]["payload"] == {"line": "missed"}
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=5)
