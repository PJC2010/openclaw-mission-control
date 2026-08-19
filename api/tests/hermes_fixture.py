"""Builds a miniature ~/.hermes on disk matching the verified 0.20.4 layout
(docs/decisions.md A3): state.db (schema_version 26), cron/jobs.json,
cron/executions.db, cron/ticker_heartbeat."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

STATE_SCHEMA = """
CREATE TABLE schema_version (version INTEGER NOT NULL);
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    user_id TEXT,
    parent_session_id TEXT,
    started_at REAL NOT NULL,
    ended_at REAL,
    end_reason TEXT,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    estimated_cost_usd REAL,
    actual_cost_usd REAL,
    last_activity_at REAL,
    title TEXT
);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT,
    tool_calls TEXT,
    tool_name TEXT,
    timestamp REAL NOT NULL
);
"""

EXECUTIONS_SCHEMA = """
CREATE TABLE executions (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'cron',
    process_id TEXT NOT NULL DEFAULT 'p1',
    pid INTEGER NOT NULL DEFAULT 1,
    process_started_at INTEGER,
    status TEXT NOT NULL,
    claimed_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    error TEXT
);
"""


def build_home(root: Path, schema_version: int = 26, now: float | None = None) -> Path:
    now = now or time.time()
    home = root / ".hermes"
    (home / "cron").mkdir(parents=True)

    # NOTE: sqlite3's context manager commits but does NOT close; an unclosed
    # WAL connection checkpoints at GC time and re-touches state.db's mtime,
    # breaking heartbeat-age tests. Close explicitly.
    conn = sqlite3.connect(home / "state.db")
    try:
        conn.executescript(STATE_SCHEMA)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (schema_version,))
        conn.execute(
            "INSERT INTO sessions (id, source, started_at, ended_at, end_reason, input_tokens,"
            " output_tokens, actual_cost_usd, last_activity_at, title)"
            " VALUES ('s-done','telegram',?,?,'completed',1200,340,0.031,?,'ask about invoices')",
            (now - 600, now - 300, now - 300),
        )
        conn.execute(
            "INSERT INTO sessions (id, source, started_at, last_activity_at, title)"
            " VALUES ('s-live','cron',?,?,'nightly digest')",
            (now - 60, now - 5),
        )
        conn.execute(
            "INSERT INTO sessions (id, source, parent_session_id, started_at, last_activity_at)"
            " VALUES ('s-sub','cron','s-live',?,?)",
            (now - 30, now - 4),
        )
        conn.executemany(
            "INSERT INTO messages (session_id, role, content, tool_calls, tool_name, timestamp)"
            " VALUES (?,?,?,?,?,?)",
            [
                ("s-done", "user", "summarize my invoices", None, None, now - 590),
                (
                    "s-done",
                    "assistant",
                    None,
                    json.dumps(
                        [{"function": {"name": "web_search", "arguments": '{"q": "invoices"}'}}]
                    ),
                    None,
                    now - 580,
                ),
                ("s-done", "tool", "3 invoices found", None, "web_search", now - 570),
                ("s-done", "assistant", "You have 3 open invoices.", None, None, now - 560),
                ("s-live", "assistant", "digest running", None, None, now - 10),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    conn = sqlite3.connect(home / "cron" / "executions.db")
    try:
        conn.executescript(EXECUTIONS_SCHEMA)
        base = now - 120
        iso = lambda offset: time.strftime(  # noqa: E731
            "%Y-%m-%dT%H:%M:%S", time.gmtime(base + offset)
        )
        conn.execute(
            "INSERT INTO executions (id, job_id, status, claimed_at, started_at, finished_at)"
            " VALUES ('e-ok','job-digest','completed',?,?,?)",
            (iso(0), iso(1), iso(30)),
        )
        conn.execute(
            "INSERT INTO executions (id, job_id, status, claimed_at, started_at, error)"
            " VALUES ('e-run','job-digest','running',?,?,NULL)",
            (iso(60), iso(61)),
        )
        conn.commit()
    finally:
        conn.close()

    (home / "cron" / "jobs.json").write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "id": "job-digest",
                        "name": "nightly digest",
                        "schedule": {"kind": "cron", "expr": "0 6 * * *"},
                        "enabled": True,
                        "next_run_at": "2026-08-20T06:00:00+00:00",
                        "last_status": "success",
                        "failure_streak": 0,
                    },
                    {
                        "id": "job-poll",
                        "name": "inbox poll",
                        "schedule": {"kind": "interval", "minutes": 15},
                        "enabled": True,
                        "next_run_at": "2026-08-19T12:15:00+00:00",
                        "last_status": "success",
                        "failure_streak": 2,
                    },
                ]
            }
        )
    )
    (home / "cron" / "ticker_heartbeat").touch()
    return home
