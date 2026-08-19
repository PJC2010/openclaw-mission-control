"""Mission Control approval transport for Hermes Agent (§7.6).

Hermes offers a first-class slot for exactly this: `security.approval.
transport` names a plugin-registered transport, and anything other than
the literal string "builtin" in `transport_fallback` means a transport
failure denies. This plugin fills that slot by asking Mission Control,
which asks the operator.

Verified against hermes-agent 0.20.4 (2026-08-19). Four findings shape it:

1. `ApprovalRequest` carries NO session identifier. Correlation to a run
   is only possible by pairing the `pre_approval_request` hook (which
   fires on the agent-turn thread and does see session context) with the
   transport call, joined on `request_id`.

2. The transport callback runs on a bare `threading.Thread` with no
   `contextvars` copy, so reading session context *inside* `present()`
   silently yields the wrong session under concurrency. We never do it.

3. `approvals.mode: smart` runs an auxiliary-LLM guardian BEFORE the
   transport; an APPROVE verdict there returns immediately and this
   plugin never sees the request. Mission Control cannot gate what it is
   not shown, so we refuse to register unless the mode is `manual` and
   say exactly why.

4. Decisions are returned via `request.respond(choice)` with choice in
   {"once", "session", "always", "deny"}. We only ever send "once" or
   "deny": one approval, one execution (§7.6 forbids caching approvals),
   so "session"/"always" are never ours to grant.

Deliberately stdlib-only: this code runs inside the Hermes interpreter and
must not impose dependencies on it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
import uuid
from typing import Any

log = logging.getLogger("mission_control.hermes_transport")

TRANSPORT_NAME = "mission-control"

# Mission Control binds loopback only (C2); the agent runs on the same host.
DEFAULT_URL = "http://127.0.0.1:8100"

# Answer before Hermes gives up, so the operator's verdict still lands.
DEADLINE_MARGIN_S = 5.0
POLL_WAIT_S = 10.0

DECISION_ONCE = "once"
DECISION_DENY = "deny"

# request_id -> correlation captured by the pre_approval_request hook.
_correlation: dict[str, dict[str, Any]] = {}
_correlation_lock = threading.Lock()
_CORRELATION_TTL_S = 3600.0


def _config() -> tuple[str, str]:
    """(base_url, token). Token from env or a 0600 file, never inline."""
    url = os.environ.get("MISSION_CONTROL_URL", DEFAULT_URL).rstrip("/")
    token = os.environ.get("MISSION_CONTROL_TOKEN", "")
    if not token:
        path = os.environ.get(
            "MISSION_CONTROL_TOKEN_FILE", os.path.expanduser("~/.hermes/mission-control.token")
        )
        try:
            with open(path, "r", encoding="utf-8") as handle:
                token = handle.read().strip()
        except OSError:
            token = ""
    return url, token


def _remember(request_id: str, context: dict[str, Any]) -> None:
    now = time.time()
    with _correlation_lock:
        _correlation[request_id] = {"at": now, **context}
        if len(_correlation) > 512:
            for key, value in list(_correlation.items()):
                if now - value.get("at", now) > _CORRELATION_TTL_S:
                    _correlation.pop(key, None)


def _recall(request_id: str) -> dict[str, Any]:
    with _correlation_lock:
        return dict(_correlation.pop(request_id, {}))


def _post(url: str, token: str, path: str, body: dict[str, Any], timeout: float) -> dict[str, Any]:
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url + path,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + token,
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _get(url: str, token: str, path: str, timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(
        url + path, method="GET", headers={"Authorization": "Bearer " + token}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def present(request: Any) -> Any:
    """Ask Mission Control; block until a human answers or time runs out.

    Every failure path returns deny. Mission Control being unreachable,
    slow, broken, or confused must never let a gated command run (C7).
    """
    try:
        return _present_inner(request)
    except BaseException:  # noqa: BLE001 — including KeyboardInterrupt/SystemExit
        log.exception("mission control transport failed; denying")
        try:
            return request.respond(DECISION_DENY)
        except Exception:  # noqa: BLE001
            # If even respond() fails, returning the bare string is the
            # documented fallback shape and still denies.
            return DECISION_DENY


def _present_inner(request: Any) -> Any:
    url, token = _config()
    if not token:
        log.error("no Mission Control service token configured; denying")
        return request.respond(DECISION_DENY)

    request_id = str(getattr(request, "request_id", "") or uuid.uuid4().hex)
    command = str(getattr(request, "command", "") or "")
    description = str(getattr(request, "description", "") or "")
    digest = str(getattr(request, "digest", "") or "")
    pattern_key = str(getattr(request, "pattern_key", "") or "")
    allowed = tuple(getattr(request, "allowed_choices", ()) or ())
    timeout_seconds = float(getattr(request, "timeout_seconds", 300.0) or 300.0)
    surface = str(getattr(request, "surface", "") or "")

    context = _recall(request_id)
    deadline = time.monotonic() + max(1.0, timeout_seconds - DEADLINE_MARGIN_S)

    # The COMPLETE request is sent; Mission Control stores arguments whole
    # (§12 S7) and classifies risk on its own side (§7.4).
    tool_args = {
        "command": command,
        "pattern_key": pattern_key,
        "pattern_keys": list(getattr(request, "pattern_keys", ()) or ()),
        "digest": digest,
        "surface": surface,
        "allowed_choices": list(allowed),
    }
    expected_digest = _canonical_digest(tool_args)
    body = {
        # request_id is unique per approval, so it is the natural
        # idempotency key: a Hermes retry maps to one approval, not two.
        "idempotency_key": "hermes:" + request_id,
        "tool_name": context.get("tool_name") or pattern_key or "hermes.command",
        "tool_args": tool_args,
        "rationale": description,
        "run_id": context.get("session_id") or context.get("session_key"),
        "ttl_seconds": max(10, int(timeout_seconds)),
    }

    try:
        created = _post(url, token, "/v1/approvals", body, timeout=10.0)
    except urllib.error.HTTPError as exc:
        # 409 means this key was already used with different arguments —
        # a payload swap or a bug. Either way: deny (§7.3).
        log.error("mission control refused the request (HTTP %s); denying", exc.code)
        return request.respond(DECISION_DENY)
    except Exception as exc:  # noqa: BLE001
        log.error("mission control unreachable (%r); denying", exc)
        return request.respond(DECISION_DENY)

    approval_id = created.get("approval_id")
    state = created.get("state")
    if not approval_id:
        log.error("mission control returned no approval id; denying")
        return request.respond(DECISION_DENY)
    if state != "pending":
        return request.respond(_choice_for(created, allowed, expected_digest))

    poll_path = "/v1/approvals/{}/decision?wait={}".format(approval_id, int(POLL_WAIT_S))
    while time.monotonic() < deadline:
        try:
            outcome = _get(url, token, poll_path, timeout=POLL_WAIT_S + 10.0)
        except Exception as exc:  # noqa: BLE001
            log.error("polling mission control failed (%r); denying", exc)
            return request.respond(DECISION_DENY)
        if outcome.get("state") != "pending":
            return request.respond(_choice_for(outcome, allowed, expected_digest))

    log.warning("no decision before the Hermes deadline; denying")
    return request.respond(DECISION_DENY)


def _canonical_digest(args: dict[str, Any]) -> str:
    """Must match the server's canonicalisation exactly."""
    blob = json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _choice_for(
    outcome: dict[str, Any], allowed: tuple[str, ...], expected_digest: str | None = None
) -> str:
    """Translate a Mission Control outcome into a Hermes choice.

    `allowed` is honoured rather than assumed: "once" and "deny" are
    documented as always present, but we check anyway and fall back to
    denying, which is always legal.
    """
    if outcome.get("allowed") is True and outcome.get("state") == "approved":
        # Approve-then-mutate defence: the approval we are about to act on
        # must be for the arguments we actually submitted.
        returned = outcome.get("args_digest")
        if expected_digest and returned and returned != expected_digest:
            log.error(
                "SECURITY: approved arguments do not match what was submitted "
                "(expected %s, got %s); denying",
                expected_digest[:16], str(returned)[:16],
            )
            return DECISION_DENY
        if not allowed or DECISION_ONCE in allowed:
            return DECISION_ONCE
        log.error("approved, but Hermes does not offer %r (%s); denying", DECISION_ONCE, allowed)
    return DECISION_DENY


