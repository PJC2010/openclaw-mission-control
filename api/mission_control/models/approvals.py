"""Approvals subsystem tables (§5.6, §5.7) — the security boundary.

Schema-level invariants the Phase 2 gateway will rely on:
- `idempotency_key` unique — a replay returns the existing row; a replay
  with different args is detected via `args_digest` and 409s (§7.3).
- `tool_args` complete, never truncated (S7 forbids truncation in storage).
- `claimed_risk` (agent hint) is stored apart from `risk_level`
  (server-classified) — the agent may be compromised (§7.4).
- policies are allowlist-only: absence of a matching rule means ask (§5.7).
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base
from .enums import (
    APPROVAL_STATE,
    DECIDED_VIA,
    POLICY_ACTION,
    RISK_LEVEL,
    ApprovalState,
    DecidedVia,
    PolicyAction,
    RiskLevel,
)

UUID_DEFAULT = text("gen_random_uuid()")


class Approval(Base):
    """§5.6 — one gated tool call awaiting / carrying a decision."""

    __tablename__ = "approvals"
    __table_args__ = (
        Index("ix_approvals_state_created", "state", "created_at"),
        Index("ix_approvals_agent_created", "agent_id", text("created_at DESC")),
        # One mirror row per runtime-side approval id (RPC route): a replayed
        # gateway event must not open a second pending request for one action.
        UniqueConstraint("agent_id", "external_ref", name="uq_approvals_agent_id_external_ref"),
        Index(
            "ix_approvals_resolution_pending",
            "resolution_state",
            "decided_at",
            postgresql_where=text("resolution_state IN ('pending','failed')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=UUID_DEFAULT)
    agent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agents.id", ondelete="RESTRICT"))
    run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("runs.id", ondelete="SET NULL"))
    # What the wrapper sent as its runtime-native run reference; the
    # normalizer back-fills run_id once the run row exists (docs/decisions.md D6).
    external_run_id: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str] = mapped_column(Text, unique=True)
    tool_name: Mapped[str] = mapped_column(Text)
    tool_args: Mapped[dict[str, Any]]  # complete payload — truncation forbidden (S7)
    args_digest: Mapped[str] = mapped_column(String(64))  # sha256 hex, server-computed (§7.3)
    claimed_risk: Mapped[RiskLevel | None] = mapped_column(RISK_LEVEL)
    risk_level: Mapped[RiskLevel] = mapped_column(RISK_LEVEL)
    # Why the server graded it this way — shown to the operator, and the
    # audit record of what the classifier saw (§7.4).
    risk_categories: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb")
    )
    # How the request reached us: 'http' (§7.1 wrapper) or 'openclaw_rpc'
    # (the gateway raised it and we decide over the WS).
    source: Mapped[str] = mapped_column(Text, default="http", server_default="http")
    # The runtime's own approval id, for resolving back over the RPC route.
    external_ref: Mapped[str | None] = mapped_column(Text)
    objective_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("objectives.id", ondelete="RESTRICT")
    )
    rationale: Mapped[str] = mapped_column(Text, default="", server_default="")
    state: Mapped[ApprovalState] = mapped_column(
        APPROVAL_STATE, default=ApprovalState.PENDING, server_default="pending"
    )
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=text("now()"))
    expires_at: Mapped[datetime.datetime]
    decided_at: Mapped[datetime.datetime | None]
    decided_by: Mapped[str | None] = mapped_column(Text)  # tailnet login (§11.2)
    decided_via: Mapped[DecidedVia | None] = mapped_column(DECIDED_VIA)
    decision_note: Mapped[str | None] = mapped_column(Text)

    # §7.6 "one approval, one execution": an approved row is claimable
    # exactly once. Without this a compromised wrapper polls once and
    # executes twice, and the queue cannot tell the difference.
    consumed_at: Mapped[datetime.datetime | None]

    # A decision that never reaches the runtime is a fail-open: the runtime
    # record stays pending, times out, and whatever its own fallback says
    # happens instead of what the operator said. These track delivery so an
    # undelivered verdict is visible rather than silent.
    #   not_required — nothing to deliver (HTTP wrapper polls for itself)
    #   pending      — queued for delivery to the runtime
    #   confirmed    — the runtime accepted our verdict
    #   failed       — delivery is failing; the operator needs to know
    resolution_state: Mapped[str] = mapped_column(
        Text, default="not_required", server_default="not_required"
    )
    resolution_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    resolution_error: Mapped[str | None] = mapped_column(Text)
    resolution_confirmed_at: Mapped[datetime.datetime | None]


class ApprovalPolicy(Base):
    """§5.7 — allowlist auto-approval rules. `critical` never auto-approves (S8)."""

    __tablename__ = "approval_policies"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=UUID_DEFAULT)
    objective_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("objectives.id", ondelete="RESTRICT")
    )
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agents.id", ondelete="RESTRICT")
    )
    tool_name_pattern: Mapped[str] = mapped_column(Text)
    arg_matchers: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    action: Mapped[PolicyAction] = mapped_column(POLICY_ACTION)
    max_risk_level: Mapped[RiskLevel] = mapped_column(
        RISK_LEVEL, default=RiskLevel.LOW, server_default="low"
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    created_by: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=text("now()"))


class ServiceToken(Base):
    """Agent-side credentials (§7.5 / §12 S5).

    Hashed at rest and rotatable without redeploy. Scopes are limited to
    `approvals:create` and `approvals:poll`; `approvals:decide` is not a
    grantable scope for this credential class at all — C4 is enforced by
    the token never being able to name it, not merely by omitting it.
    """

    __tablename__ = "service_tokens"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=UUID_DEFAULT)
    name: Mapped[str] = mapped_column(Text)
    # sha256 of the presented secret; the plaintext is shown once at creation.
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    # Short non-secret prefix so the operator can tell tokens apart in the UI.
    token_prefix: Mapped[str] = mapped_column(String(12))
    scopes: Mapped[list[str]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    # Optional binding: a token issued for one agent cannot speak for another.
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE")
    )
    created_at: Mapped[datetime.datetime] = mapped_column(server_default=text("now()"))
    created_by: Mapped[str] = mapped_column(Text)
    last_used_at: Mapped[datetime.datetime | None]
    revoked_at: Mapped[datetime.datetime | None]


class SystemFlag(Base):
    """Global operational flags — the kill switch lives here (§7.7).

    Key/value rather than columns so Phase 5's quiet-hours and later
    switches do not each need a migration. Every write is audited.
    """

    __tablename__ = "system_flags"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    updated_at: Mapped[datetime.datetime] = mapped_column(
        server_default=text("now()"), onupdate=text("now()")
    )
    updated_by: Mapped[str] = mapped_column(Text, default="system", server_default="system")


KILL_SWITCH_KEY = "kill_switch"
