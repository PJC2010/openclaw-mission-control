"""Read-only access to Hermes Agent's on-disk state (§6.2).

Layout verified against hermes-agent 0.20.4 source (2026-08-18, see
docs/decisions.md A3):

    {home}/state.db              sessions + messages (WAL, schema_version 26)
    {home}/cron/jobs.json        cron job definitions (flat JSON, NOT SQLite)
    {home}/cron/executions.db    cron execution ledger (separate SQLite)
    {home}/cron/ticker_heartbeat scheduler liveness file (mtime, ~60s cadence)

Every SQLite open is `mode=ro` (never immutable — the daemon keeps
writing), we never write, lock, or VACUUM, and a schema mismatch raises
`HermesSchemaError` instead of guessing (§6.2 'fail loudly').

All functions here are synchronous and pure-ish (paths in, plain dicts
out); the adapter runs them in a worker thread.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

SESSIONS_REQUIRED = {
    "id", "source", "parent_session_id", "started_at", "ended_at", "end_reason",
    "input_tokens", "output_tokens", "estimated_cost_usd", "actual_cost_usd",
    "last_activity_at", "title",
}
MESSAGES_REQUIRED = {"id", "session_id", "role", "content", "tool_calls", "tool_name", "timestamp"}
EXECUTIONS_REQUIRED = {"id", "job_id", "status", "claimed_at", "started_at", "finished_at", "error"}


class HermesSchemaError(RuntimeError):
    """The on-disk schema does not match what this adapter was verified
    against. Ingestion must stop rather than produce garbage."""


def _connect_ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1.0)
    conn.row_factory = sqlite3.Row
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def check_state_schema(state_db: Path, expected_version: int) -> None:
    with _connect_ro(state_db) as conn:
        try:
            row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
        except sqlite3.OperationalError as exc:
            raise HermesSchemaError(f"state.db has no readable schema_version table: {exc}") from exc
        if row is None:
            raise HermesSchemaError("state.db schema_version table is empty")
        version = int(row["version"])
        if version != expected_version:
            raise HermesSchemaError(
                f"state.db schema_version={version}, expected {expected_version}. "
                "Refusing to ingest (§6.2): verify the installed hermes-agent "
                "version against docs/decisions.md A3, review upstream schema "
                "changes, then set MC_HERMES_EXPECTED_SCHEMA_VERSION deliberately."
            )
        for table, required in (("sessions", SESSIONS_REQUIRED), ("messages", MESSAGES_REQUIRED)):
            missing = required - _columns(conn, table)
            if missing:
                raise HermesSchemaError(
                    f"state.db table {table!r} is missing expected columns: {sorted(missing)}"
                )


def check_executions_schema(executions_db: Path) -> None:
    with _connect_ro(executions_db) as conn:
        missing = EXECUTIONS_REQUIRED - _columns(conn, "executions")
        if missing:
            raise HermesSchemaError(
                f"executions.db table 'executions' is missing expected columns: {sorted(missing)}"
            )


def read_sessions_since(state_db: Path, activity_floor: float) -> list[dict[str, Any]]:
    """Sessions whose activity moved past the high-water mark. Sessions
    mutate in place, so callers pass a small overlap and rely on dedupe."""
    query = """
        SELECT id, source, parent_session_id, started_at, ended_at, end_reason,
               input_tokens, output_tokens, estimated_cost_usd, actual_cost_usd,
               last_activity_at, title
        FROM sessions
        WHERE COALESCE(last_activity_at, ended_at, started_at) > ?
        ORDER BY COALESCE(last_activity_at, ended_at, started_at)
        LIMIT 1000
    """
    with _connect_ro(state_db) as conn:
        return [dict(row) for row in conn.execute(query, (activity_floor,))]


def read_messages_since(state_db: Path, rowid_floor: int, limit: int = 2000) -> list[dict[str, Any]]:
    query = """
        SELECT id, session_id, role, content, tool_calls, tool_name, timestamp
        FROM messages
        WHERE id > ?
        ORDER BY id
        LIMIT ?
    """
    with _connect_ro(state_db) as conn:
        return [dict(row) for row in conn.execute(query, (rowid_floor, limit))]


def read_executions(
    executions_db: Path, claimed_floor: str, active_ids: list[str]
) -> list[dict[str, Any]]:
    """New executions past the watermark plus known-active ones (their
    status flips in place)."""
    placeholders = ",".join("?" for _ in active_ids)
    id_clause = f"OR id IN ({placeholders})" if active_ids else ""
    query = f"""
        SELECT id, job_id, status, claimed_at, started_at, finished_at, error
        FROM executions
        WHERE claimed_at > ? {id_clause}
        ORDER BY claimed_at
        LIMIT 1000
    """
    with _connect_ro(executions_db) as conn:
        return [dict(row) for row in conn.execute(query, (claimed_floor, *active_ids))]


def read_jobs(jobs_json: Path) -> list[dict[str, Any]]:
    """Cron job definitions. The daemon rewrites this file under an
    advisory lock we do not share, so a mid-write read can fail to parse —
    callers treat that as 'retry next poll', not corruption."""
    raw = jobs_json.read_text(encoding="utf-8")
    doc = json.loads(raw)
    if isinstance(doc, dict):
        jobs = doc.get("jobs", [])
    elif isinstance(doc, list):
        jobs = doc
    else:
        raise HermesSchemaError(f"jobs.json root is {type(doc).__name__}, expected list or object")
    return [job for job in jobs if isinstance(job, dict)]


def heartbeat_age_s(home: Path) -> float | None:
    """Seconds since the cron ticker last proved the daemon alive."""
    candidates = [home / "cron" / "ticker_heartbeat", home / "state.db-wal", home / "state.db"]
    ages = []
    now = time.time()
    for path in candidates:
        try:
            ages.append(now - path.stat().st_mtime)
        except OSError:
            continue
    return min(ages) if ages else None
