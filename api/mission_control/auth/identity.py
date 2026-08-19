"""Operator identity middleware — the §11.3 three-layer header-trust model.

Layer 1 — loopback bind (C2). Enforced at startup by `Settings`; re-checked
    here per request: any non-loopback peer is rejected outright, so even a
    misconfigured bind cannot silently widen access.
Layer 2 — tailnet ACL restricting who may reach port 443 on this node.
    Deployment concern; see deploy/tailscale/acl-snippet.hujson.
Layer 3 — never trust the claimed `Tailscale-User-Login` alone:
    a. the request must carry the forwarded headers Serve sets
       (X-Forwarded-For / -Proto / -Host). Identity headers without them
       mean a local process is spoofing (§17 test 6) — a loopback peer by
       itself proves nothing if some other proxy forwards to loopback.
    b. the forwarded source address is resolved through the local Tailscale
       daemon (`tailscale whois`) and must map to the *same* login the
       header claims (§17 test 7). whois failure of any kind = reject.

Everything except an explicit exempt list (liveness only) requires operator
identity. This middleware is the OPERATOR path only; the agent service-token
path (Phase 2) is a separate module by design (§7.5).
"""

from __future__ import annotations

import ipaddress
import logging
import re
from dataclasses import dataclass
from email.header import decode_header, make_header

from asyncio import timeout as asyncio_timeout

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from ..config import Settings
from .peer import PeerResolver, resolve_peer_uid
from .tailscale import WhoisResolver

security_log = logging.getLogger("mission_control.security")

HEADER_LOGIN = "tailscale-user-login"
HEADER_NAME = "tailscale-user-name"
# Serve strips these from inbound requests and re-injects its own values, so
# any occurrence we see was set by Serve itself — *if* the request came
# through Serve, which is exactly what the forwarded-header check proves.
IDENTITY_HEADERS = (HEADER_LOGIN, HEADER_NAME, "tailscale-user-profile-pic", "tailscale-app-capabilities")
# All three are set by tailscale serve on every proxied request (verified
# against Tailscale docs/examples and OpenClaw's ingress-attribution
# reference implementation (§11.4), 2026-08-18).
FORWARDED_HEADERS = ("x-forwarded-for", "x-forwarded-proto", "x-forwarded-host")
# Set on Funnel (public-internet) ingress. We never enable Funnel (C1), so
# its presence means misconfiguration or spoofing — reject either way.
HEADER_FUNNEL = "tailscale-funnel-request"

DEFAULT_EXEMPT_PATHS = frozenset({"/health"})

# The AGENT protocol endpoints (§7.1). These are exempt from operator
# identity because they use the other auth path entirely — a service token
# (§7.5) checked by the route's own dependency. They are matched by METHOD
# and path together: GET .../decision is an agent poll, while POST to the
# same path is the operator decision and must never be exempt (C4).
AGENT_ROUTES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("POST", re.compile(r"^/v1/approvals/?$")),
    ("GET", re.compile(r"^/v1/approvals/[^/]+/decision/?$")),
)


def is_agent_route(method: str, path: str) -> bool:
    return any(method == verb and pattern.match(path) for verb, pattern in AGENT_ROUTES)


@dataclass(frozen=True)
class OperatorIdentity:
    """Verified operator, attached to request.state.operator."""

    login: str          # canonical login from whois, not the raw header
    display_name: str
    source_ip: str      # tailnet address the request came from


class IdentityRejected(Exception):
    def __init__(self, status: int, detail: str, event: str, **context: object) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.event = event
        self.context = context


def _decode_rfc2047(value: str) -> str:
    """Serve Q-encodes non-ASCII header values (RFC 2047)."""
    try:
        return str(make_header(decode_header(value)))
    except Exception:  # malformed encoded-word — keep the raw value, it is inert data
        return value


