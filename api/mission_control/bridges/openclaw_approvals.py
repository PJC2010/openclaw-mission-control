"""OpenClaw approval bridge — the RPC route.

Rather than wrapping tool calls with a plugin hook (whose approval timeout
is capped at 10 minutes, below our 15-minute TTL), Mission Control joins
the gateway as an operator client holding `operator.approvals`, mirrors
every raised approval into our own queue, and resolves it back over the
same socket once the operator decides.

Verified against OpenClaw 2026.7.x source and docs (2026-08-19). Three
findings shape this code, and each is load-bearing:

1. `approval.resolve` FAILS CLOSED BY DENYING. A wrong `kind`, or a
   decision outside that record's `allowedDecisions`, does not return an
   error — it commits a deny with reason "malformed-verdict". A client bug
   therefore silently kills the agent's command. So we always
   `approval.get` first and echo the record's own `presentation.kind`
   back, and validate the decision against `allowedDecisions`, rather than
   guessing either.

2. Host `askFallback` can make OpenClaw FAIL OPEN. With
   `askFallback: "full"` (and effective host security "full"), a null
   decision — timeout, expiry, or no route — returns `approvedByAsk: true`
   and the command RUNS. Default is "deny". We assert it at every scope on
   connect and refuse to report healthy otherwise: a gate that can be
   bypassed by waiting is not a gate (C7).

3. Decisions are exactly `allow-once` | `allow-always` | `deny`. We never
   send `allow-always` — §7.6 is explicit that one approval means one
   execution, and standing permission is not ours to grant.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import uuid
from typing import Any

from ..approvals.service import ApprovalService, ApprovalSnapshot
from ..config import Settings
from ..models.enums import ApprovalState
from ..adapters.openclaw.client import GatewayError
from ..normalizer import Normalizer

log = logging.getLogger("mission_control.bridges.openclaw")
security_log = logging.getLogger("mission_control.security")

DECISION_ALLOW_ONCE = "allow-once"
DECISION_DENY = "deny"

# Leave room to answer before the runtime's own deadline lapses: if the
# gateway expires the request first, our verdict lands on a dead record.
DEADLINE_MARGIN_S = 20.0

# Gateway error codes meaning "this record definitively does not exist",
# as opposed to "I could not reach or read it".
NOT_FOUND_CODES = frozenset({"NOT_FOUND", "UNKNOWN_APPROVAL", "NO_SUCH_APPROVAL"})


class OpenClawApprovalBridge:
    """Mirrors gateway approvals into Mission Control and resolves them back."""

    def __init__(
        self,
        agent_id: uuid.UUID,
        settings: Settings,
        approvals: ApprovalService,
        normalizer: Normalizer | None = None,
    ) -> None:
        self.agent_id = agent_id
        self._settings = settings
        self._approvals = approvals
        self._normalizer = normalizer
        self._client: Any | None = None
        self._fail_open_risk: str | None = None
        approvals.register_decision_hook(self._on_decision)

    # ── wiring from the adapter ──────────────────────────────────────────

    def attach(self, client: Any) -> None:
        """Called by the adapter with its live, authenticated client."""
        self._client = client

    def detach(self) -> None:
        self._client = None

    @property
    def fail_open_risk(self) -> str | None:
        """Non-None when the gateway is configured such that a missed
        decision could still allow the action."""
        return self._fail_open_risk

    # ── startup safety check ─────────────────────────────────────────────

    async def verify_fail_closed(self) -> str | None:
        """Assert `askFallback == "deny"` at every scope.

        Returns None when safe, else a human-readable reason. This is
        checked on every (re)connect because the operator can change exec
        policy at runtime.
        """
        if self._client is None:
            return "not connected"
        try:
            policy = await self._client.call("exec.approvals.get", {})
        except Exception as exc:  # noqa: BLE001 — unverifiable == unsafe
            reason = f"could not read exec approval policy ({exc!r})"
            self._fail_open_risk = reason
            return reason

        offenders: list[str] = []
        defaults = policy.get("defaults") if isinstance(policy.get("defaults"), dict) else {}
        if defaults.get("askFallback") not in (None, "deny"):
            offenders.append(f"defaults.askFallback={defaults.get('askFallback')!r}")
        agents = policy.get("agents") if isinstance(policy.get("agents"), dict) else {}
        for agent_key, entry in agents.items():
            if isinstance(entry, dict) and entry.get("askFallback") not in (None, "deny"):
                offenders.append(f"agents.{agent_key}.askFallback={entry.get('askFallback')!r}")

        if offenders:
            reason = (
                "OpenClaw exec policy can FAIL OPEN: " + ", ".join(offenders) +
                ". With askFallback other than 'deny', a timed-out or unrouted "
                "approval can still execute, which defeats the gate (C7). Fix with: "
                "openclaw exec-policy set --ask-fallback deny"
            )
            security_log.critical("%s", reason)
            self._fail_open_risk = reason
            await self._alert("openclaw_fail_open_risk", {"offenders": offenders})
            return reason

        self._fail_open_risk = None
        log.info("openclaw exec policy verified fail-closed (askFallback=deny at all scopes)")
        return None

    # ── inbound: gateway raised an approval ──────────────────────────────

    async def bootstrap_pending(self) -> int:
        """Mirror approvals already pending at connect time.

        Both list methods take no params and return a BARE ARRAY (not
        {items: []}) — tolerate either shape rather than assume.
        """
        if self._client is None:
            return 0
        mirrored = 0
        for method in ("exec.approval.list", "plugin.approval.list"):
            try:
                result = await self._client.call(method, {})
            except Exception as exc:  # noqa: BLE001 — one family missing is survivable
                log.warning("%s unavailable: %r", method, exc)
                continue
            rows = result if isinstance(result, list) else (result or {}).get("approvals") or []
            if isinstance(result, dict) and not rows:
                for value in result.values():
                    if isinstance(value, list):
                        rows = value
                        break
            for row in rows:
                if isinstance(row, dict):
                    payload = row if "request" in row else {"id": row.get("id"), "request": row}
                    if await self.handle_requested(payload):
                        mirrored += 1
        return mirrored

    async def handle_requested(self, event_payload: dict[str, Any]) -> bool:
        """Mirror one `*.approval.requested` event into our queue."""
        approval_id = event_payload.get("id")
        if not approval_id:
            log.warning("approval event without an id: %r", list(event_payload)[:6])
            return False

        # Never guess the kind — resolving with the wrong one force-denies.
        try:
            record = await self._get_record(str(approval_id))
        except Exception as exc:  # noqa: BLE001 — mirror on the event alone
            log.warning("approval.get(%s) unavailable while mirroring: %r", approval_id, exc)
            record = None
        presentation = (record or {}).get("presentation") or {}
        request = event_payload.get("request")
        if not isinstance(request, dict):
            request = presentation.get("request") if isinstance(presentation.get("request"), dict) else {}

        tool_name = (
            presentation.get("toolName")
            or request.get("toolName")
            or request.get("tool")
            or ("exec" if request.get("command") else None)
            or presentation.get("title")
            or "openclaw.approval"
        )
        rationale = str(
            presentation.get("description") or request.get("description") or request.get("reason") or ""
        )

        # The complete request payload is the argument set (§12 S7).
        tool_args: dict[str, Any] = {"request": request}
        if presentation:
            tool_args["presentation"] = {
                key: presentation.get(key)
                for key in ("kind", "title", "severity", "allowedDecisions", "urlPath")
                if key in presentation
            }

        ttl_seconds = self._ttl_from_deadline(event_payload.get("expiresAtMs"))

        try:
            snapshot, created = await self._approvals.create(
                agent_id=self.agent_id,
                idempotency_key=f"openclaw:{approval_id}",
                tool_name=str(tool_name),
                tool_args=tool_args,
                rationale=rationale,
                external_run_id=str(request.get("sessionKey") or request.get("runId") or "") or None,
                source="openclaw_rpc",
                external_ref=str(approval_id),
                ttl_seconds=ttl_seconds,
                actor="openclaw-gateway",
            )
        except Exception:  # noqa: BLE001 — mirroring must not kill the socket
            log.exception("failed to mirror openclaw approval %s", approval_id)
            return False

        # A policy auto-decision (or kill switch / rate limit) resolves
        # immediately; the hook has already fired for those.
        if created and snapshot.state is ApprovalState.PENDING:
            log.info("mirrored openclaw approval %s as %s", approval_id, snapshot.id)
        return created

    def _ttl_from_deadline(self, expires_at_ms: Any) -> int | None:
        """Clamp our TTL to the runtime's own deadline.

        Our default (15 min) is already shorter than OpenClaw's 30-minute
        exec timeout, but plugin approvals cap at 10 minutes — expiring
        after the gateway already gave up would leave the operator
        deciding a dead record.
        """
        if not isinstance(expires_at_ms, (int, float)):
            return None
        remaining = expires_at_ms / 1000.0 - datetime.datetime.now(datetime.timezone.utc).timestamp()
        budget = int(remaining - DEADLINE_MARGIN_S)
        if budget <= 0:
            return 10  # already effectively dead; expire promptly
        return min(self._settings.approval_ttl_seconds, budget)

    # ── outbound: operator decided ───────────────────────────────────────

    async def _on_decision(self, snapshot: ApprovalSnapshot) -> None:
        """Carry a decision back to the gateway (registered as a hook)."""
        await self.deliver(snapshot)

    async def deliver(self, snapshot: ApprovalSnapshot) -> bool:
        """Deliver one verdict. Returns True once the gateway has it.

        Failure is recorded rather than logged and dropped: an undelivered
        verdict means the gateway is still waiting and will fall back to
        its own timeout behaviour, which is not the operator's decision.
        """
        if snapshot.source != "openclaw_rpc" or not snapshot.external_ref:
            return True
        if snapshot.state is ApprovalState.PENDING:
            return False
        if self._client is None:
            await self._approvals.mark_resolution(
                snapshot.id, confirmed=False, error="not connected to the gateway"
            )
            return False

        try:
            record = await self._get_record(snapshot.external_ref)
        except Exception as exc:  # noqa: BLE001 — unreadable, so still owed
            await self._approvals.mark_resolution(
                snapshot.id, confirmed=False, error=f"approval.get failed: {exc!r}"
            )
            return False
        if record is None:
            # Only a positive not-found reaches here: `*.approval.get`
            # resolves pending records only, so the gateway has already
            # reached a terminal state and there is nothing left to deliver.
            log.info(
                "openclaw approval %s reported absent; already terminal",
                snapshot.external_ref,
            )
            await self._approvals.mark_resolution(snapshot.id, confirmed=True)
            return True
        presentation = record.get("presentation") or {}
        request_payload = record.get("request") if isinstance(record.get("request"), dict) else {}
        kind = presentation.get("kind") or record.get("kind") or record.get("approvalKind")
        # allowedDecisions appears on the request payload for both exec and
        # plugin approvals, and on the kind-agnostic presentation. Prefer
        # whichever is actually present rather than assuming a shape.
        allowed = (
            presentation.get("allowedDecisions")
            or record.get("allowedDecisions")
            or request_payload.get("allowedDecisions")
        )
        allowed_set = set(allowed) if isinstance(allowed, list) else None

        want_allow = snapshot.state is ApprovalState.APPROVED
        decision = DECISION_ALLOW_ONCE if want_allow else DECISION_DENY
        if want_allow and allowed_set is not None and DECISION_ALLOW_ONCE not in allowed_set:
            # Sending an unlisted decision would force a malformed-deny
            # anyway; denying deliberately is at least honest and logged.
            security_log.error(
                "openclaw approval %s does not allow %s (allowed=%s) — denying instead",
                snapshot.external_ref, DECISION_ALLOW_ONCE, sorted(allowed_set),
            )
            decision = DECISION_DENY

        params: dict[str, Any] = {"id": snapshot.external_ref, "decision": decision}
        if kind:
            params["kind"] = kind  # echoed back, never guessed
        try:
            result = await self._client.call("approval.resolve", params)
        except Exception as exc:  # noqa: BLE001
            await self._approvals.mark_resolution(
                snapshot.id, confirmed=False, error=repr(exc)
            )
            return False
        applied = result.get("applied") if isinstance(result, dict) else None
        # `applied: false` means someone answered first — the record is
        # terminal either way, so the obligation is discharged.
        log.info(
            "resolved openclaw approval %s as %s (applied=%s)",
            snapshot.external_ref, decision, applied,
        )
        await self._approvals.mark_resolution(snapshot.id, confirmed=True)
        return True

    async def retry_undelivered(self) -> int:
        """Re-attempt verdicts the gateway has not confirmed."""
        if self._client is None:
            return 0
        delivered = 0
        for snapshot in await self._approvals.undelivered():
            if snapshot.source != "openclaw_rpc":
                continue
            if await self.deliver(snapshot):
                delivered += 1
        return delivered

    async def reconcile(self) -> int:
        """Re-list pending gateway approvals and mirror anything missed.

        Broadcasts are best-effort — the gateway drops them for slow
        subscribers — so the event stream is never treated as the record.
        A restart on either side also diverges the two views.
        """
        return await self.bootstrap_pending()

    async def _get_record(self, approval_id: str) -> dict[str, Any] | None:
        """The record, or None only when the gateway positively says it is
        gone. Raises on anything else — "I could not read it" must never be
        mistaken for "it is already resolved"."""
        if self._client is None:
            raise ConnectionError("not connected to the gateway")
        try:
            record = await self._client.call("approval.get", {"id": approval_id})
        except GatewayError as exc:
            if exc.code in NOT_FOUND_CODES:
                return None  # definitively absent: already terminal
            raise
        if isinstance(record, dict):
            return record.get("approval") if isinstance(record.get("approval"), dict) else record
        return None

    async def _alert(self, kind: str, payload: dict[str, Any]) -> None:
        if self._normalizer is None:
            return
        try:
            from ..models.enums import EventKind
            from ..normalizer import NormalizedEvent

            await self._normalizer.ingest(
                self.agent_id,
                [
                    NormalizedEvent(
                        ts=datetime.datetime.now(datetime.timezone.utc),
                        kind=EventKind.ERROR,
                        payload={"alert": kind, **payload},
                    )
                ],
            )
        except Exception:  # noqa: BLE001
            log.exception("failed to raise %s alert", kind)
