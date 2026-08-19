"""Adapter high-water-mark cursors (Phase 1).

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-19
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "adapter_cursors",
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("cursor", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("agent_id", "source", name="pk_adapter_cursors"),
        sa.ForeignKeyConstraint(
            ["agent_id"], ["agents.id"],
            name="fk_adapter_cursors_agent_id_agents", ondelete="CASCADE",
        ),
    )
    # mc_app grants arrive via the default privileges established in 0001.


def downgrade() -> None:
    op.drop_table("adapter_cursors")
