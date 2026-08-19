"""OpenClaw Gateway adapter (§6.1).

Live path and backfill path are the same code: the audit ledger is polled
over the WS with a durable (sequence, watermark) cursor, so a reconnect
resumes exactly — no gap unless the ledger's own retention (30 days /
100k rows) outlived our downtime, in which case the gap window is logged
explicitly instead of silently accepted.

Liveness comes from the push side (`tick` every ~15s, `heartbeat`);
socket loss marks the agent down and the reconnect loop applies
exponential backoff with jitter, capped at 60s.
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import logging
import random
import uuid
from typing import Any

from ... import __version__
from ...config import Settings
from ...normalizer import AgentStatus, Normalizer
from ..base import AdapterHealth
from ..util import ts_from_ms
from .client import GatewayError, OpenClawClient
from .mapping import audit_row_events, session_lineage
from .protocol import GatewayEvent

log = logging.getLogger("mission_control.adapters.openclaw")

BACKOFF_BASE_S = 1.0
BACKOFF_CAP_S = 60.0  # §6.1
AUDIT_OVERLAP_MS = 5_000
AUDIT_PAGE_LIMIT = 500
SESSIONS_REFRESH_S = 60.0
# Gateway approval broadcasts are dropIfSlow and scope-narrowed, so the
# event stream is a hint, never the record: re-list on a timer as well.
APPROVAL_RECONCILE_S = 30.0
LEDGER_RETENTION = datetime.timedelta(days=30)


class OpenClawAdapter:
    name = "openclaw"

    def __init__(
        self,
        agent_id: uuid.UUID,
        settings: Settings,
        normalizer: Normalizer,
        approval_bridge: Any | None = None,
    ) -> None:
        self.agent_id = agent_id
        self._bridge = approval_bridge
        self._settings = settings
        self._normalizer = normalizer
        self._key_prefix = f"oc:{hashlib.sha256(settings.openclaw_url.encode()).hexdigest()[:10]}"
        self._task: asyncio.Task[None] | None = None
        self._health = AdapterHealth(ok=False, state="starting")
        self._cursor: dict = {}
        self._lineage: dict[str, str] = {}          # child sessionKey → parent sessionKey
        self._last_run_in_session: dict[str, str] = {}
        self._sessions_dirty = asyncio.Event()
        self._last_heartbeat_sent = 0.0

    # ── AgentAdapter protocol ────────────────────────────────────────────

    async def start(self) -> None:
        self._cursor = await self._normalizer.get_cursor(self.agent_id, "openclaw.audit")
        self._task = asyncio.create_task(self._run(), name=f"openclaw-adapter-{self.agent_id}")

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
        self._cursor = {"sequence": 0, "watermark_ms": int(since.timestamp() * 1000)}
        await self._save_cursor()
        return 0  # the running poll loop picks the rewound cursor up next pass

    def health(self) -> AdapterHealth:
        return self._health

    # ── connection loop ──────────────────────────────────────────────────

    async def _run(self) -> None:
        attempt = 0
        while True:
            scopes = ["operator.read"]
            if self._bridge is not None:
                # Only what the approval route needs; never operator.write.
                scopes.append("operator.approvals")
            client = OpenClawClient(
                self._settings.openclaw_url,
                self._settings.openclaw_token,
                client_version=__version__,
                on_event=self._on_event,
                rpc_timeout_s=self._settings.openclaw_rpc_timeout_s,
                scopes=scopes,
            )
            try:
                hello = await client.connect()
                attempt = 0
                server = hello.get("server", {})
                log.info(
                    "connected to openclaw gateway %s (protocol %s)",
                    server.get("version"),
                    hello.get("protocol"),
                )
                self._log_gap_if_cursor_stale()
                await self._normalizer.set_agent_status(
                    self.agent_id,
                    AgentStatus(status="up", heartbeat_at=self._now()),
                )
                self._health = AdapterHealth(ok=True, state="running")

                if self._bridge is not None:
                    self._bridge.attach(client)
                    # Re-checked on every connect: exec policy is editable
                    # at runtime, and a gate that can be waited out is not
                    # a gate (C7).
                    risk = await self._bridge.verify_fail_closed()
                    if risk:
                        self._health = AdapterHealth(
                            ok=False, state="degraded", detail=risk
                        )
                    mirrored = await self._bridge.bootstrap_pending()
                    if mirrored:
                        log.info("mirrored %d pending gateway approvals", mirrored)

                await self._observe(client)
            except asyncio.CancelledError:
                await client.close()
                raise
            except GatewayError as exc:
                detail = f"gateway error: {exc}"
                if exc.retryable and exc.retry_after_ms:
                    detail += f" (retry in {exc.retry_after_ms}ms)"
                log.warning("%s", detail)
                self._health = AdapterHealth(ok=False, state="degraded", detail=detail)
            except Exception as exc:  # noqa: BLE001 — reconnect on everything
                log.warning("openclaw connection lost: %r", exc)
                self._health = AdapterHealth(ok=False, state="degraded", detail=repr(exc))
            finally:
                if self._bridge is not None:
                    self._bridge.detach()
                await client.close()
            await self._normalizer.set_agent_status(
                self.agent_id, AgentStatus(status="down", detail="gateway disconnected")
            )
            attempt += 1
            delay = min(BACKOFF_CAP_S, BACKOFF_BASE_S * (2 ** min(attempt, 8)))
            delay *= 0.5 + random.random() / 2  # jitter (§6.1)
            await asyncio.sleep(delay)

    async def _observe(self, client: OpenClawClient) -> None:
        """Connected steady state: poll the audit ledger; refresh session
        lineage on change signals or every SESSIONS_REFRESH_S."""
        await self._refresh_sessions(client)
        loop = asyncio.get_running_loop()
        last_sessions_refresh = loop.time()
        last_approval_reconcile = loop.time()
        while not client.closed.is_set():
            await self._poll_audit(client)
            now = loop.time()
            if self._sessions_dirty.is_set() or now - last_sessions_refresh > SESSIONS_REFRESH_S:
                self._sessions_dirty.clear()
                await self._refresh_sessions(client)
                last_sessions_refresh = now
            if self._bridge is not None and now - last_approval_reconcile > APPROVAL_RECONCILE_S:
                last_approval_reconcile = now
                try:
                    # Two obligations, both easy to lose silently: approvals
                    # raised while we were not listening, and verdicts that
                    # never reached the gateway.
                    await self._bridge.reconcile()
                    await self._bridge.retry_undelivered()
                except Exception:  # noqa: BLE001 — never kill the socket for this
                    log.exception("approval reconciliation failed")
            try:
                await asyncio.wait_for(
                    client.closed.wait(), timeout=self._settings.openclaw_poll_interval_s
                )
            except asyncio.TimeoutError:
                pass
        raise ConnectionError("gateway socket closed")

    # ── audit ledger ingestion ───────────────────────────────────────────

    async def _poll_audit(self, client: OpenClawClient) -> int:
        stored_seq = int(self._cursor.get("sequence") or 0)
        watermark_ms = int(self._cursor.get("watermark_ms") or 0)
        params: dict = {"limit": AUDIT_PAGE_LIMIT}
        if watermark_ms:
            params["after"] = max(0, watermark_ms - AUDIT_OVERLAP_MS)
        collected: list[dict] = []
        cursor: str | None = None
        for _page in range(20):  # bound one poll's work
            page_params = dict(params)
            if cursor:
                page_params["cursor"] = cursor
            try:
                result = await client.call("audit.activity.list", page_params)
            except GatewayError as exc:
                if exc.code in ("UNKNOWN_METHOD", "NOT_FOUND") or "unknown method" in str(exc):
                    self._health = AdapterHealth(
                        ok=False,
                        state="degraded",
                        detail=(
                            "gateway does not advertise audit.activity.list — "
                            "older OpenClaw than verified (2026.7.x). Run the "
                            "decisions.md A4 version check before Phase 1 use."
                        ),
                    )
                    return 0
                raise
            rows = result.get("events") or []
            fresh = [r for r in rows if int(r.get("sequence") or 0) > stored_seq]
            collected.extend(fresh)
            cursor = result.get("nextCursor")
            # Newest-first pages: stop once a page brought nothing new.
            if not cursor or len(fresh) < len(rows) or not rows:
                break
        if not collected:
            return 0
        events = []
        for row in sorted(collected, key=lambda r: int(r.get("sequence") or 0)):
            events.extend(
                audit_row_events(row, self._key_prefix, self._lineage, self._last_run_in_session)
            )
        inserted = await self._normalizer.ingest(self.agent_id, events)
        max_seq = max(int(r.get("sequence") or 0) for r in collected)
        max_ms = max(
            int(r["occurredAt"]) if isinstance(r.get("occurredAt"), (int, float)) else watermark_ms
            for r in collected
        )
        self._cursor = {"sequence": max(stored_seq, max_seq), "watermark_ms": max(watermark_ms, max_ms)}
        await self._save_cursor()
        if inserted:
            self._health = AdapterHealth(ok=True, state="running", last_ingest_at=self._now())
        return inserted

    async def _refresh_sessions(self, client: OpenClawClient) -> None:
        try:
            result = await client.call("sessions.list", {})
        except GatewayError as exc:
            log.warning("sessions.list failed: %s", exc)
            return
        rows = result.get("sessions") or result.get("items") or []
        if isinstance(rows, list):
            self._lineage = session_lineage([r for r in rows if isinstance(r, dict)])

    def _log_gap_if_cursor_stale(self) -> None:
        """§6.1: if the ledger's retention outlived our cursor, backfill is
        impossible — log the gap window instead of pretending."""
        watermark_ms = int(self._cursor.get("watermark_ms") or 0)
        if not watermark_ms:
            return
        watermark = ts_from_ms(watermark_ms)
        if watermark and self._now() - watermark > LEDGER_RETENTION:
            log.error(
                "openclaw audit cursor (%s) is older than the gateway's %s ledger "
                "retention — events in the gap window [%s .. retention edge] are lost",
                watermark.isoformat(),
                LEDGER_RETENTION,
                watermark.isoformat(),
            )

    # ── push-side events ─────────────────────────────────────────────────

    async def _on_event(self, event: GatewayEvent) -> None:
        if event.event in ("tick", "heartbeat", "health"):
            loop_now = asyncio.get_running_loop().time()
            if loop_now - self._last_heartbeat_sent > 10.0:
                self._last_heartbeat_sent = loop_now
                await self._normalizer.set_agent_status(
                    self.agent_id, AgentStatus(status="up", heartbeat_at=self._now())
                )
        elif event.event in ("sessions.changed", "presence"):
            self._sessions_dirty.set()
        elif event.event in (
            "exec.approval.requested",
            "plugin.approval.requested",
            "openclaw.approval.requested",
        ):
            if self._bridge is not None:
                await self._bridge.handle_requested(event.payload)
        elif event.event in (
            "exec.approval.resolved",
            "plugin.approval.resolved",
            "openclaw.approval.resolved",
        ):
            # Someone (or something) else answered. Our own resolve is
            # race-safe, so this is informational.
            log.info(
                "gateway reported approval %s resolved as %s",
                event.payload.get("id"), event.payload.get("decision"),
            )
        elif event.event == "shutdown":
            await self._normalizer.set_agent_status(
                self.agent_id, AgentStatus(status="down", detail="gateway announced shutdown")
            )
        # chat/agent stream deltas are deliberately not persisted in Phase 1
        # (metadata-only OpenClaw observation; see docs/decisions.md P1-D2).

    async def _save_cursor(self) -> None:
        await self._normalizer.set_cursor(self.agent_id, "openclaw.audit", self._cursor)

    @staticmethod
    def _now() -> datetime.datetime:
        return datetime.datetime.now(datetime.timezone.utc)
