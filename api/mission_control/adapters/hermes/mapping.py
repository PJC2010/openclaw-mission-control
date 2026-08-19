"""Hermes rows → normalized contract (§6.3).

Dedupe keys are stable per source row/state so overlapping re-reads and
backfills collapse; run hints ride on the events and are applied even when
the event itself dedupes away (the normalizer applies hints first).
"""

from __future__ import annotations

import datetime
import json
from typing import Any

from ...models.enums import EventKind, RunStatus, RunTrigger
from ...normalizer.contract import NormalizedEvent, RunUpsert, ScheduledTaskSnapshot
from ..util import clipped_text, ts_from_epoch, ts_from_iso

_FAILED_MARKERS = ("error", "fail", "crash", "exception")
_CANCELLED_MARKERS = ("cancel", "abort", "interrupt")


def _session_status(row: dict[str, Any]) -> RunStatus:
    if row.get("ended_at") is None:
        return RunStatus.RUNNING
    reason = (row.get("end_reason") or "").lower()
    if any(marker in reason for marker in _CANCELLED_MARKERS):
        return RunStatus.CANCELLED
    if any(marker in reason for marker in _FAILED_MARKERS):
        return RunStatus.FAILED
    return RunStatus.SUCCEEDED


def _session_trigger(row: dict[str, Any]) -> RunTrigger:
    if row.get("parent_session_id"):
        return RunTrigger.SUBAGENT
    source = (row.get("source") or "").lower()
    if source == "cron":
        return RunTrigger.SCHEDULE
    if source in ("cli", "local", "manual"):
        return RunTrigger.MANUAL
    return RunTrigger.MESSAGE


def session_run_upsert(row: dict[str, Any]) -> RunUpsert:
    status = _session_status(row)
    cost = row.get("actual_cost_usd")
    if cost is None:
        cost = row.get("estimated_cost_usd")
    return RunUpsert(
        external_id=str(row["id"]),
        started_at=ts_from_epoch(row.get("started_at")),
        ended_at=ts_from_epoch(row.get("ended_at")),
        status=status,
        trigger=_session_trigger(row),
        parent_external_id=str(row["parent_session_id"]) if row.get("parent_session_id") else None,
        error_summary=(row.get("end_reason") if status is RunStatus.FAILED else None),
        token_input=row.get("input_tokens"),
        token_output=row.get("output_tokens"),
        cost_usd=cost,
    )


def session_events(row: dict[str, Any], key_prefix: str) -> list[NormalizedEvent]:
    run = session_run_upsert(row)
    session_id = str(row["id"])
    started = ts_from_epoch(row.get("started_at")) or datetime.datetime.now(datetime.timezone.utc)
    events = [
        NormalizedEvent(
            ts=started,
            kind=EventKind.LIFECYCLE,
            payload={
                "transition": "run_started",
                "source": row.get("source"),
                "title": row.get("title"),
            },
            external_run_id=session_id,
            dedupe_key=f"{key_prefix}:sess:{session_id}:start",
            run=run,
        )
    ]
    ended = ts_from_epoch(row.get("ended_at"))
    if ended is not None:
        events.append(
            NormalizedEvent(
                ts=ended,
                kind=EventKind.LIFECYCLE,
                payload={
                    "transition": "run_ended",
                    "status": (run.status or RunStatus.SUCCEEDED).value,
                    "end_reason": row.get("end_reason"),
                },
                external_run_id=session_id,
                dedupe_key=f"{key_prefix}:sess:{session_id}:end",
                run=run,
            )
        )
    return events