def _capture_correlation(**kwargs: Any) -> None:
    """`pre_approval_request` hook — runs on the agent-turn thread, where
    session context is actually valid."""
    try:
        surface = str(kwargs.get("surface") or "")
        # On the transport path Hermes reports surface as
        # "transport:<name>", not "gateway".
        if not surface.startswith("transport:"):
            return
        request_id = str(kwargs.get("request_id") or "")
        if not request_id:
            return
        _remember(
            request_id,
            {
                "session_key": kwargs.get("session_key"),
                "session_id": kwargs.get("session_id"),
                "turn_id": kwargs.get("turn_id"),
                "tool_call_id": kwargs.get("tool_call_id"),
                "tool_name": kwargs.get("tool_name"),
            },
        )
    except Exception:  # noqa: BLE001 — hook errors are swallowed by Hermes anyway
        log.exception("failed to capture approval correlation")


def _assert_manual_mode(ctx: Any) -> None:
    """`smart` mode can approve before the transport ever runs, which would
    make Mission Control a decoration rather than a gate."""
    mode = None
    for getter in ("get_config", "config"):
        source = getattr(ctx, getter, None)
        try:
            config = source() if callable(source) else source
            if isinstance(config, dict):
                mode = ((config.get("approvals") or {}).get("mode"))
                break
        except Exception:  # noqa: BLE001
            continue
    if mode is not None and str(mode).lower() != "manual":
        log.critical(
            "approvals.mode is %r, not 'manual'. In 'smart' mode the auxiliary "
            "LLM can approve a command before this transport is consulted, and "
            "in 'off' mode nothing is gated at all — Mission Control would show "
            "an empty queue while commands ran. Set approvals.mode: manual.",
            mode,
        )


def register(ctx: Any) -> None:
    """Plugin entry point."""
    _assert_manual_mode(ctx)
    try:
        ctx.register_hook("pre_approval_request", _capture_correlation)
    except Exception:  # noqa: BLE001 — correlation is a nicety, gating is not
        log.warning("could not register pre_approval_request hook; approvals will "
                    "still gate, but will not be linked to a run")
    ctx.register_approval_transport(TRANSPORT_NAME, present)
    url, token = _config()
    log.info(
        "mission control approval transport registered (url=%s, token=%s)",
        url, "present" if token else "MISSING — every request will be denied",
    )
