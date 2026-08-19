"""§17 acceptance tests — the list that must pass before Phase 2 is done.

Numbered against the spec. Tests 6 and 7 live in test_identity.py (Phase
0); test 4 (wrapper fails closed when Mission Control is unreachable) is
in test_agent_wrappers.py; tests 10 and 13 are UI behaviours covered in
test_approvals_ui.py and the runbook's on-device checklist.
"""

from __future__ import annotations

import asyncio
import datetime
import uuid

import httpx
import pytest
from sqlalchemy import select

from mission_control.app import create_app
from mission_control.auth.service_token import (
    SCOPE_CREATE,
    SCOPE_POLL,
    generate_token,
)
from mission_control.models import Agent, Approval, AuditLog, ServiceToken
from mission_control.models.enums import AgentRuntime, ApprovalState, DecidedVia, RiskLevel

from .conftest import make_settings, ok_resolver, security_text, serve_headers, OPERATOR_LOGIN

pytestmark = pytest.mark.anyio


# ── fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
async def app(test_db_url):
    from sqlalchemy import text

    application = create_app(
        settings=make_settings(database_url=test_db_url, workspace_allowlist="/workspace"),
        whois_resolver=ok_resolver,
    )
    async with application.state.db_engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE notifications, audit_log, outcomes, approval_policies, "
                "approvals, adapter_cursors, scheduled_tasks, events, runs, "
                "service_tokens, system_flags, agents, objectives "
                "RESTART IDENTITY CASCADE"
            )
        )
    yield application
    await application.state.db_engine.dispose()


@pytest.fixture
async def agent_id(app) -> uuid.UUID:
    return await app.state.normalizer.register_agent(
        AgentRuntime.HERMES, "hermes:approvals-test", "Hermes"
    )


@pytest.fixture
async def token(app, agent_id) -> str:
    """A bound agent token with the only two mintable scopes."""
    plaintext, prefix, digest = generate_token()
    async with app.state.db_sessions() as session:
        session.add(
            ServiceToken(
                name="test-agent",
                token_hash=digest,
                token_prefix=prefix,
                scopes=[SCOPE_CREATE, SCOPE_POLL],
                agent_id=agent_id,
                created_by="test",
            )
        )
        await session.commit()
    return plaintext


def client(app, peer: str = "127.0.0.1") -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, client=(peer, 51000))
    return httpx.AsyncClient(transport=transport, base_url="http://mc.test")


def agent_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def create_request(
    http: httpx.AsyncClient, token: str, *, key: str, tool: str = "write_file", args=None
):
    return await http.post(
        "/v1/approvals",
        headers=agent_headers(token),
        json={
            "idempotency_key": key,
            "tool_name": tool,
            "tool_args": args if args is not None else {"path": "/workspace/out.txt"},
            "rationale": "test",
        },
    )


async def audit_actions(app) -> list[tuple[str, str]]:
    async with app.state.db_sessions() as session:
        rows = (await session.scalars(select(AuditLog).order_by(AuditLog.id))).all()
    return [(row.action, row.actor) for row in rows]


# ── §17.1 approve end to end ─────────────────────────────────────────────


async def test_01_approve_flow_records_tailnet_login(app, token, agent_id):
    async with client(app) as http:
        created = await create_request(http, token, key="key-approve-001")
        assert created.status_code == 201
        approval_id = created.json()["approval_id"]
        assert created.json()["state"] == "pending"

        # The agent blocks; the operator decides from the dashboard.
        poll = asyncio.create_task(
            http.get(
                f"/v1/approvals/{approval_id}/decision?wait=5", headers=agent_headers(token)
            )
        )
        await asyncio.sleep(0.2)
        decided = await http.post(
            f"/v1/approvals/{approval_id}/decision",
            headers=serve_headers(),
            json={"approve": True, "note": "looks fine"},
        )
        assert decided.status_code == 200
        assert decided.json()["state"] == "approved"

        outcome = (await poll).json()
        assert outcome["state"] == "approved"
        assert outcome["allowed"] is True

    # §12 S6 — the decision is attributed to the resolved tailnet identity.
    actions = await audit_actions(app)
    assert ("approval.approved", OPERATOR_LOGIN) in actions