def _rightmost_forwarded_ip(x_forwarded_for: str) -> str | None:
    """The rightmost X-Forwarded-For entry is the hop Serve itself appended,
    the only one not attacker-suppliable. Returns a validated bare IP."""
    parts = [p.strip() for p in x_forwarded_for.split(",") if p.strip()]
    if not parts:
        return None
    candidate = parts[-1]
    if candidate.startswith("["):  # [v6]:port
        candidate = candidate[1:].split("]", 1)[0]
    elif candidate.count(":") == 1:  # v4:port
        candidate = candidate.split(":", 1)[0]
    candidate = candidate.split("%", 1)[0]  # zone id
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return None


class OperatorIdentityMiddleware:
    """Pure ASGI middleware (no BaseHTTPMiddleware) enforcing §11.3."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        settings: Settings,
        whois_resolver: WhoisResolver,
        exempt_paths: frozenset[str] = DEFAULT_EXEMPT_PATHS,
        peer_resolver: PeerResolver | None = None,
    ) -> None:
        self.app = app
        self.settings = settings
        self.whois_resolver = whois_resolver
        self.exempt_paths = exempt_paths
        self.peer_resolver = peer_resolver or resolve_peer_uid

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if scope["path"] in self.exempt_paths:
            await self.app(scope, receive, send)
            return
        # Agent protocol endpoints authenticate via the service-token path;
        # the route dependency enforces it. Never fall through to operator
        # identity here — the two paths stay disjoint (§7.5).
        if is_agent_route(scope.get("method", ""), scope["path"]):
            await self.app(scope, receive, send)
            return

        try:
            operator = await self._authenticate(scope)
        except IdentityRejected as rejection:
            security_log.warning(
                "identity rejected: event=%s path=%s peer=%s detail=%r context=%r",
                rejection.event,
                scope.get("path"),
                (scope.get("client") or ("?",))[0],
                rejection.detail,
                rejection.context,
            )
            response = JSONResponse({"detail": rejection.detail}, status_code=rejection.status)
            await response(scope, receive, send)
            return

        scope.setdefault("state", {})
        scope["state"]["operator"] = operator
        await self.app(scope, receive, send)

    async def _authenticate(self, scope: Scope) -> OperatorIdentity:
        # Layer 1 (defense in depth): only tailscale serve, proxying over
        # loopback, may reach this app at all.
        client = scope.get("client")
        peer_ip = client[0] if client else ""
        try:
            peer_is_loopback = ipaddress.ip_address(peer_ip).is_loopback
        except ValueError:
            peer_is_loopback = False
        if not peer_is_loopback:
            raise IdentityRejected(
                403, "direct access is not permitted", "non_loopback_peer", peer=peer_ip
            )

        # Loopback alone proves nothing on a host that also runs the agents:
        # a compromised runtime can open this socket and set every header
        # Serve would have set. Ask the kernel who owns the other end —
        # `tailscale serve` proxies as root, agents do not (§11.4, C4).
        peer_port = (client[1] if client and len(client) > 1 else 0) or 0
        server = scope.get("server") or ("127.0.0.1", 0)
        server_host = str(server[0] or "127.0.0.1")
        server_port = int(server[1] or 0)
        credentials = None
        try:
            credentials = self.peer_resolver(peer_ip, int(peer_port), server_host, server_port)
        except Exception as exc:  # noqa: BLE001 — unverifiable means unsafe
            security_log.error("peer uid lookup failed: %r", exc)
        if credentials is None:
            if self.settings.require_peer_uid:
                raise IdentityRejected(
                    403,
                    "cannot verify the local origin of this connection",
                    "peer_uid_unresolved",
                    peer=f"{peer_ip}:{peer_port}",
                )
        elif credentials.uid not in self.settings.operator_uid_set:
            # This is the agent-self-approval case. Log it as loudly as the
            # header-spoofing case, because it is the same attack one layer
            # down.
            raise IdentityRejected(
                403,
                "this connection did not come through tailscale serve",
                "peer_uid_not_permitted",
                peer_uid=credentials.uid,
                permitted=sorted(self.settings.operator_uid_set),
            )

        headers = Headers(scope=scope)

        # C4 / §17 test 5: a service token must never reach an operator
        # endpoint — above all POST /v1/approvals/{id}/decision. Operator
        # auth is identity-header only and never uses bearer tokens, so a
        # bearer here is either a confused agent or an escalation attempt.
        # Either way it is a security event, and 403 (not 401) is the
        # honest answer: the credential was understood and refused.
        if headers.get("authorization") is not None:
            raise IdentityRejected(
                403,
                "service tokens cannot be used on operator endpoints",
                "service_token_on_operator_path",
                path=scope.get("path"),
                method=scope.get("method"),
            )

        if headers.get(HEADER_FUNNEL) is not None:
            raise IdentityRejected(
                403, "public (funnel) ingress is not permitted", "funnel_request_rejected"
            )

        claimed_login = headers.get(HEADER_LOGIN)
        if claimed_login is None:
            # Includes tagged devices coming through Serve (no identity
            # headers) and bare local requests. Nothing to verify → 401.
            raise IdentityRejected(
                401, "operator identity required (access via tailscale serve)", "missing_identity"
            )
        if len(headers.getlist(HEADER_LOGIN)) > 1:
            raise IdentityRejected(
                403, "conflicting identity headers", "duplicate_identity_header"
            )

        # Layer 3a: identity headers are only meaningful on a request Serve
        # proxied, and Serve always sets the forwarded trio. Missing ⇒ spoof.
        missing = [h for h in FORWARDED_HEADERS if headers.get(h) is None]
        if missing:
            raise IdentityRejected(
                403,
                "identity headers present without tailscale serve forwarding",
                "identity_without_serve_forwarding",
                missing=missing,
                claimed_login=claimed_login,
            )

        if headers.get("x-forwarded-proto", "").lower() != "https":
            raise IdentityRejected(
                403, "unexpected forwarded protocol", "non_https_forwarded_proto",
                proto=headers.get("x-forwarded-proto"),
            )

        expected_host = self.settings.expected_forwarded_host.strip().lower()
        forwarded_host = headers.get("x-forwarded-host", "").strip().lower()
        if expected_host and forwarded_host.split(":", 1)[0] != expected_host:
            raise IdentityRejected(
                403, "unexpected forwarded host", "forwarded_host_mismatch",
                forwarded_host=forwarded_host, expected=expected_host,
            )

        source_ip = _rightmost_forwarded_ip(headers.get("x-forwarded-for", ""))
        if source_ip is None:
            raise IdentityRejected(
                403, "unparseable forwarded source address", "bad_forwarded_for",
                x_forwarded_for=headers.get("x-forwarded-for"),
            )
        # A forwarded source that is itself loopback cannot be a tailnet peer;
        # OpenClaw's reference implementation rejects this case too.
        if ipaddress.ip_address(source_ip).is_loopback:
            raise IdentityRejected(
                403, "forwarded source address is not a tailnet peer", "loopback_forwarded_for",
                source_ip=source_ip,
            )

        # Layer 3b: ask tailscaled who actually owns that address. Any
        # failure — daemon down, timeout, unknown IP, tagged node — rejects.
        try:
            async with asyncio_timeout(self.settings.whois_timeout_s):
                whois = await self.whois_resolver(source_ip)
        except TimeoutError as exc:
            raise IdentityRejected(
                403, "identity verification unavailable", "whois_timeout", source_ip=source_ip
            ) from exc
        except Exception as exc:  # noqa: BLE001 — fail closed on *everything*
            raise IdentityRejected(
                403, "identity verification unavailable", "whois_error",
                source_ip=source_ip, error=repr(exc),
            ) from exc
        if whois is None:
            raise IdentityRejected(
                403, "identity verification failed", "whois_unresolved",
                source_ip=source_ip, claimed_login=claimed_login,
            )

        claimed = _decode_rfc2047(claimed_login).strip().casefold()
        verified = whois.login.strip().casefold()
        if not claimed or claimed != verified:
            raise IdentityRejected(
                403, "identity header does not match tailnet identity", "whois_login_mismatch",
                source_ip=source_ip, claimed_login=claimed_login, whois_login=whois.login,
            )

        display_name = _decode_rfc2047(headers.get(HEADER_NAME, "")) or whois.display_name
        return OperatorIdentity(
            login=whois.login, display_name=display_name, source_ip=source_ip
        )
