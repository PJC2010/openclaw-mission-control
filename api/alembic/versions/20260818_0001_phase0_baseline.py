"""Phase 0 baseline — every table in spec §5.

Revision ID: 0001
Revises:
Create Date: 2026-08-18

Also applies the §12 S4 role grants when the roles exist (created by
deploy/postgres/initdb/01-roles.sh):
  - mc_app       CRUD on everything EXCEPT audit_log, which is SELECT+INSERT
                 only (append-only at the role level, §5.9)
  - mc_readonly  SELECT on everything
  - mc_migrate   owns the schema (runs this migration)

Grants are guarded so the migration also applies cleanly to throwaway
databases without the roles (CI, local dev).
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

UUID_PK = dict(server_default=sa.text("gen_random_uuid()"))
NOW = sa.text("now()")
EMPTY_JSONB = sa.text("'{}'::jsonb")


def _enum(name: str, *values: str) -> postgresql.ENUM:
    return postgresql.ENUM(*values, name=name, create_type=False)


objective_track = _enum("objective_track", "villagemd", "castillo", "axon", "personal")
objective_status = _enum("objective_status", "active", "paused", "archived")
agent_runtime = _enum("agent_runtime", "openclaw", "hermes")
run_status = _enum("run_status", "running", "succeeded", "failed", "cancelled")
run_trigger = _enum("run_trigger", "schedule", "message", "manual", "subagent")
objective_source = _enum("objective_source", "inferred", "manual", "none")
event_kind = _enum(
    "event_kind", "tool_call", "tool_result", "message", "log", "error", "lifecycle"
)
risk_level = _enum("risk_level", "low", "medium", "high", "critical")
approval_state = _enum(
    "approval_state", "pending", "approved", "denied", "expired", "cancelled"
)
decided_via = _enum("decided_via", "dashboard", "auto_policy", "timeout")
policy_action = _enum("policy_action", "auto_approve", "always_ask")
outcome_verdict = _enum("outcome_verdict", "useful", "not_useful", "harmful")
actor_source = _enum("actor_source", "identity_header", "service_token", "system")

ALL_ENUMS = (
    objective_track,
    objective_status,
    agent_runtime,
    run_status,
    run_trigger,
    objective_source,
    event_kind,
    risk_level,
    approval_state,
    decided_via,
    policy_action,
    outcome_verdict,
    actor_source,
)


def upgrade() -> None:
    bind = op.get_bind()
    for enum_type in ALL_ENUMS:
        enum_type.create(bind, checkfirst=True)

    # ── §5.1 objectives ──────────────────────────────────────────────────
    op.create_table(
        "objectives",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False, **UUID_PK),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("track", objective_track, nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("horizon_start", sa.Date(), nullable=True),
        sa.Column("horizon_end", sa.Date(), nullable=True),
        sa.Column("status", objective_status, nullable=False, server_default="active"),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), nullable=False, server_default=NOW),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), nullable=False, server_default=NOW),
        sa.PrimaryKeyConstraint("id", name="pk_objectives"),
        # Bare name on purpose: op.create_table applies the metadata naming
        # convention, expanding this to ck_objectives_priority_range.
        sa.CheckConstraint("priority BETWEEN 1 AND 5", name="priority_range"),
    )

    # ── §5.2 agents ──────────────────────────────────────────────────────
    op.create_table(
        "agents",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False, **UUID_PK),
        sa.Column("runtime", agent_runtime, nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("instance_key", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="unknown"),
        sa.Column("last_heartbeat_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("config_snapshot", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), nullable=False, server_default=NOW),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), nullable=False, server_default=NOW),
        sa.PrimaryKeyConstraint("id", name="pk_agents"),
        sa.UniqueConstraint("instance_key", name="uq_agents_instance_key"),
    )

    # ── §5.3 runs ────────────────────────────────────────────────────────
    op.create_table(
        "runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False, **UUID_PK),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("objective_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("objective_source", objective_source, nullable=False, server_default="none"),
        sa.Column("trigger", run_trigger, nullable=False),
        sa.Column("parent_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("started_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("ended_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("status", run_status, nullable=False, server_default="running"),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column("token_input", sa.BigInteger(), nullable=True),
        sa.Column("token_output", sa.BigInteger(), nullable=True),
        sa.Column("cost_usd", sa.Numeric(12, 6), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_runs"),
        sa.UniqueConstraint("agent_id", "external_id", name="uq_runs_agent_id_external_id"),
        sa.ForeignKeyConstraint(
            ["agent_id"], ["agents.id"], name="fk_runs_agent_id_agents", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["objective_id"], ["objectives.id"],
            name="fk_runs_objective_id_objectives", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["parent_run_id"], ["runs.id"], name="fk_runs_parent_run_id_runs", ondelete="RESTRICT"
        ),
    )
    op.create_index("ix_runs_agent_started", "runs", ["agent_id", sa.text("started_at DESC")])
    op.create_index(
        "ix_runs_objective_started", "runs", ["objective_id", sa.text("started_at DESC")]
    )

    # ── §5.4 events (append-only; partitioning deferred, see decisions D4) ─
    op.create_table(
        "events",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("ts", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("kind", event_kind, nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False, server_default=EMPTY_JSONB),
        sa.Column("dedupe_key", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_events"),
        sa.UniqueConstraint("dedupe_key", name="uq_events_dedupe_key"),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name="fk_events_run_id_runs", ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"], ["agents.id"], name="fk_events_agent_id_agents", ondelete="RESTRICT"
        ),
    )
    op.create_index("ix_events_agent_seq", "events", ["agent_id", "seq"])
    op.create_index("ix_events_run_id_id", "events", ["run_id", "id"])
    op.create_index("ix_events_ts_brin", "events", ["ts"], postgresql_using="brin")

    # ── §5.5 scheduled_tasks ─────────────────────────────────────────────
    op.create_table(
        "scheduled_tasks",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False, **UUID_PK),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("cron_expr", sa.Text(), nullable=True),
        sa.Column("timezone", sa.Text(), nullable=False, server_default="UTC"),
        sa.Column("next_fire_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("last_outcome", sa.Text(), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("objective_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("staleness_flag", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("writeback_supported", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), nullable=False, server_default=NOW),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), nullable=False, server_default=NOW),
        sa.PrimaryKeyConstraint("id", name="pk_scheduled_tasks"),
        sa.UniqueConstraint(
            "agent_id", "external_id", name="uq_scheduled_tasks_agent_id_external_id"
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"], ["agents.id"],
            name="fk_scheduled_tasks_agent_id_agents", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["last_run_id"], ["runs.id"],
            name="fk_scheduled_tasks_last_run_id_runs", ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["objective_id"], ["objectives.id"],
            name="fk_scheduled_tasks_objective_id_objectives", ondelete="RESTRICT",
        ),
    )

    # ── §5.6 approvals — the critical table ──────────────────────────────
    op.create_table(
        "approvals",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False, **UUID_PK),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("external_run_id", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("tool_name", sa.Text(), nullable=False),
        sa.Column("tool_args", postgresql.JSONB(), nullable=False),  # complete, never truncated (S7)
        sa.Column("args_digest", sa.String(64), nullable=False),
        sa.Column("claimed_risk", risk_level, nullable=True),  # agent hint only (§7.4)
        sa.Column("risk_level", risk_level, nullable=False),  # server-classified
        sa.Column("objective_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=False, server_default=""),
        sa.Column("state", approval_state, nullable=False, server_default="pending"),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), nullable=False, server_default=NOW),
        sa.Column("expires_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("decided_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("decided_by", sa.Text(), nullable=True),
        sa.Column("decided_via", decided_via, nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_approvals"),
        sa.UniqueConstraint("idempotency_key", name="uq_approvals_idempotency_key"),
        sa.ForeignKeyConstraint(
            ["agent_id"], ["agents.id"], name="fk_approvals_agent_id_agents", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name="fk_approvals_run_id_runs", ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["objective_id"], ["objectives.id"],
            name="fk_approvals_objective_id_objectives", ondelete="RESTRICT",
        ),
    )
    op.create_index("ix_approvals_state_created", "approvals", ["state", "created_at"])
    op.create_index(
        "ix_approvals_agent_created", "approvals", ["agent_id", sa.text("created_at DESC")]
    )

    # ── §5.7 approval_policies (allowlist semantics only) ────────────────
    op.create_table(
        "approval_policies",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False, **UUID_PK),
        sa.Column("objective_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("tool_name_pattern", sa.Text(), nullable=False),
        sa.Column("arg_matchers", postgresql.JSONB(), nullable=False, server_default=EMPTY_JSONB),
        sa.Column("action", policy_action, nullable=False),
        sa.Column("max_risk_level", risk_level, nullable=False, server_default="low"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), nullable=False, server_default=NOW),
        sa.PrimaryKeyConstraint("id", name="pk_approval_policies"),
        sa.ForeignKeyConstraint(
            ["objective_id"], ["objectives.id"],
            name="fk_approval_policies_objective_id_objectives", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"], ["agents.id"],
            name="fk_approval_policies_agent_id_agents", ondelete="RESTRICT",
        ),
    )

    # ── §5.8 outcomes ────────────────────────────────────────────────────
    op.create_table(
        "outcomes",
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("verdict", outcome_verdict, nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("rated_at", postgresql.TIMESTAMP(timezone=True), nullable=False, server_default=NOW),
        sa.Column("rated_by", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("run_id", name="pk_outcomes"),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name="fk_outcomes_run_id_runs", ondelete="CASCADE"
        ),
    )

    # ── §5.9 audit_log (append-only at the role level) ───────────────────
    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("ts", postgresql.TIMESTAMP(timezone=True), nullable=False, server_default=NOW),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("actor_source", actor_source, nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("entity_type", sa.Text(), nullable=False),
        sa.Column("entity_id", sa.Text(), nullable=True),
        sa.Column("before", postgresql.JSONB(), nullable=True),
        sa.Column("after", postgresql.JSONB(), nullable=True),
        sa.Column("request_ip", postgresql.INET(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_audit_log"),
    )
    op.create_index("ix_audit_log_ts", "audit_log", [sa.text("ts DESC")])
    op.create_index("ix_audit_log_entity", "audit_log", ["entity_type", "entity_id"])

    # ── §5.10 notifications ──────────────────────────────────────────────
    op.create_table(
        "notifications",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False, **UUID_PK),
        sa.Column("approval_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("sent_at", postgresql.TIMESTAMP(timezone=True), nullable=False, server_default=NOW),
        sa.Column("delivered_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("failed_reason", sa.Text(), nullable=True),
        sa.Column("payload_digest", sa.String(64), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_notifications"),
        sa.ForeignKeyConstraint(
            ["approval_id"], ["approvals.id"],
            name="fk_notifications_approval_id_approvals", ondelete="SET NULL",
        ),
    )

    # ── §12 S4 role grants (no-ops where the roles don't exist) ──────────
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mc_app') THEN
                GRANT USAGE ON SCHEMA public TO mc_app;
                GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO mc_app;
                GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO mc_app;
                -- §5.9: append-only. The application role can never rewrite history.
                REVOKE UPDATE, DELETE, TRUNCATE ON audit_log FROM mc_app;
                ALTER DEFAULT PRIVILEGES FOR ROLE mc_migrate IN SCHEMA public
                    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO mc_app;
                ALTER DEFAULT PRIVILEGES FOR ROLE mc_migrate IN SCHEMA public
                    GRANT USAGE, SELECT ON SEQUENCES TO mc_app;
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mc_readonly') THEN
                GRANT USAGE ON SCHEMA public TO mc_readonly;
                GRANT SELECT ON ALL TABLES IN SCHEMA public TO mc_readonly;
                ALTER DEFAULT PRIVILEGES FOR ROLE mc_migrate IN SCHEMA public
                    GRANT SELECT ON TABLES TO mc_readonly;
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    for table in (
        "notifications",
        "audit_log",
        "outcomes",
        "approval_policies",
        "approvals",
        "scheduled_tasks",
        "events",
        "runs",
        "agents",
        "objectives",
    ):
        op.drop_table(table)
    bind = op.get_bind()
    for enum_type in ALL_ENUMS:
        enum_type.drop(bind, checkfirst=True)