def message_event(row: dict[str, Any], key_prefix: str, clip: int) -> NormalizedEvent:
    role = (row.get("role") or "").lower()
    payload: dict[str, Any] = {"role": role}
    kind = EventKind.MESSAGE
    if role == "tool":
        kind = EventKind.TOOL_RESULT
        payload["tool_name"] = row.get("tool_name")
        payload["content"] = clipped_text(row.get("content"), clip)
    elif row.get("tool_calls"):
        kind = EventKind.TOOL_CALL
        raw_calls = row.get("tool_calls")
        try:
            calls = json.loads(raw_calls) if isinstance(raw_calls, str) else raw_calls
            payload["tool_calls"] = [
                {
                    "name": (call.get("function") or {}).get("name") or call.get("name"),
                    "arguments": clipped_text(
                        json.dumps((call.get("function") or {}).get("arguments") or call.get("arguments") or {})
                        if not isinstance((call.get("function") or {}).get("arguments"), str)
                        else (call.get("function") or {}).get("arguments"),
                        clip,
                    ),
                }
                for call in calls
                if isinstance(call, dict)
            ]
        except (ValueError, TypeError, AttributeError):
            payload["tool_calls_raw"] = clipped_text(str(raw_calls), clip)
    else:
        payload["content"] = clipped_text(row.get("content"), clip)
    return NormalizedEvent(
        ts=ts_from_epoch(row.get("timestamp")) or datetime.datetime.now(datetime.timezone.utc),
        kind=kind,
        payload=payload,
        external_run_id=str(row["session_id"]),
        dedupe_key=f"{key_prefix}:msg:{row['id']}",
        source_seq=int(row["id"]),
    )


_EXEC_STATUS = {
    "claimed": RunStatus.RUNNING,
    "running": RunStatus.RUNNING,
    "completed": RunStatus.SUCCEEDED,
    "failed": RunStatus.FAILED,
    # 'unknown' is the ledger's own "daemon lost track of this attempt".
    "unknown": RunStatus.FAILED,
}
EXEC_TERMINAL = {"completed", "failed", "unknown"}


def execution_events(row: dict[str, Any], key_prefix: str) -> list[NormalizedEvent]:
    status = (row.get("status") or "unknown").lower()
    run_status = _EXEC_STATUS.get(status, RunStatus.FAILED)
    external_id = f"cron:{row['id']}"
    error = row.get("error")
    run = RunUpsert(
        external_id=external_id,
        started_at=ts_from_iso(row.get("started_at")) or ts_from_iso(row.get("claimed_at")),
        ended_at=ts_from_iso(row.get("finished_at")),
        status=run_status,
        trigger=RunTrigger.SCHEDULE,
        error_summary=(
            error or ("execution outcome unknown — daemon lost track" if status == "unknown" else None)
        )
        if run_status is RunStatus.FAILED
        else None,
    )
    ts = (
        ts_from_iso(row.get("finished_at"))
        or ts_from_iso(row.get("started_at"))
        or ts_from_iso(row.get("claimed_at"))
        or datetime.datetime.now(datetime.timezone.utc)
    )
    return [
        NormalizedEvent(
            ts=ts,
            kind=EventKind.LIFECYCLE,
            payload={
                "transition": "cron_execution",
                "status": status,
                "job_id": row.get("job_id"),
                "error": error,
            },
            external_run_id=external_id,
            dedupe_key=f"{key_prefix}:exec:{row['id']}:{status}",
            run=run,
        )
    ]


def job_snapshot(job: dict[str, Any]) -> ScheduledTaskSnapshot | None:
    job_id = job.get("id")
    if job_id is None:
        return None
    schedule = job.get("schedule") if isinstance(job.get("schedule"), dict) else {}
    cron_expr = None
    if schedule.get("kind") == "cron":
        cron_expr = schedule.get("expr") or schedule.get("cron") or schedule.get("expression")
    elif schedule.get("kind") == "interval" and schedule.get("minutes"):
        cron_expr = None  # interval jobs have no cron expression; next_fire_at carries truth
    return ScheduledTaskSnapshot(
        external_id=str(job_id),
        name=str(job.get("name") or job_id),
        cron_expr=cron_expr,
        timezone="UTC",
        next_fire_at=ts_from_iso(job.get("next_run_at")),
        last_outcome=job.get("last_status"),
        consecutive_failures=int(job.get("failure_streak") or 0),
        enabled=bool(job.get("enabled", True)),
        writeback_supported=False,  # Phase 4 decides write-back (§18.6)
    )
