"""Approval endpoints — two auth paths that never converge (§7.5).

AGENT path (service token, exempted from operator identity in the
middleware by method+path):
    POST /v1/approvals                      create a gated request
    GET  /v1/approvals/{id}/decision?wait=  block until decided

OPERATOR path (Tailscale identity, §11):
    GET  /v1/approvals                      queue
    GET  /v1/approvals/{id}                 detail, complete arguments
    POST /v1/approvals/{id}/decision        decide  ← never reachable by a token
    GET/POST /v1/system/kill-switch         §7.7
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import desc, select

from ..approvals.service import IdempotencyConflict
from ..auth.identity import OperatorIdentity
from ..auth.service_token import (
    SCOPE_CREATE,
    SCOPE_POLL,
    AuthenticatedAgent,
    authenticate,
    extract_bearer,
)
from ..models import Agent, Approval, ServiceToken
from ..models.enums import ApprovalState, RiskLevel

router = APIRouter()


# ── dependencies ─────────────────────────────────────────────────────────


async def require_service_token(
    request: Request, authorization: str | None = Header(default=None)
) -> AuthenticatedAgent:
    """The AGENT auth path. Never consults identity headers."""
    plaintext = extract_bearer(authorization)
    if plaintext is None:
        raise HTTPException(status_code=401, detail="service token required")
    async with request.app.state.db_sessions() as session:
        agent = await authenticate(session, plaintext)
    if agent is None:
        raise HTTPException(status_code=401, detail="invalid or revoked service token")
    return agent


def require_operator(request: Request) -> OperatorIdentity:
    """The OPERATOR auth path. The middleware has already verified identity
    against `tailscale whois`; absence here means a wiring mistake, so fail
    closed rather than assume."""
    operator = getattr(request.state, "operator", None)
    if operator is None:
        raise HTTPException(status_code=401, detail="operator identity required")
    return operator


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


# ── agent path ───────────────────────────────────────────────────────────


class CreateApprovalBody(BaseModel):
    idempotency_key: str = Field(min_length=8, max_length=256)
    tool_name: str = Field(min_length=1, max_length=512)
    # Complete arguments — stored whole, never truncated (§12 S7).
    tool_args: Any = Field(default_factory=dict)
    rationale: str = Field(default="", max_length=8192)
    # A hint only. Stored as `claimed_risk`; the server classifies
    # independently and the agent's opinion never lowers the result (§7.4).
    claimed_risk: str | None = None
    run_id: str | None = Field(default=None, max_length=512)
    ttl_seconds: int | None = Field(default=None, ge=10, le=86400)


@router.post("/v1/approvals", status_code=201)
async def create_approval(
    request: Request,
    body: CreateApprovalBody,
    agent: AuthenticatedAgent = Depends(require_service_token),
) -> dict[str, Any]:
    if not agent.has(SCOPE_CREATE):
        raise HTTPException(status_code=403, detail=f"token lacks {SCOPE_CREATE}")
    if agent.agent_id is None:
        # An unbound token could speak for any agent. Refuse rather than
        # infer identity from the request body.
        raise HTTPException(
            status_code=403, detail="service token is not bound to an agent"
        )

    claimed: RiskLevel | None = None
    if body.claimed_risk:
        try:
            claimed = RiskLevel(body.claimed_risk.strip().lower())
        except ValueError:
            claimed = None  # lenient: it is only a hint

    service = request.app.state.approvals
    try:
        snapshot, created = await service.create(
            agent_id=uuid.UUID(agent.agent_id),
            idempotency_key=body.idempotency_key,
            tool_name=body.tool_name,
            tool_args=body.tool_args,
            rationale=body.rationale,
            claimed_risk=claimed,
            external_run_id=body.run_id,
            ttl_seconds=body.ttl_seconds,
            actor=f"service_token:{agent.name}",
            request_ip=_client_ip(request),
        )
    except IdempotencyConflict as conflict:
        # §7.3 — same key, different args. Either a bug or a payload swap
        # after approval. Refuse loudly; the wrapper must fail closed.
        raise HTTPException(
            status_code=409,
            detail={
                "error": "idempotency_key reused with different arguments",
                "approval_id": str(conflict.existing_id),
            },
        ) from conflict

    return {
        "approval_id": str(snapshot.id),
        "state": snapshot.state.value,
        "allowed": snapshot.allowed,
        "risk_level": snapshot.risk_level.value,
        "expires_at": snapshot.expires_at.isoformat(),
        "created": created,
        "poll_url": f"/v1/approvals/{snapshot.id}/decision",
    }


@router.get("/v1/approvals/{approval_id}/decision")
async def poll_decision(
    request: Request,
    approval_id: uuid.UUID,
    wait: float = Query(default=0, ge=0, le=60),
    agent: AuthenticatedAgent = Depends(require_service_token),
) -> dict[str, Any]:
    if not agent.has(SCOPE_POLL):
        raise HTTPException(status_code=403, detail=f"token lacks {SCOPE_POLL}")

    async with request.app.state.db_sessions() as session:
        approval = await session.get(Approval, approval_id)
    # An agent may only poll its own approvals. 404 rather than 403 so the
    # response does not confirm that another agent's approval exists.
    if approval is None or (agent.agent_id and str(approval.agent_id) != agent.agent_id):
        raise HTTPException(status_code=404, detail="approval not found")

    service = request.app.state.approvals
    outcome = (
        await service.wait_for_decision(approval_id, wait)
        if wait > 0
        else await service.get_decision(approval_id)
    )
    return outcome.as_json()


# ── operator path ────────────────────────────────────────────────────────


def _approval_json(approval: Approval, agents: dict[str, str]) -> dict[str, Any]:
    return {
        "id": str(approval.id),
        "agent_id": str(approval.agent_id),
        "agent_name": agents.get(str(approval.agent_id), "unknown"),
        "tool_name": approval.tool_name,
        "risk_level": approval.risk_level.value,
        "risk_categories": approval.risk_categories or {},
        "claimed_risk": approval.claimed_risk.value if approval.claimed_risk else None,
        "state": approval.state.value,
        "rationale": approval.rationale,
        "source": approval.source,
        "created_at": approval.created_at.isoformat(),
        "expires_at": approval.expires_at.isoformat(),
        "decided_at": approval.decided_at.isoformat() if approval.decided_at else None,
        "decided_by": approval.decided_by,
        "decided_via": approval.decided_via.value if approval.decided_via else None,
        "decision_note": approval.decision_note,
        "external_run_id": approval.external_run_id,
    }


@router.get("/v1/approvals")
async def list_approvals(
    request: Request,
    state: ApprovalState | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    operator: OperatorIdentity = Depends(require_operator),
) -> dict[str, Any]:
    async with request.app.state.db_sessions() as session:
        query = select(Approval).order_by(desc(Approval.created_at)).limit(limit)
        if state is not None:
            query = query.where(Approval.state == state)
        approvals = (await session.scalars(query)).all()
        agents = {
            str(row.id): row.display_name for row in (await session.scalars(select(Agent))).all()
        }
        pending_count = len(
            (
                await session.scalars(
                    select(Approval.id).where(Approval.state == ApprovalState.PENDING)
                )
            ).all()
        )
    kill_switch = await request.app.state.approvals.kill_switch_state()
    return {
        "approvals": [_approval_json(a, agents) for a in approvals],
        "pending_count": pending_count,
        "kill_switch": kill_switch,
    }


@router.get("/v1/approvals/{approval_id}")
async def approval_detail(
    request: Request,
    approval_id: uuid.UUID,
    operator: OperatorIdentity = Depends(require_operator),
) -> dict[str, Any]:
    async with request.app.state.db_sessions() as session:
        approval = await session.get(Approval, approval_id)
        if approval is None:
            raise HTTPException(status_code=404, detail="approval not found")
        agents = {
            str(row.id): row.display_name for row in (await session.scalars(select(Agent))).all()
        }
    body = _approval_json(approval, agents)
    # §12 S7 — the COMPLETE arguments. Truncation in storage is forbidden
    # and truncation here would defeat the point of the review.
    body["tool_args"] = approval.tool_args
    body["args_digest"] = approval.args_digest
    return body


class DecisionBody(BaseModel):
    approve: bool
    note: str | None = Field(default=None, max_length=2048)


@router.post("/v1/approvals/{approval_id}/decision")
async def decide_approval(
    request: Request,
    approval_id: uuid.UUID,
    body: DecisionBody,
    operator: OperatorIdentity = Depends(require_operator),
) -> dict[str, Any]:
    """The only route to `approved`. Reachable exclusively via verified
    tailnet identity — a service token is refused by the middleware before
    it ever arrives here (C4, §17 test 5)."""
    try:
        snapshot = await request.app.state.approvals.decide(
            approval_id,
            approve=body.approve,
            operator_login=operator.login,
            note=body.note,
            request_ip=_client_ip(request),
        )
    except KeyError as missing:
        raise HTTPException(status_code=404, detail="approval not found") from missing
    return {
        "id": str(snapshot.id),
        "state": snapshot.state.value,
        "decided_by": snapshot.decided_by,
        "decided_via": snapshot.decided_via.value if snapshot.decided_via else None,
        "decision_note": snapshot.decision_note,
    }


# ── kill switch (§7.7) ───────────────────────────────────────────────────


@router.get("/v1/system/kill-switch")
async def get_kill_switch(
    request: Request, operator: OperatorIdentity = Depends(require_operator)
) -> dict[str, Any]:
    return await request.app.state.approvals.kill_switch_state()


@router.post("/v1/system/kill-switch")
async def set_kill_switch(
    request: Request,
    engaged: bool = Body(embed=True),
    operator: OperatorIdentity = Depends(require_operator),
) -> dict[str, Any]:
    return await request.app.state.approvals.set_kill_switch(
        engaged=engaged, operator_login=operator.login, request_ip=_client_ip(request)
    )


# ── service token administration (operator only) ─────────────────────────


@router.get("/v1/service-tokens")
async def list_service_tokens(
    request: Request, operator: OperatorIdentity = Depends(require_operator)
) -> dict[str, Any]:
    async with request.app.state.db_sessions() as session:
        tokens = (await session.scalars(select(ServiceToken).order_by(ServiceToken.created_at))).all()
    return {
        "tokens": [
            {
                "id": str(token.id),
                "name": token.name,
                "prefix": token.token_prefix,
                "scopes": token.scopes,
                "agent_id": str(token.agent_id) if token.agent_id else None,
                "created_at": token.created_at.isoformat(),
                "created_by": token.created_by,
                "last_used_at": token.last_used_at.isoformat() if token.last_used_at else None,
                "revoked_at": token.revoked_at.isoformat() if token.revoked_at else None,
            }
            for token in tokens
        ]
    }


@router.post("/v1/service-tokens/{token_id}/revoke")
async def revoke_service_token(
    request: Request,
    token_id: uuid.UUID,
    operator: OperatorIdentity = Depends(require_operator),
) -> dict[str, Any]:
    import datetime

    from ..models import AuditLog
    from ..models.enums import ActorSource

    async with request.app.state.db_sessions() as session:
        token = await session.get(ServiceToken, token_id)
        if token is None:
            raise HTTPException(status_code=404, detail="token not found")
        token.revoked_at = datetime.datetime.now(datetime.timezone.utc)
        session.add(
            AuditLog(
                actor=operator.login,
                actor_source=ActorSource.IDENTITY_HEADER,
                action="service_token.revoked",
                entity_type="service_token",
                entity_id=str(token_id),
                after={"name": token.name, "prefix": token.token_prefix},
                request_ip=_client_ip(request),
            )
        )
        await session.commit()
    return {"id": str(token_id), "revoked": True}