# ── §17.2 deny ───────────────────────────────────────────────────────────


async def test_02_deny_flow(app, token):
    async with client(app) as http:
        created = await create_request(http, token, key="key-deny-002")
        approval_id = created.json()["approval_id"]
        await http.post(
            f"/v1/approvals/{approval_id}/decision",
            headers=serve_headers(),
            json={"approve": False, "note": "no"},
        )
        outcome = (
            await http.get(
                f"/v1/approvals/{approval_id}/decision", headers=agent_headers(token)
            )
        ).json()
    assert outcome["state"] == "denied"
    assert outcome["allowed"] is False


# ── §17.3 TTL expiry is distinct from denial ─────────────────────────────


async def test_03_expiry_is_distinct_from_denial(app, token):
    async with client(app) as http:
        created = await create_request(http, token, key="key-expire-003")
        approval_id = created.json()["approval_id"]

        # Age the request past its TTL, then run the sweeper.
        async with app.state.db_sessions() as session:
            approval = await session.get(Approval, uuid.UUID(approval_id))
            approval.expires_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=1)
            await session.commit()
        swept = await app.state.approvals.sweep_expired()
        assert swept == 1

        outcome = (
            await http.get(
                f"/v1/approvals/{approval_id}/decision", headers=agent_headers(token)
            )
        ).json()
        # The wrapper treats this as deny, but the operator can tell
        # "I never saw it" from "I said no".
        assert outcome["state"] == "expired"
        assert outcome["allowed"] is False

        detail = (
            await http.get(f"/v1/approvals/{approval_id}", headers=serve_headers())
        ).json()
        assert detail["state"] == "expired"
        assert detail["decided_via"] == "timeout"


async def test_03b_decision_after_expiry_cannot_approve(app, token):
    """A late click must not resurrect an approval whose TTL elapsed."""
    async with client(app) as http:
        created = await create_request(http, token, key="key-late-003b")
        approval_id = created.json()["approval_id"]
        async with app.state.db_sessions() as session:
            approval = await session.get(Approval, uuid.UUID(approval_id))
            approval.expires_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=1)
            await session.commit()
        decided = await http.post(
            f"/v1/approvals/{approval_id}/decision",
            headers=serve_headers(),
            json={"approve": True},
        )
    assert decided.json()["state"] == "expired"


# ── §17.5 a service token can never decide ───────────────────────────────


async def test_05_service_token_cannot_decide(app, token, security_events):
    async with client(app) as http:
        created = await create_request(http, token, key="key-escalate-005")
        approval_id = created.json()["approval_id"]
        attempt = await http.post(
            f"/v1/approvals/{approval_id}/decision",
            headers=agent_headers(token),
            json={"approve": True},
        )
    assert attempt.status_code == 403
    assert "service_token_on_operator_path" in security_text(security_events)

    # And the approval is untouched.
    async with app.state.db_sessions() as session:
        approval = await session.get(Approval, uuid.UUID(approval_id))
    assert approval.state is ApprovalState.PENDING


async def test_05b_service_token_cannot_reach_other_operator_routes(app, token):
    async with client(app) as http:
        for method, path, body in [
            ("GET", "/v1/approvals", None),
            ("GET", "/v1/system/kill-switch", None),
            ("POST", "/v1/system/kill-switch", {"engaged": True}),
            ("GET", "/v1/service-tokens", None),
            ("GET", "/v1/agents", None),
        ]:
            response = await http.request(
                method, path, headers=agent_headers(token), json=body
            )
            assert response.status_code == 403, f"{method} {path}"


# ── §17.8 idempotency conflict ───────────────────────────────────────────


async def test_08_same_key_different_args_conflicts(app, token, security_events):
    async with client(app) as http:
        first = await create_request(
            http, token, key="key-idem-008", args={"path": "/workspace/a.txt"}
        )
        assert first.status_code == 201
        replay = await create_request(
            http, token, key="key-idem-008", args={"path": "/workspace/a.txt"}
        )
        assert replay.status_code == 201
        assert replay.json()["created"] is False
        assert replay.json()["approval_id"] == first.json()["approval_id"]

        swapped = await create_request(
            http, token, key="key-idem-008", args={"path": "/etc/shadow"}
        )
    assert swapped.status_code == 409
    assert "idempotency_key reuse with mutated args" in security_text(security_events)
    actions = await audit_actions(app)
    assert any(action == "approval.idempotency_conflict" for action, _ in actions)


