"""OpenClaw approval bridge — the RPC route's safety properties.

The gateway's `approval.resolve` fails closed by DENYING on a malformed
verdict (wrong kind, or a decision outside that record's allowedDecisions)
rather than returning an error, so a client bug silently kills the agent's
command. These tests pin the guards that prevent that, plus the fail-open
detection that makes the gate meaningful at all.
"""

from __future__ import annotations

import uuid

import pytest

from mission_control.bridges import OpenClawApprovalBridge
from mission_control.models.enums import AgentRuntime, ApprovalState

from .conftest import make_settings, security_text

pytestmark = pytest.mark.anyio


class FakeClient:
    """Records calls and replays scripted results."""

    def __init__(self, **responses):
        self.responses = responses
        self.calls: list[tuple[str, dict]] = []

    async def call(self, method: str, params: dict) -> dict:
        self.calls.append((method, dict(params)))
        value = self.responses.get(method)
        if isinstance(value, Exception):
            raise value
        if callable(value):
            return value(params)
        return value if value is not None else {}

    def params_for(self, method: str) -> dict:
        return next(p for m, p in self.calls if m == method)


@pytest.fixture
async def bridge(db_sessions, normalizer):
    from mission_control.approvals import ApprovalService

    settings = make_settings()
    service = ApprovalService(db_sessions, settings, normalizer=normalizer)
    agent_id = await normalizer.register_agent(AgentRuntime.OPENCLAW, "openclaw:bridge", "OC")
    return OpenClawApprovalBridge(agent_id, settings, service, normalizer), service, agent_id


# ── fail-open detection (C7) ─────────────────────────────────────────────


async def test_ask_fallback_full_is_flagged_as_fail_open(bridge, security_events):
    obj, _service, _agent = bridge
    obj.attach(
        FakeClient(**{"exec.approvals.get": {"defaults": {"askFallback": "full"}, "agents": {}}})
    )
    reason = await obj.verify_fail_closed()
    assert reason and "FAIL OPEN" in reason
    assert obj.fail_open_risk is not None
    assert "askFallback" in security_text(security_events)


async def test_ask_fallback_full_on_one_agent_is_flagged(bridge):
    obj, _service, _agent = bridge
    obj.attach(
        FakeClient(
            **{
                "exec.approvals.get": {
                    "defaults": {"askFallback": "deny"},
                    "agents": {"main": {"askFallback": "deny"}, "side": {"askFallback": "allowlist"}},
                }
            }
        )
    )
    reason = await obj.verify_fail_closed()
    assert reason and "agents.side" in reason


async def test_ask_fallback_deny_everywhere_is_safe(bridge):
    obj, _service, _agent = bridge
    obj.attach(
        FakeClient(
            **{
                "exec.approvals.get": {
                    "defaults": {"askFallback": "deny"},
                    "agents": {"main": {"askFallback": "deny"}},
                }
            }
        )
    )
    assert await obj.verify_fail_closed() is None
    assert obj.fail_open_risk is None


async def test_unreadable_policy_counts_as_unsafe(bridge):
    """The decider credential holds operator.approvals, while exec.approvals.*
    escalates to operator.admin — so this probe can legitimately fail. It is
    still treated as unverified, never as verified-safe."""
    obj, _service, _agent = bridge
    obj.attach(FakeClient(**{"exec.approvals.get": PermissionError("missing scope")}))
    reason = await obj.verify_fail_closed()
    assert reason and "could not read" in reason


# ── mirroring ────────────────────────────────────────────────────────────


async def test_requested_event_is_mirrored_with_complete_payload(bridge):
    obj, service, agent_id = bridge
    obj.attach(
        FakeClient(
            **{
                "approval.get": {
                    "presentation": {
                        "kind": "exec",
                        "allowedDecisions": ["allow-once", "deny"],
                        "title": "run a command",
                    }
                }
            }
        )
    )
    created = await obj.handle_requested(
        {
            "id": "gw-1",
            "createdAtMs": 1_766_000_000_000,
            "expiresAtMs": 1_766_000_600_000,
            "request": {
                "command": "rm -rf /var/data",
                "sessionKey": "sess-a",
                "runId": "run-a",
                "allowedDecisions": ["allow-once", "deny"],
            },
        }
    )
    assert created is True

    from sqlalchemy import select
    from mission_control.models import Approval

    async with service._sessions() as session:  # noqa: SLF001
        row = await session.scalar(select(Approval).where(Approval.external_ref == "gw-1"))
    assert row.source == "openclaw_rpc"
    assert row.state is ApprovalState.PENDING
    # Complete payload retained (§12 S7), and classified critical by us —
    # never by anything the gateway or the agent asserted.
    assert row.tool_args["request"]["command"] == "rm -rf /var/data"
    assert row.risk_level.value == "critical"


async def test_replayed_requested_event_does_not_duplicate(bridge):
    obj, service, _agent = bridge
    obj.attach(FakeClient(**{"approval.get": {"presentation": {"kind": "exec"}}}))
    event = {"id": "gw-2", "expiresAtMs": 1_766_000_600_000, "request": {"command": "ls"}}
    assert await obj.handle_requested(event) is True
    assert await obj.handle_requested(event) is False

    from sqlalchemy import func, select
    from mission_control.models import Approval

    async with service._sessions() as session:  # noqa: SLF001
        count = await session.scalar(
            select(func.count()).select_from(Approval).where(Approval.external_ref == "gw-2")
        )
    assert count == 1


async def test_event_without_id_is_ignored(bridge):
    obj, _service, _agent = bridge
    obj.attach(FakeClient())
    assert await obj.handle_requested({"request": {"command": "ls"}}) is False


# ── resolving back ───────────────────────────────────────────────────────


