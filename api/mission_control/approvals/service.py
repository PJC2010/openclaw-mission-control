"""The approval gateway (§7) — the blocking decision point between an agent
and a gated action.

Every path in this module fails closed. If the classifier cannot parse the
arguments, if the policy table is unreadable, if the operator never looks,
if the kill switch is on, if the rate limiter trips — the answer is not
approved. The only route to `approved` is an explicit human decision
carrying a verified tailnet identity, or an explicit `auto_approve` policy
match on a non-critical action.
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Sequence

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..config import Settings
from ..models import KILL_SWITCH_KEY, Approval, ApprovalPolicy, AuditLog, SystemFlag
from ..models.enums import (
    ActorSource,
    ApprovalState,
    DecidedVia,
    EventKind,
    RiskLevel,
)
from .classifier import classify
from .policy import evaluate

log = logging.getLogger("mission_control.approvals")
security_log = logging.getLogger("mission_control.security")

TERMINAL_STATES = frozenset(
    {ApprovalState.APPROVED, ApprovalState.DENIED, ApprovalState.EXPIRED, ApprovalState.CANCELLED}
)


class IdempotencyConflict(Exception):
    """§7.3 — same idempotency_key, different args. Either a bug or an
    attempt to swap the payload after approval; both are security events."""

    def __init__(self, existing_id: uuid.UUID) -> None:
        super().__init__("idempotency_key reused with different arguments")
        self.existing_id = existing_id


class RateLimited(Exception):
    """§7.8 — this agent has exceeded its hourly approval budget."""


@dataclass(frozen=True)
class ApprovalSnapshot:
    """Detached view of an approval, safe to hand to hooks and routes."""

    id: uuid.UUID
    agent_id: uuid.UUID
    state: ApprovalState
    tool_name: str
    tool_args: dict[str, Any]
    risk_level: RiskLevel
    risk_categories: dict[str, Any]
    source: str
    external_ref: str | None
    decided_via: DecidedVia | None
    decided_by: str | None
    decision_note: str | None
    expires_at: datetime.datetime
    created_at: datetime.datetime
    resolution_state: str = "not_required"
    resolution_attempts: int = 0

    @property
    def allowed(self) -> bool:
        return self.state is ApprovalState.APPROVED


@dataclass
class DecisionOutcome:
    """What a polling wrapper is told. `allowed` is the only field that may
    let an action run, and it is True only for an explicit approval."""

    approval_id: uuid.UUID
    state: ApprovalState
    decided_via: DecidedVia | None = None
    note: str | None = None
    reason: str = ""
    # True only on the single poll that successfully claimed an approval.
    claimed: bool = False
    already_consumed: bool = False
    # Lets the wrapper prove the call it is about to make is the call that
    # was approved (approve-then-mutate defence).
    args_digest: str | None = None

    @property
    def allowed(self) -> bool:
        """Permission to execute — granted at most once per approval (§7.6)."""
        return self.state is ApprovalState.APPROVED and self.claimed

    def as_json(self) -> dict[str, Any]:
        return {
            "approval_id": str(self.approval_id),
            "state": self.state.value,
            "allowed": self.allowed,
            "already_consumed": self.already_consumed,
            "args_digest": self.args_digest,
            "decided_via": self.decided_via.value if self.decided_via else None,
            "note": self.note,
            "reason": self.reason,
        }


DecisionHook = Callable[[ApprovalSnapshot], Awaitable[None]]


def canonical_args(tool_args: Any) -> str:
    return json.dumps(tool_args, sort_keys=True, separators=(",", ":"), default=str)


def digest_args(tool_args: Any) -> str:
    return hashlib.sha256(canonical_args(tool_args).encode("utf-8")).hexdigest()


class ApprovalService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        normalizer: Any | None = None,
    ) -> None:
        self._sessions = session_factory
        self._settings = settings
        self._normalizer = normalizer  # optional: surfaces alerts in the live tail
        self._waiters: dict[uuid.UUID, asyncio.Event] = {}
        self._hooks: list[DecisionHook] = []
        self._sweeper: asyncio.Task[None] | None = None

    # ── lifecycle ────────────────────────────────────────────────────────

    def register_decision_hook(self, hook: DecisionHook) -> None:
        """Called after a decision commits — used by the OpenClaw bridge to
        resolve the runtime-side approval over the gateway RPC."""
        self._hooks.append(hook)

    async def start(self) -> None:
        self._sweeper = asyncio.create_task(self._sweep_loop(), name="approval-ttl-sweeper")

    async def stop(self) -> None:
        if self._sweeper is not None:
            self._sweeper.cancel()
            try:
                await self._sweeper
            except asyncio.CancelledError:
                pass
            self._sweeper = None

    # ── creation (§7.1 steps 2–3) ────────────────────────────────────────

    async def create(
        self,
        *,
        agent_id: uuid.UUID,
        idempotency_key: str,
        tool_name: str,
        tool_args: Any,
        rationale: str = "",
        claimed_risk: RiskLevel | None = None,
        external_run_id: str | None = None,
        run_id: uuid.UUID | None = None,
        objective_id: uuid.UUID | None = None,
        source: str = "http",
        external_ref: str | None = None,
        ttl_seconds: int | None = None,
        actor: str = "agent",
        request_ip: str | None = None,
    ) -> tuple[ApprovalSnapshot, bool]:
        """Create (or return) an approval. Returns (snapshot, created)."""
        now = datetime.datetime.now(datetime.timezone.utc)
        args_digest = digest_args(tool_args)

        async with self._sessions() as session:
            # §7.3 — a retry with the same key returns the existing row and
            # its current state; the same key with different args is a
            # security event, not a new request.
            existing = await session.scalar(
                select(Approval).where(Approval.idempotency_key == idempotency_key)
            )
            if existing is not None:
                if existing.args_digest != args_digest:
                    security_log.error(
                        "idempotency_key reuse with mutated args: key=%s approval=%s "
                        "stored_digest=%s presented_digest=%s agent=%s",
                        idempotency_key, existing.id, existing.args_digest, args_digest, agent_id,
                    )
                    await self._audit(
                        session,
                        actor=actor,
                        actor_source=ActorSource.SERVICE_TOKEN,
                        action="approval.idempotency_conflict",
                        entity_id=str(existing.id),
                        after={
                            "idempotency_key": idempotency_key,
                            "stored_args_digest": existing.args_digest,
                            "presented_args_digest": args_digest,
                        },
                        request_ip=request_ip,
                    )
                    await session.commit()
                    raise IdempotencyConflict(existing.id)
                return self._snapshot(existing), False

            # §7.7 — while the kill switch is engaged, nothing new proceeds.
            if await self._kill_switch_on(session):
                approval = self._build(
                    agent_id=agent_id, idempotency_key=idempotency_key, tool_name=tool_name,
                    tool_args=tool_args, args_digest=args_digest, rationale=rationale,
                    claimed_risk=claimed_risk, external_run_id=external_run_id, run_id=run_id,
                    objective_id=objective_id, source=source, external_ref=external_ref,
                    now=now, ttl_seconds=ttl_seconds,
                    risk=classify(tool_name, tool_args, self._settings.workspace_roots),
                )
                approval.state = ApprovalState.DENIED
                approval.decided_at = now
                approval.decided_via = DecidedVia.KILL_SWITCH
                approval.decided_by = "system"
                approval.decision_note = "denied: all agents paused (kill switch engaged)"
                session.add(approval)
                await self._flush_with_dedupe(session, approval)
                await self._audit(
                    session, actor="system", actor_source=ActorSource.SYSTEM,
                    action="approval.auto_denied.kill_switch",
                    entity_id=str(approval.id), after=self._audit_body(approval),
                    request_ip=request_ip,
                )
                await session.commit()
                return self._snapshot(approval), True

            # §7.8 — approval fatigue is an attack. Beyond the hourly budget,
            # auto-deny and raise a DISTINCT alert.
            recent = await session.scalar(
                select(func.count())
                .select_from(Approval)
                .where(
                    Approval.agent_id == agent_id,
                    Approval.created_at >= now - datetime.timedelta(hours=1),
                )
            )
            over_budget = int(recent or 0) >= self._settings.approval_rate_limit_per_hour

            risk = classify(tool_name, tool_args, self._settings.workspace_roots)
            approval = self._build(
                agent_id=agent_id, idempotency_key=idempotency_key, tool_name=tool_name,
                tool_args=tool_args, args_digest=args_digest, rationale=rationale,
                claimed_risk=claimed_risk, external_run_id=external_run_id, run_id=run_id,
                objective_id=objective_id, source=source, external_ref=external_ref,
                now=now, ttl_seconds=ttl_seconds, risk=risk,
            )

            if over_budget:
                approval.state = ApprovalState.DENIED
                approval.decided_at = now
                approval.decided_via = DecidedVia.RATE_LIMIT
                approval.decided_by = "system"
                approval.decision_note = (
                    f"denied: agent exceeded {self._settings.approval_rate_limit_per_hour} "
                    "approval requests per hour (§7.8)"
                )
                session.add(approval)
                await self._flush_with_dedupe(session, approval)
                await self._audit(
                    session, actor="system", actor_source=ActorSource.SYSTEM,
                    action="approval.auto_denied.rate_limit",
                    entity_id=str(approval.id),
                    after=self._audit_body(approval) | {"recent_count": int(recent or 0)},
                    request_ip=request_ip,
                )
                await session.commit()
                security_log.error(
                    "APPROVAL FLOOD: agent=%s produced %s requests in the last hour; "
                    "excess auto-denied (§7.8)", agent_id, recent,
                )
                await self._raise_alert(
                    agent_id,
                    "approval_rate_limit",
                    {
                        "recent_count": int(recent or 0),
                        "limit_per_hour": self._settings.approval_rate_limit_per_hour,
                        "tool_name": tool_name,
                    },
                )
                return self._snapshot(approval), True

            # §7.1 step 3 — allowlist policy evaluation. Critical actions can
            # never reach auto-approve (S8), enforced inside evaluate().
            policies: Sequence[ApprovalPolicy] = (
                await session.scalars(select(ApprovalPolicy).where(ApprovalPolicy.enabled.is_(True)))
            ).all()
            decision = evaluate(
                policies,
                tool_name=tool_name,
                tool_args=tool_args,
                risk=risk,
                agent_id=agent_id,
                objective_id=objective_id,
            )
            if decision.auto_approve:
                approval.state = ApprovalState.APPROVED
                approval.decided_at = now
                approval.decided_via = DecidedVia.AUTO_POLICY
                approval.decided_by = "system"
                approval.decision_note = decision.reason

            session.add(approval)
            await self._flush_with_dedupe(session, approval)
            await self._audit(
                session,
                actor="system" if decision.auto_approve else actor,
                actor_source=ActorSource.SYSTEM if decision.auto_approve else ActorSource.SERVICE_TOKEN,
                action=(
                    "approval.auto_approved" if decision.auto_approve else "approval.created"
                ),
                entity_id=str(approval.id),
                after=self._audit_body(approval) | {"policy": decision.matched_policy_id},
                request_ip=request_ip,
            )
            await session.commit()
            snapshot = self._snapshot(approval)

        if snapshot.state is ApprovalState.PENDING:
            log.info(
                "approval pending: id=%s tool=%s risk=%s agent=%s",
                snapshot.id, snapshot.tool_name, snapshot.risk_level.value, snapshot.agent_id,
            )
        else:
            await self._fire_hooks(snapshot)
        return snapshot, True

    # ── decision (§7.1 step 5) ───────────────────────────────────────────

    async def decide(
        self,
        approval_id: uuid.UUID,
        *,
        approve: bool,
        operator_login: str,
        note: str | None = None,
        request_ip: str | None = None,
    ) -> ApprovalSnapshot:
        """Record an operator decision. Callers MUST have passed the
        identity path (§11) — this method never checks a service token,
        because a service token can never reach it (C4)."""
        async with self._sessions() as session:
            approval = await session.get(Approval, approval_id)
            if approval is None:
                raise KeyError(approval_id)
            before = self._audit_body(approval)
            if approval.state is not ApprovalState.PENDING:
                # First answer wins; a late second decision changes nothing.
                return self._snapshot(approval)

            # A decision cannot resurrect an approval whose TTL already
            # elapsed, even if the sweeper has not run yet.
            now = datetime.datetime.now(datetime.timezone.utc)
            if approval.expires_at <= now:
                approval.state = ApprovalState.EXPIRED
                approval.decided_at = now
                approval.decided_via = DecidedVia.TIMEOUT
                approval.decision_note = "expired before the decision was recorded"
            elif await self._kill_switch_on(session):
                approval.state = ApprovalState.DENIED
                approval.decided_at = now
                approval.decided_via = DecidedVia.KILL_SWITCH
                approval.decided_by = operator_login
                approval.decision_note = "denied: all agents paused (kill switch engaged)"
            else:
                approval.state = (
                    ApprovalState.APPROVED if approve else ApprovalState.DENIED
                )
                approval.decided_at = now
                approval.decided_via = DecidedVia.DASHBOARD
                approval.decided_by = operator_login
                approval.decision_note = note

            if approval.source != "http":
                # A verdict for a runtime that is blocking on us is not
                # delivered until the runtime says so.
                approval.resolution_state = "pending"
            await self._audit(
                session,
                actor=operator_login,
                actor_source=ActorSource.IDENTITY_HEADER,  # §12 S6
                action=f"approval.{approval.state.value}",
                entity_id=str(approval.id),
                before=before,
                after=self._audit_body(approval),
                request_ip=request_ip,
            )
            await session.commit()
            snapshot = self._snapshot(approval)

        self._wake(approval_id)
        await self._fire_hooks(snapshot)
        return snapshot

    # ── polling (§7.1 step 4) ────────────────────────────────────────────

    async def get_decision(self, approval_id: uuid.UUID) -> DecisionOutcome:
        async with self._sessions() as session:
            approval = await session.get(Approval, approval_id)
            if approval is None:
                raise KeyError(approval_id)
            # Kill switch overrides even a stored approval: "Pause All
            # Agents" means nothing proceeds, so a wrapper that has not yet
            # observed its approval is told no. Stricter than §7.7's letter
            # ("pending and new"), matching its intent.
            if await self._kill_switch_on(session):
                return DecisionOutcome(
                    approval_id=approval_id,
                    state=ApprovalState.DENIED,
                    decided_via=DecidedVia.KILL_SWITCH,
                    reason="all agents paused (kill switch engaged)",
                )
            return DecisionOutcome(
                approval_id=approval_id,
                state=approval.state,
                decided_via=approval.decided_via,
                note=approval.decision_note,
                already_consumed=approval.consumed_at is not None,
                args_digest=approval.args_digest,
            )

    async def claim_decision(self, approval_id: uuid.UUID) -> DecisionOutcome:
        """Read the decision AND consume it if it is an approval.

        §7.6: one approval, one execution. The claim is a single atomic
        compare-and-set, so two concurrent pollers cannot both be told yes,
        and a replayed poll after a crash is refused rather than silently
        re-authorising the action.
        """
        outcome = await self.get_decision(approval_id)
        if outcome.state is not ApprovalState.APPROVED:
            return outcome
        now = datetime.datetime.now(datetime.timezone.utc)
        async with self._sessions() as session:
            claimed_id = await session.scalar(
                update(Approval)
                .where(
                    Approval.id == approval_id,
                    Approval.state == ApprovalState.APPROVED,
                    Approval.consumed_at.is_(None),
                )
                .values(consumed_at=now)
                .returning(Approval.id)
            )
            await session.commit()
        if claimed_id is None:
            security_log.warning(
                "approval %s was polled again after being consumed — a wrapper "
                "retry, or an attempt to execute one approval twice (§7.6)",
                approval_id,
            )
            outcome.already_consumed = True
            outcome.claimed = False
            outcome.reason = "this approval was already used; request a new one"
            return outcome
        outcome.claimed = True
        outcome.already_consumed = False
        return outcome

    async def wait_for_decision(self, approval_id: uuid.UUID, wait_seconds: float) -> DecisionOutcome:
        """Long-poll for a terminal state. On timeout returns the current
        (pending) state — the wrapper re-polls; it never treats a timeout
        as permission."""
        deadline = min(wait_seconds, self._settings.approval_long_poll_max_seconds)
        outcome = await self.claim_decision(approval_id)
        if outcome.state in TERMINAL_STATES or deadline <= 0:
            return outcome
        event = self._waiters.setdefault(approval_id, asyncio.Event())
        try:
            await asyncio.wait_for(event.wait(), timeout=deadline)
        except (asyncio.TimeoutError, TimeoutError):
            pass
        finally:
            if event.is_set():
                self._waiters.pop(approval_id, None)
        return await self.claim_decision(approval_id)

    def _wake(self, approval_id: uuid.UUID) -> None:
        event = self._waiters.get(approval_id)
        if event is not None:
            event.set()

    # ── TTL expiry (§7.2) ────────────────────────────────────────────────

    async def sweep_expired(self) -> int:
        """Move due pending approvals to `expired` — reported distinctly
        from `denied` so the operator can tell "I said no" from "I never
        saw it"."""
        now = datetime.datetime.now(datetime.timezone.utc)
        async with self._sessions() as session:
            due = (
                await session.scalars(
                    select(Approval).where(
                        Approval.state == ApprovalState.PENDING, Approval.expires_at <= now
                    )
                )
            ).all()
            if not due:
                return 0
            snapshots: list[ApprovalSnapshot] = []
            for approval in due:
                before = self._audit_body(approval)
                approval.state = ApprovalState.EXPIRED
                approval.decided_at = now
                approval.decided_via = DecidedVia.TIMEOUT
                approval.decision_note = "expired without a decision"
                if approval.source != "http":
                    approval.resolution_state = "pending"
                await self._audit(
                    session, actor="system", actor_source=ActorSource.SYSTEM,
                    action="approval.expired", entity_id=str(approval.id),
                    before=before, after=self._audit_body(approval),
                )
                snapshots.append(self._snapshot(approval))
            await session.commit()

        for snapshot in snapshots:
            self._wake(snapshot.id)
            await self._fire_hooks(snapshot)
            log.warning(
                "approval expired without a decision: id=%s tool=%s — if this rises, "
                "the notification path is broken (§7.2)", snapshot.id, snapshot.tool_name,
            )
        return len(snapshots)

    async def _sweep_loop(self) -> None:
        while True:
            try:
                await self.sweep_expired()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — the sweeper must never die
                log.exception("approval TTL sweep failed")
            await asyncio.sleep(self._settings.approval_expiry_sweep_seconds)

    # ── kill switch (§7.7) ───────────────────────────────────────────────

    async def kill_switch_state(self) -> dict[str, Any]:
        async with self._sessions() as session:
            flag = await session.get(SystemFlag, KILL_SWITCH_KEY)
            if flag is None:
                return {"engaged": False}
            value = dict(flag.value or {})
            value.setdefault("engaged", False)
            value["updated_at"] = flag.updated_at.isoformat()
            value["updated_by"] = flag.updated_by
            return value

    async def set_kill_switch(
        self, *, engaged: bool, operator_login: str, request_ip: str | None = None
    ) -> dict[str, Any]:
        now = datetime.datetime.now(datetime.timezone.utc)
        denied: list[ApprovalSnapshot] = []
        async with self._sessions() as session:
            flag = await session.get(SystemFlag, KILL_SWITCH_KEY)
            before = dict(flag.value) if flag is not None else {"engaged": False}
            if flag is None:
                flag = SystemFlag(key=KILL_SWITCH_KEY, value={})
                session.add(flag)
            flag.value = {"engaged": engaged, "changed_at": now.isoformat()}
            flag.updated_at = now
            flag.updated_by = operator_login

            if engaged:
                pending = (
                    await session.scalars(
                        select(Approval).where(Approval.state == ApprovalState.PENDING)
                    )
                ).all()
                for approval in pending:
                    approval.state = ApprovalState.DENIED
                    approval.decided_at = now
                    approval.decided_via = DecidedVia.KILL_SWITCH
                    approval.decided_by = operator_login
                    approval.decision_note = "denied: all agents paused (kill switch engaged)"
                    if approval.source != "http":
                        approval.resolution_state = "pending"
                    denied.append(self._snapshot(approval))

            await self._audit(
                session,
                actor=operator_login,
                actor_source=ActorSource.IDENTITY_HEADER,
                action="system.kill_switch." + ("engaged" if engaged else "released"),
                entity_type="system_flag",
                entity_id=KILL_SWITCH_KEY,
                before=before,
                after={"engaged": engaged, "pending_denied": len(denied)},
                request_ip=request_ip,
            )
            await session.commit()

        for snapshot in denied:
            self._wake(snapshot.id)
            await self._fire_hooks(snapshot)
        log.warning(
            "KILL SWITCH %s by %s (%d pending approvals denied)",
            "ENGAGED" if engaged else "released", operator_login, len(denied),
        )
        return {"engaged": engaged, "pending_denied": len(denied)}

    # ── verdict delivery to the runtimes ─────────────────────────────────

    async def undelivered(self, limit: int = 50) -> list[ApprovalSnapshot]:
        """Decided approvals whose verdict the runtime has not confirmed."""
        async with self._sessions() as session:
            rows = (
                await session.scalars(
                    select(Approval)
                    .where(Approval.resolution_state.in_(("pending", "failed")))
                    .order_by(Approval.decided_at)
                    .limit(limit)
                )
            ).all()
            return [self._snapshot(row) for row in rows]

    async def mark_resolution(
        self, approval_id: uuid.UUID, *, confirmed: bool, error: str | None = None
    ) -> None:
        now = datetime.datetime.now(datetime.timezone.utc)
        async with self._sessions() as session:
            approval = await session.get(Approval, approval_id)
            if approval is None:
                return
            approval.resolution_attempts = (approval.resolution_attempts or 0) + 1
            if confirmed:
                approval.resolution_state = "confirmed"
                approval.resolution_confirmed_at = now
                approval.resolution_error = None
            else:
                approval.resolution_state = "failed"
                approval.resolution_error = (error or "")[:2000]
                security_log.error(
                    "verdict for approval %s has not reached its runtime after %d "
                    "attempts: %s — until it does, the runtime is deciding on its "
                    "own timeout, not on the operator's answer",
                    approval_id, approval.resolution_attempts, error,
                )
            await session.commit()

    async def resolution_summary(self) -> dict[str, int]:
        async with self._sessions() as session:
            rows = (
                await session.scalars(
                    select(Approval.resolution_state).where(
                        Approval.resolution_state.in_(("pending", "failed"))
                    )
                )
            ).all()
        summary = {"pending": 0, "failed": 0}
        for state in rows:
            summary[state] = summary.get(state, 0) + 1
        return summary

    async def _kill_switch_on(self, session: AsyncSession) -> bool:
        flag = await session.get(SystemFlag, KILL_SWITCH_KEY)
        if flag is None:
            return False
        return bool((flag.value or {}).get("engaged"))

    # ── helpers ──────────────────────────────────────────────────────────

    def _build(
        self, *, agent_id, idempotency_key, tool_name, tool_args, args_digest, rationale,
        claimed_risk, external_run_id, run_id, objective_id, source, external_ref, now,
        ttl_seconds, risk,
    ) -> Approval:
        ttl = ttl_seconds if ttl_seconds is not None else self._settings.approval_ttl_seconds
        return Approval(
            agent_id=agent_id,
            run_id=run_id,
            external_run_id=external_run_id,
            idempotency_key=idempotency_key,
            tool_name=tool_name,
            tool_args=tool_args if isinstance(tool_args, dict) else {"value": tool_args},
            args_digest=args_digest,
            claimed_risk=claimed_risk,
            risk_level=risk.level,
            risk_categories=risk.as_json(),
            objective_id=objective_id,
            rationale=rationale or "",
            source=source,
            external_ref=external_ref,
            state=ApprovalState.PENDING,
            created_at=now,
            expires_at=now + datetime.timedelta(seconds=ttl),
        )

    async def _flush_with_dedupe(self, session: AsyncSession, approval: Approval) -> None:
        """Surface the (agent_id, external_ref) uniqueness violation as a
        clear error rather than a raw IntegrityError — a replayed gateway
        event must not open a second request for one real action."""
        try:
            await session.flush()
        except IntegrityError as exc:
            await session.rollback()
            raise IdempotencyConflict(approval.id) from exc

    def _snapshot(self, approval: Approval) -> ApprovalSnapshot:
        return ApprovalSnapshot(
            id=approval.id,
            agent_id=approval.agent_id,
            state=approval.state,
            tool_name=approval.tool_name,
            tool_args=dict(approval.tool_args or {}),
            risk_level=approval.risk_level,
            risk_categories=dict(approval.risk_categories or {}),
            source=approval.source,
            external_ref=approval.external_ref,
            decided_via=approval.decided_via,
            decided_by=approval.decided_by,
            decision_note=approval.decision_note,
            expires_at=approval.expires_at,
            created_at=approval.created_at,
            resolution_state=approval.resolution_state or "not_required",
            resolution_attempts=approval.resolution_attempts or 0,
        )

    def _audit_body(self, approval: Approval) -> dict[str, Any]:
        # Deliberately excludes tool_args: the audit row records the
        # decision, and the complete args already live on the approval row
        # (§5.6, never truncated). Digest is enough to prove identity.
        return {
            "state": approval.state.value,
            "tool_name": approval.tool_name,
            "args_digest": approval.args_digest,
            "risk_level": approval.risk_level.value,
            "risk_categories": approval.risk_categories,
            "source": approval.source,
            "decided_via": approval.decided_via.value if approval.decided_via else None,
            "decided_by": approval.decided_by,
        }

    async def _audit(
        self, session: AsyncSession, *, actor: str, actor_source: ActorSource, action: str,
        entity_id: str | None, after: dict[str, Any] | None = None,
        before: dict[str, Any] | None = None, entity_type: str = "approval",
        request_ip: str | None = None,
    ) -> None:
        session.add(
            AuditLog(
                actor=actor,
                actor_source=actor_source,
                action=action,
                entity_type=entity_type,
                entity_id=entity_id,
                before=before,
                after=after,
                request_ip=request_ip,
            )
        )

    async def _fire_hooks(self, snapshot: ApprovalSnapshot) -> None:
        for hook in self._hooks:
            try:
                await hook(snapshot)
            except Exception:  # noqa: BLE001 — a failing hook must not alter the decision
                log.exception("approval decision hook failed for %s", snapshot.id)

    async def _raise_alert(self, agent_id: uuid.UUID, kind: str, payload: dict[str, Any]) -> None:
        """Surface a distinct alert in the live event stream so it is
        visible in the dashboard, not just in the log (§7.8)."""
        if self._normalizer is None:
            return
        try:
            from ..normalizer import NormalizedEvent

            await self._normalizer.ingest(
                agent_id,
                [
                    NormalizedEvent(
                        ts=datetime.datetime.now(datetime.timezone.utc),
                        kind=EventKind.ERROR,
                        payload={"alert": kind, **payload},
                    )
                ],
            )
        except Exception:  # noqa: BLE001 — alerting must not break the deny path
            log.exception("failed to raise %s alert", kind)
