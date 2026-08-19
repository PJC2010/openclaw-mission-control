"""Operator-verdict, audit, and notification tables (§5.8–§5.10)."""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import BigInteger, ForeignKey, Identity, Index, String, Text, text
from sqlalchemy.dialects.postgresql import INET
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base
from .enums import ACTOR_SOURCE, OUTCOME_VERDICT, ActorSource, OutcomeVerdict

UUID_DEFAULT = text("gen_random_uuid()")


class Outcome(Base):
    """§5.8 — the operator's one-tap verdict on a completed run."""

    __tablename__ = "outcomes"

    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True
    )
    verdict: Mapped[OutcomeVerdict] = mapped_column(OUTCOME_VERDICT)
    note: Mapped[str | None] = mapped_column(Text)
    rated_at: Mapped[datetime.datetime] = mapped_column(server_default=text("now()"))
    rated_by: Mapped[str] = mapped_column(Text)


class AuditLog(Base):
    """§5.9 — append-only. The mc_app role holds INSERT/SELECT but no
    UPDATE/DELETE (granted in the baseline migration; §12 S4/S6)."""

    __tablename__ = "audit_log"
    __table_args__ = (
        Index("ix_audit_log_ts", text("ts DESC")),
        Index("ix_audit_log_entity", "entity_type", "entity_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    ts: Mapped[datetime.datetime] = mapped_column(server_default=text("now()"))
    actor: Mapped[str] = mapped_column(Text)  # tailnet login or 'system'
    actor_source: Mapped[ActorSource] = mapped_column(ACTOR_SOURCE)
    action: Mapped[str] = mapped_column(Text)
    entity_type: Mapped[str] = mapped_column(Text)
    entity_id: Mapped[str | None] = mapped_column(Text)
    before: Mapped[dict[str, Any] | None]
    after: Mapped[dict[str, Any] | None]
    request_ip: Mapped[str | None] = mapped_column(INET)


class Notification(Base):
    """§5.10 — delivery ledger for the notifier (push + fallback)."""

    __tablename__ = "notifications"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=UUID_DEFAULT)
    approval_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("approvals.id", ondelete="SET NULL")
    )
    channel: Mapped[str] = mapped_column(Text)  # 'webpush' | 'ntfy' | ... (§18.1 open)
    sent_at: Mapped[datetime.datetime] = mapped_column(server_default=text("now()"))
    delivered_at: Mapped[datetime.datetime | None]
    failed_reason: Mapped[str | None] = mapped_column(Text)
    # Lock-screen bodies never carry tool args (§10); the digest ties the
    # notification to what was actually sent without storing it twice.
    payload_digest: Mapped[str | None] = mapped_column(String(64))
