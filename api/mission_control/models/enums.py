"""Domain enums (§5). Stored as native PostgreSQL enum types.

Values — not Python member names — are persisted; keep values snake_case
and treat them as a wire format.
"""

from __future__ import annotations

import enum

from sqlalchemy import Enum as SaEnum


class Track(enum.StrEnum):
    """Pete's three ventures plus personal (§2, §5.1)."""

    VILLAGEMD = "villagemd"
    CASTILLO = "castillo"
    AXON = "axon"
    PERSONAL = "personal"


class ObjectiveStatus(enum.StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    ARCHIVED = "archived"


class AgentRuntime(enum.StrEnum):
    OPENCLAW = "openclaw"
    HERMES = "hermes"


class RunStatus(enum.StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RunTrigger(enum.StrEnum):
    SCHEDULE = "schedule"
    MESSAGE = "message"
    MANUAL = "manual"
    SUBAGENT = "subagent"


class ObjectiveSource(enum.StrEnum):
    INFERRED = "inferred"
    MANUAL = "manual"
    NONE = "none"


class EventKind(enum.StrEnum):
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    MESSAGE = "message"
    LOG = "log"
    ERROR = "error"
    LIFECYCLE = "lifecycle"


class RiskLevel(enum.StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ApprovalState(enum.StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"  # distinct from denied on purpose (§7.2)
    CANCELLED = "cancelled"


class DecidedVia(enum.StrEnum):
    DASHBOARD = "dashboard"
    AUTO_POLICY = "auto_policy"
    TIMEOUT = "timeout"
    # Phase 2: auto-denials that are neither an operator's "no" nor a
    # missed TTL. Kept distinct so the queue can explain itself (§7.7/§7.8).
    KILL_SWITCH = "kill_switch"
    RATE_LIMIT = "rate_limit"


class PolicyAction(enum.StrEnum):
    AUTO_APPROVE = "auto_approve"
    ALWAYS_ASK = "always_ask"


class OutcomeVerdict(enum.StrEnum):
    USEFUL = "useful"
    NOT_USEFUL = "not_useful"
    HARMFUL = "harmful"


class ActorSource(enum.StrEnum):
    IDENTITY_HEADER = "identity_header"
    SERVICE_TOKEN = "service_token"
    SYSTEM = "system"


def pg_enum(py_enum: type[enum.StrEnum], name: str) -> SaEnum:
    """Native PG enum persisting member *values*."""
    return SaEnum(
        py_enum,
        name=name,
        values_callable=lambda e: [member.value for member in e],
        native_enum=True,
    )


OBJECTIVE_TRACK = pg_enum(Track, "objective_track")
OBJECTIVE_STATUS = pg_enum(ObjectiveStatus, "objective_status")
AGENT_RUNTIME = pg_enum(AgentRuntime, "agent_runtime")
RUN_STATUS = pg_enum(RunStatus, "run_status")
RUN_TRIGGER = pg_enum(RunTrigger, "run_trigger")
OBJECTIVE_SOURCE = pg_enum(ObjectiveSource, "objective_source")
EVENT_KIND = pg_enum(EventKind, "event_kind")
RISK_LEVEL = pg_enum(RiskLevel, "risk_level")
APPROVAL_STATE = pg_enum(ApprovalState, "approval_state")
DECIDED_VIA = pg_enum(DecidedVia, "decided_via")
POLICY_ACTION = pg_enum(PolicyAction, "policy_action")
OUTCOME_VERDICT = pg_enum(OutcomeVerdict, "outcome_verdict")
ACTOR_SOURCE = pg_enum(ActorSource, "actor_source")