# ── §17.9 critical is never auto-approved ────────────────────────────────


async def test_09_critical_ignores_auto_approve_policy(app, token, agent_id):
    from mission_control.models import ApprovalPolicy
    from mission_control.models.enums import PolicyAction

    async with app.state.db_sessions() as session:
        session.add(
            ApprovalPolicy(
                tool_name_pattern="*",           # maximally permissive
                arg_matchers={},
                action=PolicyAction.AUTO_APPROVE,
                max_risk_level=RiskLevel.CRITICAL,  # operator tried to allow everything
                enabled=True,
                created_by="test",
            )
        )
        await session.commit()

    async with client(app) as http:
        # A benign, non-critical call does auto-approve under this policy.
        benign = await create_request(
            http, token, key="key-benign-009", tool="read_file",
            args={"path": "/workspace/notes.md"},
        )
        assert benign.json()["state"] == "approved"

        # A credential read is critical and still asks, policy notwithstanding.
        critical = await create_request(
            http, token, key="key-critical-009", tool="read_file",
            args={"path": "/home/pete/.ssh/id_ed25519"},
        )
    assert critical.json()["state"] == "pending"
    assert critical.json()["risk_level"] == "critical"


# ── §17.11 flooding ──────────────────────────────────────────────────────


async def test_11_rate_limit_engages_and_alerts(app, token, agent_id, security_events):
    from mission_control.models import Event
    from mission_control.models.enums import EventKind

    limit = app.state.settings.approval_rate_limit_per_hour
    async with client(app) as http:
        states = []
        for index in range(limit + 5):
            response = await create_request(
                http, token, key=f"key-flood-{index:03d}", tool="read_file",
                args={"path": f"/workspace/{index}.txt"},
            )
            states.append(response.json()["state"])

    assert states[:limit].count("pending") == limit
    excess = states[limit:]
    assert excess and all(state == "denied" for state in excess)
    assert "APPROVAL FLOOD" in security_text(security_events)

    async with app.state.db_sessions() as session:
        approvals = (await session.scalars(select(Approval))).all()
        alerts = (
            await session.scalars(select(Event).where(Event.kind == EventKind.ERROR))
        ).all()
    rate_limited = [a for a in approvals if a.decided_via is DecidedVia.RATE_LIMIT]
    assert len(rate_limited) == len(excess)
    # A distinct alert, visible in the live tail — not just a log line.
    assert any(event.payload.get("alert") == "approval_rate_limit" for event in alerts)


# ── §17.12 kill switch ───────────────────────────────────────────────────


async def test_12_kill_switch_denies_pending_and_new(app, token):
    async with client(app) as http:
        pending = await create_request(http, token, key="key-kill-012a")
        pending_id = pending.json()["approval_id"]
        assert pending.json()["state"] == "pending"

        engaged = await http.post(
            "/v1/system/kill-switch", headers=serve_headers(), json={"engaged": True}
        )
        assert engaged.status_code == 200
        assert engaged.json()["pending_denied"] == 1

        # The already-pending request is denied…
        outcome = (
            await http.get(
                f"/v1/approvals/{pending_id}/decision", headers=agent_headers(token)
            )
        ).json()
        assert outcome["state"] == "denied"
        assert outcome["allowed"] is False

        # …and new requests are refused outright.
        fresh = await create_request(http, token, key="key-kill-012b")
        assert fresh.json()["state"] == "denied"

        # Visible to the operator (home screen reads this).
        queue = (await http.get("/v1/approvals", headers=serve_headers())).json()
        assert queue["kill_switch"]["engaged"] is True

        # Releasing restores normal operation.
        await http.post(
            "/v1/system/kill-switch", headers=serve_headers(), json={"engaged": False}
        )
        after = await create_request(http, token, key="key-kill-012c")
        assert after.json()["state"] == "pending"

    actions = await audit_actions(app)
    assert ("system.kill_switch.engaged", OPERATOR_LOGIN) in actions
    assert ("system.kill_switch.released", OPERATOR_LOGIN) in actions


