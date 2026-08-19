"""Phase 2 — approvals gateway support tables and columns.

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-19

Adds the agent-side credential store (§7.5), global operational flags for
the kill switch (§7.7), and the columns the approval path needs for the
OpenClaw RPC route and for showing the operator *why* something was graded
critical (§7.4).
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # §7.8 rate-limit and §7.7 kill-switch denials need to be legible in the
    # audit trail as distinct outcomes, not lumped into 'dashboard'.
    op.execute("ALTER TYPE decided_via ADD VALUE IF NOT EXISTS 'kill_switch'")
    op.execute("ALTER TYPE decided_via ADD VALUE IF NOT EXISTS 'rate_limit'")

    op.create_table(
        "service_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("token_prefix", sa.String(12), nullable=False),
        sa.Column("scopes", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("last_used_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("revoked_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_service_tokens"),
        sa.UniqueConstraint("token_hash", name="uq_service_tokens_token_hash"),
        sa.ForeignKeyConstraint(
            ["agent_id"], ["agents.id"],
            name="fk_service_tokens_agent_id_agents", ondelete="CASCADE",
        ),
    )

    op.create_table(
        "system_flags",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_by", sa.Text(), nullable=False, server_default="system"),
        sa.PrimaryKeyConstraint("key", name="pk_system_flags"),
    )

    op.add_column(
        "approvals",
        sa.Column("risk_categories", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
    )
    op.add_column(
        "approvals", sa.Column("source", sa.Text(), nullable=False, server_default="http")
    )
    op.add_column("approvals", sa.Column("external_ref", sa.Text(), nullable=True))
    # One mirror row per runtime-side approval: replayed gateway events must
    # not be able to create duplicate pending requests for one real action.
    op.create_unique_constraint(
        "uq_approvals_agent_id_external_ref", "approvals", ["agent_id", "external_ref"]
    )

    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mc_app') THEN
                GRANT SELECT, INSERT, UPDATE, DELETE ON service_tokens, system_flags TO mc_app;
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mc_readonly') THEN
                GRANT SELECT ON service_tokens, system_flags TO mc_readonly;
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.drop_constraint("uq_approvals_agent_id_external_ref", "approvals", type_="unique")
    op.drop_column("approvals", "external_ref")
    op.drop_column("approvals", "source")
    op.drop_column("approvals", "risk_categories")
    op.drop_table("system_flags")
    op.drop_table("service_tokens")
    # PostgreSQL cannot drop an enum value; the added decided_via values
    # remain. Harmless: nothing reads them once the rows are gone.
