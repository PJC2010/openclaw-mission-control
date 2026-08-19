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

from sqlalchemy import Boolean, ForeignKey, Index, String, Text, text
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