async def test_12b_kill_switch_overrides_a_prior_approval(app, token):
    """Pause All Agents means nothing proceeds — including an approval the
    wrapper has not yet collected."""
    async with client(app) as http:
        created = await create_request(http, token, key="key-kill-012d")
        approval_id = created.json()["approval_id"]
        await http.post(
            f"/v1/approvals/{approval_id}/decision",
            headers=serve_headers(),
            json={"approve": True},
        )
        await http.post(
            "/v1/system/kill-switch", headers=serve_headers(), json={"engaged": True}
        )
        outcome = (
            await http.get(
                f"/v1/approvals/{approval_id}/decision", headers=agent_headers(token)
            )
        ).json()
    assert outcome["allowed"] is False


# ── token hygiene ────────────────────────────────────────────────────────


async def test_unbound_token_cannot_create(app):
    plaintext, prefix, digest = generate_token()
    async with app.state.db_sessions() as session:
        session.add(
            ServiceToken(
                name="unbound", token_hash=digest, token_prefix=prefix,
                scopes=[SCOPE_CREATE, SCOPE_POLL], agent_id=None, created_by="test",
            )
        )
        await session.commit()
    async with client(app) as http:
        response = await create_request(http, plaintext, key="key-unbound-001")
    assert response.status_code == 403


async def test_revoked_token_rejected(app, token, agent_id):
    async with app.state.db_sessions() as session:
        row = (await session.scalars(select(ServiceToken))).one()
        row.revoked_at = datetime.datetime.now(datetime.timezone.utc)
        await session.commit()
    async with client(app) as http:
        response = await create_request(http, token, key="key-revoked-001")
    assert response.status_code == 401


async def test_agent_cannot_poll_another_agents_approval(app, token, agent_id):
    other = await app.state.normalizer.register_agent(
        AgentRuntime.OPENCLAW, "openclaw:other", "Other"
    )
    other_plain, prefix, digest = generate_token()
    async with app.state.db_sessions() as session:
        session.add(
            ServiceToken(
                name="other", token_hash=digest, token_prefix=prefix,
                scopes=[SCOPE_CREATE, SCOPE_POLL], agent_id=other, created_by="test",
            )
        )
        await session.commit()
    async with client(app) as http:
        created = await create_request(http, token, key="key-cross-001")
        approval_id = created.json()["approval_id"]
        response = await http.get(
            f"/v1/approvals/{approval_id}/decision", headers=agent_headers(other_plain)
        )
    assert response.status_code == 404  # not 403: does not confirm existence


async def test_no_token_is_unauthenticated(app):
    async with client(app) as http:
        response = await http.post(
            "/v1/approvals",
            json={"idempotency_key": "x" * 10, "tool_name": "t", "tool_args": {}},
        )
    assert response.status_code == 401



# ── §7.6 one approval, one execution ─────────────────────────────────────


async def test_approval_can_be_claimed_only_once(app, token):
    """A compromised or buggy wrapper must not be able to poll an approval
    twice and execute twice."""
    async with client(app) as http:
        created = await create_request(http, token, key="key-claim-001")
        approval_id = created.json()["approval_id"]
        await http.post(
            f"/v1/approvals/{approval_id}/decision",
            headers=serve_headers(),
            json={"approve": True},
        )
        first = (
            await http.get(f"/v1/approvals/{approval_id}/decision", headers=agent_headers(token))
        ).json()
        second = (
            await http.get(f"/v1/approvals/{approval_id}/decision", headers=agent_headers(token))
        ).json()

    assert first["allowed"] is True
    assert first["already_consumed"] is False
    # Still reports `approved` — that is the truth — but permission is spent.
    assert second["state"] == "approved"
    assert second["allowed"] is False
    assert second["already_consumed"] is True


