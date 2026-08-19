"""Hermes Agent adapter — incremental polling with durable cursors (§6.2)."""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import json
import logging
import uuid
from pathlib import Path

from ...config import Settings
from ...normalizer import AgentStatus, Normalizer
from ..base import AdapterHealth
from . import mapping
from .readers import (
    HermesSchemaError,
    check_executions_schema,
    check_state_schema,
    heartbeat_age_s,
    read_executions,
    read_jobs,
    read_messages_since,
    read_sessions_since,
)

log = logging.getLogger("mission_control.adapters.hermes")

HEARTBEAT_UP_THRESHOLD_S = 180.0
SESSION_OVERLAP_S = 2.0


class HermesAdapter:
    name = "hermes"

    def __init__(self, agent_id: uuid.UUID, settings: Settings, normalizer: Normalizer) -> None:
        self.agent_id = agent_id
        self._settings = settings
        self._normalizer = normalizer
        self._home = Path(settings.hermes_home).expanduser()
        self._state_db = self._home / "state.db"
        self._executions_db = self._home / "cron" / "executions.db"
        self._jobs_json = self._home / "cron" / "jobs.json"
        self._key_prefix = f"hm:{hashlib.sha256(str(self._home).encode()).hexdigest()[:10]}"
        self._task: asyncio.Task[None] | None = None
        self._health = AdapterHealth(ok=False, state="starting")
        self._schema_error_logged: str | None = None
        self._cursors: dict[str, dict] = {}

    # ── AgentAdapter protocol ────────────────────────────────────────────

    async def start(self) -> None:
        await self.load_cursors()
        self._task = asyncio.create_task(self._run(), name=f"hermes-adapter-{self.agent_id}")

    async def load_cursors(self) -> None:
        for source in ("sessions", "messages", "executions", "jobs"):
            self._cursors[source] = await self._normalizer.get_cursor(
                self.agent_id, f"hermes.{source}"
            )

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self._health = AdapterHealth(ok=False, state="stopped")

    async def backfill(self, since: datetime.datetime) -> int:
        """Rewind cursors to `since`; dedupe keys make the re-read safe."""
        self._cursors["sessions"] = {"activity": since.timestamp()}
        self._cursors["messages"] = {"rowid": 0}
        self._cursors["executions"] = {"watermark": since.isoformat(), "active": []}
        self._cursors["jobs"] = {}
        for source in ("sessions", "messages", "executions", "jobs"):
            await self._save_cursor(source)
        return await self.poll_once()

    def health(self) -> AdapterHealth:
        return self._health

    # ── internals ────────────────────────────────────────────────────────

    async def _run(self) -> None:
        while True:
            try:
                await self.poll_once()
                if self._health.state != "failed":
                    self._health = AdapterHealth(
                        ok=True,
                        state="running",
                        last_ingest_at=datetime.datetime.now(datetime.timezone.utc),
                    )
            except HermesSchemaError as exc:
                # §6.2: fail loudly, ingest nothing. Keep polling so a fixed
                # install resumes without a restart.
                if str(exc) != self._schema_error_logged:
                    log.critical("hermes schema guard tripped: %s", exc)
                    self._schema_error_logged = str(exc)
                self._health = AdapterHealth(ok=False, state="failed", detail=str(exc))
            except FileNotFoundError as exc:
                self._health = AdapterHealth(
                    ok=False, state="degraded", detail=f"hermes state not found: {exc}"
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — adapter must not die on one bad poll
                log.exception("hermes poll failed")
                self._health = AdapterHealth(ok=False, state="degraded", detail=repr(exc))
            await asyncio.sleep(self._settings.hermes_poll_interval_s)

    async def poll_once(self) -> int:
        await self._update_liveness()
        if not self._state_db.exists():
            raise FileNotFoundError(self._state_db)
        await asyncio.to_thread(
            check_state_schema, self._state_db, self._settings.hermes_expected_schema_version
        )
        self._schema_error_logged = None
        emitted = 0
        emitted += await self._poll_sessions()
        emitted += await self._poll_messages()
        if self._executions_db.exists():
            await asyncio.to_thread(check_executions_schema, self._executions_db)
            emitted += await self._poll_executions()
        if self._jobs_json.exists():
            await self._poll_jobs()
        return emitted

    async def _update_liveness(self) -> None:
        age = await asyncio.to_thread(heartbeat_age_s, self._home)
        if age is not None and age < HEARTBEAT_UP_THRESHOLD_S:
            status = AgentStatus(
                status="up",
                heartbeat_at=datetime.datetime.now(datetime.timezone.utc)
                - datetime.timedelta(seconds=age),
            )
        elif age is None:
            status = AgentStatus(status="down", detail="hermes home not readable")
        else:
            status = AgentStatus(status="down", detail=f"no daemon activity for {int(age)}s")
        await self._normalizer.set_agent_status(self.agent_id, status)

    async def _poll_sessions(self) -> int:
        floor = float(self._cursors["sessions"].get("activity") or 0.0)
        rows = await asyncio.to_thread(
            read_sessions_since, self._state_db, floor - SESSION_OVERLAP_S
        )
        if not rows:
            return 0
        events = [e for row in rows for e in mapping.session_events(row, self._key_prefix)]
        inserted = await self._normalizer.ingest(self.agent_id, events)
        newest = max(
            float(r.get("last_activity_at") or r.get("ended_at") or r.get("started_at") or 0.0)
            for r in rows
        )
        self._cursors["sessions"] = {"activity": max(floor, newest)}
        await self._save_cursor("sessions")
        return inserted

    async def _poll_messages(self) -> int:
        inserted = 0
        clip = self._settings.event_payload_text_limit
        while True:
            floor = int(self._cursors["messages"].get("rowid") or 0)
            rows = await asyncio.to_thread(read_messages_since, self._state_db, floor)
            if not rows:
                return inserted
            events = [mapping.message_event(row, self._key_prefix, clip) for row in rows]
            inserted += await self._normalizer.ingest(self.agent_id, events)
            self._cursors["messages"] = {"rowid": int(rows[-1]["id"])}
            await self._save_cursor("messages")
            if len(rows) < 2000:
                return inserted

    async def _poll_executions(self) -> int:
        cursor = self._cursors["executions"]
        watermark = cursor.get("watermark") or "1970-01-01T00:00:00"
        active = list(cursor.get("active") or [])
        rows = await asyncio.to_thread(read_executions, self._executions_db, watermark, active)
        if not rows:
            return 0
        events = [e for row in rows for e in mapping.execution_events(row, self._key_prefix)]
        inserted = await self._normalizer.ingest(self.agent_id, events)
        next_active = {
            str(row["id"]) for row in rows if (row.get("status") or "") not in mapping.EXEC_TERMINAL
        }
        # Keep previously-active ids we did not see this round (still pending).
        seen = {str(row["id"]) for row in rows}
        next_active.update(a for a in active if a not in seen)
        newest = max((row.get("claimed_at") or watermark) for row in rows)
        self._cursors["executions"] = {
            "watermark": max(watermark, newest),
            "active": sorted(next_active),
        }
        await self._save_cursor("executions")
        return inserted

    async def _poll_jobs(self) -> None:
        try:
            jobs = await asyncio.to_thread(read_jobs, self._jobs_json)
        except (json.JSONDecodeError, OSError) as exc:
            # Likely a mid-write read; the next poll retries.
            log.debug("jobs.json unreadable this poll: %s", exc)
            return
        digest = hashlib.sha256(
            json.dumps(jobs, sort_keys=True, default=str).encode()
        ).hexdigest()
        if self._cursors["jobs"].get("hash") == digest:
            return
        snapshots = [s for s in (mapping.job_snapshot(job) for job in jobs) if s is not None]
        await self._normalizer.sync_scheduled_tasks(self.agent_id, snapshots)
        self._cursors["jobs"] = {"hash": digest}
        await self._save_cursor("jobs")

    async def _save_cursor(self, source: str) -> None:
        await self._normalizer.set_cursor(self.agent_id, f"hermes.{source}", self._cursors[source])