async def make_pending(obj, service, agent_id, *, external_ref="gw-x", allowed=None):
    obj.attach(
        FakeClient(
            **{
                "approval.get": {
                    "presentation": {
                        "kind": "exec",
                        "allowedDecisions": allowed if allowed is not None else ["allow-once", "deny"],
                    }
                },
                "approval.resolve": {"applied": True},
            }
        )
    )
    snapshot, _ = await service.create(
        agent_id=agent_id,
        idempotency_key=f"openclaw:{external_ref}",
        tool_name="exec",
        tool_args={"command": "ls"},
        source="openclaw_rpc",
        external_ref=external_ref,
    )
    return snapshot


async def test_approval_resolves_as_allow_once_never_allow_always(bridge):
    obj, service, agent_id = bridge
    snapshot = await make_pending(obj, service, agent_id, external_ref="gw-3")
    await service.decide(snapshot.id, approve=True, operator_login="pete@example.com")
    params = obj._client.params_for("approval.resolve")  # noqa: SLF001
    # §7.6 — one approval, one execution. allow-always would write a
    # standing allowlist entry in the runtime, which is not ours to grant.
    assert params["decision"] == "allow-once"
    assert params["kind"] == "exec"  # echoed from the record, never guessed
    assert params["id"] == "gw-3"


async def test_denial_resolves_as_deny(bridge):
    obj, service, agent_id = bridge
    snapshot = await make_pending(obj, service, agent_id, external_ref="gw-4")
    await service.decide(snapshot.id, approve=False, operator_login="pete@example.com")
    assert obj._client.params_for("approval.resolve")["decision"] == "deny"  # noqa: SLF001


async def test_approval_denied_when_allow_once_not_offered(bridge, security_events):
    """Sending a decision outside allowedDecisions would force a
    malformed-deny anyway; denying deliberately is honest and logged."""
    obj, service, agent_id = bridge
    snapshot = await make_pending(obj, service, agent_id, external_ref="gw-5", allowed=["deny"])
    await service.decide(snapshot.id, approve=True, operator_login="pete@example.com")
    assert obj._client.params_for("approval.resolve")["decision"] == "deny"  # noqa: SLF001
    assert "does not allow" in security_text(security_events)


async def test_failed_delivery_is_recorded_and_retried(bridge):
    obj, service, agent_id = bridge
    snapshot = await make_pending(obj, service, agent_id, external_ref="gw-6")
    obj._client.responses["approval.resolve"] = ConnectionError("socket gone")  # noqa: SLF001
    await service.decide(snapshot.id, approve=False, operator_login="pete@example.com")

    outstanding = await service.undelivered()
    assert [s.id for s in outstanding] == [snapshot.id]
    assert (await service.resolution_summary())["failed"] == 1

    # Gateway comes back; the retry sweep delivers it.
    obj._client.responses["approval.resolve"] = {"applied": True}  # noqa: SLF001
    assert await obj.retry_undelivered() == 1
    assert await service.undelivered() == []


async def test_delivery_without_connection_is_recorded_not_lost(bridge):
    obj, service, agent_id = bridge
    snapshot = await make_pending(obj, service, agent_id, external_ref="gw-7")
    obj.detach()
    await service.decide(snapshot.id, approve=False, operator_login="pete@example.com")
    assert [s.id for s in await service.undelivered()] == [snapshot.id]


async def test_positive_not_found_counts_as_delivered(bridge):
    """`approval.get` resolves pending records only, so a gateway that
    positively reports the record absent has already gone terminal."""
    from mission_control.adapters.openclaw.client import GatewayError

    obj, service, agent_id = bridge
    snapshot = await make_pending(obj, service, agent_id, external_ref="gw-8")
    obj._client.responses["approval.get"] = GatewayError(  # noqa: SLF001
        {"code": "NOT_FOUND", "message": "no such approval"}, "approval.get"
    )
    await service.decide(snapshot.id, approve=False, operator_login="pete@example.com")
    assert await service.undelivered() == []


async def test_unreadable_record_keeps_the_verdict_owed(bridge):
    """"I could not read it" must never be mistaken for "already resolved".

    Treating a transient RPC failure as delivered silently discards the
    operator's decision and leaves the gateway to its own askFallback.
    """
    obj, service, agent_id = bridge
    snapshot = await make_pending(obj, service, agent_id, external_ref="gw-8b")
    obj._client.responses["approval.get"] = ConnectionError("socket reset")  # noqa: SLF001
    await service.decide(snapshot.id, approve=False, operator_login="pete@example.com")
    assert [s.id for s in await service.undelivered()] == [snapshot.id]

    # And once the gateway is readable again, the retry delivers it.
    obj._client.responses["approval.get"] = {  # noqa: SLF001
        "presentation": {"kind": "exec", "allowedDecisions": ["allow-once", "deny"]}
    }
    assert await obj.retry_undelivered() == 1
    assert await service.undelivered() == []


async def test_ttl_is_clamped_to_the_gateway_deadline(bridge):
    """Expiring after the gateway already gave up would leave the operator
    deciding a record nobody is waiting on."""
    import datetime

    obj, service, agent_id = bridge
    obj.attach(FakeClient(**{"approval.get": {"presentation": {"kind": "plugin"}}}))
    soon_ms = int(
        (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=120)).timestamp()
        * 1000
    )
    await obj.handle_requested({"id": "gw-9", "expiresAtMs": soon_ms, "request": {"title": "x"}})

    from sqlalchemy import select
    from mission_control.models import Approval

    async with service._sessions() as session:  # noqa: SLF001
        row = await session.scalar(select(Approval).where(Approval.external_ref == "gw-9"))
    budget = (row.expires_at - row.created_at).total_seconds()
    # Well under our 900s default, and inside the gateway's own 120s window.
    assert 60 <= budget <= 105