async def test_concurrent_claims_yield_exactly_one_permission(app, token):
    created_id = None
    async with client(app) as http:
        created = await create_request(http, token, key="key-claim-002")
        created_id = created.json()["approval_id"]
        await http.post(
            f"/v1/approvals/{created_id}/decision",
            headers=serve_headers(),
            json={"approve": True},
        )
        results = await asyncio.gather(
            *[
                http.get(
                    f"/v1/approvals/{created_id}/decision", headers=agent_headers(token)
                )
                for _ in range(8)
            ]
        )
    allowed = [r.json()["allowed"] for r in results]
    assert allowed.count(True) == 1, f"expected exactly one grant, got {allowed}"


async def test_claim_returns_args_digest_for_mutation_check(app, token):
    """The wrapper needs to prove the call it is about to make is the call
    that was approved."""
    async with client(app) as http:
        created = await create_request(
            http, token, key="key-digest-001", args={"path": "/workspace/x.txt"}
        )
        approval_id = created.json()["approval_id"]
        await http.post(
            f"/v1/approvals/{approval_id}/decision",
            headers=serve_headers(),
            json={"approve": True},
        )
        outcome = (
            await http.get(f"/v1/approvals/{approval_id}/decision", headers=agent_headers(token))
        ).json()
        detail = (
            await http.get(f"/v1/approvals/{approval_id}", headers=serve_headers())
        ).json()
    assert outcome["args_digest"]
    assert outcome["args_digest"] == detail["args_digest"]


async def test_denied_approval_is_not_consumable(app, token):
    async with client(app) as http:
        created = await create_request(http, token, key="key-claim-003")
        approval_id = created.json()["approval_id"]
        await http.post(
            f"/v1/approvals/{approval_id}/decision",
            headers=serve_headers(),
            json={"approve": False},
        )
        outcome = (
            await http.get(f"/v1/approvals/{approval_id}/decision", headers=agent_headers(token))
        ).json()
    assert outcome["allowed"] is False
    assert outcome["already_consumed"] is False


# ── verdict delivery is an obligation, not a log line ────────────────────


async def test_bridged_decision_is_marked_undelivered_until_confirmed(app, token, agent_id):
    """A verdict for a runtime blocking on us is not done until the runtime
    has it; otherwise the runtime's own timeout silently replaces the
    operator's answer."""
    from mission_control.models import Approval

    service = app.state.approvals
    snapshot, _ = await service.create(
        agent_id=agent_id,
        idempotency_key="bridged-001",
        tool_name="exec",
        tool_args={"command": "ls"},
        source="openclaw_rpc",
        external_ref="gw-approval-1",
    )
    await service.decide(snapshot.id, approve=True, operator_login=OPERATOR_LOGIN)

    async with app.state.db_sessions() as session:
        row = await session.get(Approval, snapshot.id)
    assert row.resolution_state == "pending"
    assert (await service.resolution_summary())["pending"] == 1

    await service.mark_resolution(snapshot.id, confirmed=False, error="gateway unreachable")
    async with app.state.db_sessions() as session:
        row = await session.get(Approval, snapshot.id)
    assert row.resolution_state == "failed"
    assert row.resolution_attempts == 1
    assert "unreachable" in row.resolution_error
    assert [s.id for s in await service.undelivered()] == [snapshot.id]

    await service.mark_resolution(snapshot.id, confirmed=True)
    async with app.state.db_sessions() as session:
        row = await session.get(Approval, snapshot.id)
    assert row.resolution_state == "confirmed"
    assert row.resolution_confirmed_at is not None
    assert await service.undelivered() == []


async def test_http_sourced_approvals_need_no_delivery(app, token):
    """The HTTP wrapper polls for itself, so there is nothing to deliver."""
    from mission_control.models import Approval

    async with client(app) as http:
        created = await create_request(http, token, key="key-nodelivery-001")
        approval_id = created.json()["approval_id"]
        await http.post(
            f"/v1/approvals/{approval_id}/decision",
            headers=serve_headers(),
            json={"approve": True},
        )
    async with app.state.db_sessions() as session:
        row = await session.get(Approval, uuid.UUID(approval_id))
    assert row.resolution_state == "not_required"
