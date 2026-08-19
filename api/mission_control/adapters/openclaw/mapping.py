"""OpenClaw audit-ledger rows → normalized contract.

`audit.activity.list` (schemaVersion 1) is the durable metadata ledger:
stable `eventId` (→ dedupe_key), monotonic ledger `sequence` (→
source_seq), Unix-ms `occurredAt`. It deliberately stores no prompts,
tool arguments, or message bodies — OpenClaw observation is metadata-only
in Phase 1 (docs/decisions.md P1-D2).
"""

from __future__ import annotations

import datetime
from typing import Any

from ...models.enums import EventKind, RunStatus, RunTrigger
from ...normalizer.contract import NormalizedEvent, RunUpsert
from ..util import ts_from_iso, ts_from_ms

_RUN_STATUS = {
    "started": RunStatus.RUNNING,
    "succeeded": RunStatus.SUCCEEDED,
    "failed": RunStatus.FAILED,
    "cancelled": RunStatus.CANCELLED,
    "timed_out": RunStatus.FAILED,
    "blocked": RunStatus.FAILED,
    "unknown": RunStatus.FAILED,
}


def _occurred_at(row: dict[str, Any]) -> datetime.datetime:
    value = row.get("occurredAt")
    if isinstance(value, (int, float)):
        return ts_from_ms(int(value)) or datetime.datetime.now(datetime.timezone.utc)
    parsed = ts_from_iso(value if isinstance(value, str) else None)
    return parsed or datetime.datetime.now(datetime.timezone.utc)


def audit_row_events(
    row: dict[str, Any],
    key_prefix: str,
    lineage: dict[str, str],
    last_run_in_session: dict[str, str],
) -> list[NormalizedEvent]:
    """Map one AuditActivityEventV1 row.

    Subagent linkage (§6.1): `lineage` maps child sessionKey → spawning
    sessionKey (from sessions.list); `last_run_in_session` tracks the most
    recent runId per session as rows stream oldest-first, so a run starting
    in a spawned session resolves its parent run even within one batch.
    """
    event_type = row.get("eventType")
    ts = _occurred_at(row)
    dedupe = f"{key_prefix}:{row.get('eventId')}"
    seq = row.get("sequence") if isinstance(row.get("sequence"), int) else None
    run_id = row.get("runId")
    session_key = row.get("sessionKey")
    status = str(row.get("status") or "unknown")

    if event_type == "agent_run" and run_id:
        run_status = _RUN_STATUS.get(status, RunStatus.FAILED)
        parent = None
        if session_key:
            parent_session = lineage.get(str(session_key))
            if parent_session:
                parent = last_run_in_session.get(parent_session)
            last_run_in_session[str(session_key)] = str(run_id)
        hints = RunUpsert(
            external_id=str(run_id),
            status=run_status,
            started_at=ts if status == "started" else None,
            ended_at=ts if status != "started" else None,
            parent_external_id=parent,
            trigger=RunTrigger.SUBAGENT if parent else None,
            error_summary=(
                str(row.get("errorCode"))
                if run_status is RunStatus.FAILED and row.get("errorCode")
                else None
            ),
        )
        return [
            NormalizedEvent(
                ts=ts,
                kind=EventKind.LIFECYCLE,
                payload={
                    "transition": "run_" + ("started" if status == "started" else "ended"),
                    "status": status,
                    "action": row.get("action"),
                    "openclaw_agent_id": row.get("agentId"),
                    "session_key": session_key,
                    "error_code": row.get("errorCode"),
                },
                external_run_id=str(run_id),
                dedupe_key=dedupe,
                source_seq=seq,
                run=hints,
            )
        ]

    if event_type == "tool_action" and run_id:
        kind = EventKind.TOOL_CALL if status == "started" else EventKind.TOOL_RESULT
        return [
            NormalizedEvent(
                ts=ts,
                kind=kind,
                payload={
                    "tool_name": row.get("toolName"),
                    "tool_call_id": row.get("toolCallId"),
                    "status": status,
                    "error_code": row.get("errorCode"),
                    "openclaw_agent_id": row.get("agentId"),
                    "session_key": session_key,
                },
                external_run_id=str(run_id),
                dedupe_key=dedupe,
                source_seq=seq,
            )
        ]

    if event_type in ("inbound_message", "outbound_message"):
        run_hints = None
        if run_id and event_type == "inbound_message":
            # An inbound message that carries a run id is that run's trigger.
            run_hints = RunUpsert(external_id=str(run_id), trigger=RunTrigger.MESSAGE)
        return [
            NormalizedEvent(
                ts=ts,
                kind=EventKind.MESSAGE,
                payload={
                    "direction": row.get("direction"),
                    "channel": row.get("channel"),
                    "conversation_kind": row.get("conversationKind"),
                    "outcome": row.get("outcome"),
                    "reason_code": row.get("reasonCode"),
                    "delivery_kind": row.get("deliveryKind"),
                    "duration_ms": row.get("durationMs"),
                },
                external_run_id=str(run_id) if run_id else None,
                dedupe_key=dedupe,
                source_seq=seq,
                run=run_hints,
            )
        ]

    # Unknown variants are preserved loudly rather than dropped silently.
    return [
        NormalizedEvent(
            ts=ts,
            kind=EventKind.LOG,
            payload={"unmapped_audit_event": {k: row.get(k) for k in ("eventType", "kind", "action", "status")}},
            external_run_id=str(run_id) if run_id else None,
            dedupe_key=dedupe,
            source_seq=seq,
        )
    ]


def session_lineage(sessions: list[dict[str, Any]]) -> dict[str, str]:
    """sessionKey → spawning sessionKey, from sessions.list rows
    (`spawnedBy` when present, else `parentSessionKey`)."""
    lineage: dict[str, str] = {}
    for row in sessions:
        key = row.get("sessionKey") or row.get("key")
        spawned_by = row.get("spawnedBy") or row.get("parentSessionKey")
        if key and spawned_by:
            lineage[str(key)] = str(spawned_by)
    return lineage
