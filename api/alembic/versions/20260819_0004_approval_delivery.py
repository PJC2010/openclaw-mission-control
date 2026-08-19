"""Phase 2 hardening — single-use approvals and verdict delivery tracking.

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-19

Closes two fail-open paths found in adversarial review:

* An approved row could be polled repeatedly, so one approval could
  authorise several executions (§7.6 says one approval, one execution).
  `consumed_at` makes the agent-facing poll an atomic claim.

* A verdict that failed to reach the runtime was logged and forgotten.
  The runtime's record then stayed pending, timed out, and its own
  fallback decided — the operator's "no" silently replaced by whatever
  the runtime does on timeout. The resolution_* columns make delivery a
  tracked, retried, and visible obligation.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("approvals", sa.Column("consumed_at", postgresql.TIMESTAMP(timezone=True), nullable=True))
    op.add_column(
        "approvals",
        sa.Column("resolution_state", sa.Text(), nullable=False, server_default="not_required"),
    )
    op.add_column(
        "approvals", sa.Column("resolution_attempts", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column("approvals", sa.Column("resolution_error", sa.Text(), nullable=True))
    op.add_column(
        "approvals",
        sa.Column("resolution_confirmed_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
    )
    # The delivery worker scans for outstanding verdicts.
    op.create_index(
        "ix_approvals_resolution_pending",
        "approvals",
        ["resolution_state", "decided_at"],
        postgresql_where=sa.text("resolution_state IN ('pending','failed')"),
    )


def downgrade() -> None:
    op.drop_index("ix_approvals_resolution_pending", table_name="approvals")
    for column in (
        "resolution_confirmed_at",
        "resolution_error",
        "resolution_attempts",
        "resolution_state",
        "consumed_at",
    ):
        op.drop_column("approvals", column)
