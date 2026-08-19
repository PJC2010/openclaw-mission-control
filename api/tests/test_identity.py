"""§11.3 identity middleware — every trust layer, fail-closed everywhere.

Spec acceptance tests covered here:
  - §17 test 6: identity header without Serve's forwarded headers → rejected
  - §17 test 7: whois login differing from the header claim → rejected
"""

from __future__ import annotations

import asyncio

import pytest

from mission_control.auth.tailscale import WhoisIdentity

from .conftest import (
    OPERATOR_LOGIN,
    TAILNET_IP,
    build_app,
    client_for,
    serve_headers,
)

pytestmark = pytest.mark.anyio


# ── §17 test 6 ───────────────────────────────────────────────────────────


async def test_identity_header_without_serve_forwarding_rejected():
    """A local process setting Tailscale-User-Login but lacking the
    forwarded headers Serve always adds is spoofing — reject."""
    async with client_for(build_app()) as client:
        response = await client.get("/", headers={"Tailscale-User-Login": OPERATOR_LOGIN})
    assert response.status_code == 403


@pytest.mark.parametrize("dropped", ["X-Forwarded-For", "X-Forwarded-Proto", "X-Forwarded-Host"])
async def test_identity_header_with_partial_forwarding_rejected(dropped: str):
    headers = serve_headers()
    del headers[dropped]
    async with client_for(build_app()) as client:
        response = await client.get("/", headers=headers)
    assert response.status_code == 403


# ── §17 test 7 ───────────────────────────────────────────────────────────


async def test_whois_login_mismatch_rejected():
    """The header claims the operator, but tailscaled says the source
    address belongs to someone else → reject."""

    async def wrong_user(_ip: str) -> WhoisIdentity:
        return WhoisIdentity(login="mallory@example.com")

    async with client_for(build_app(resolver=wrong_user)) as client:
        response = await client.get("/", headers=serve_headers())
    assert response.status_code == 403


# ── Happy path ───────────────────────────────────────────────────────────


async def test_valid_serve_request_authenticates(operator_client):
    response = await operator_client.get("/v1/whoami", headers=serve_headers())
    assert response.status_code == 200
    body = response.json()
    assert body["login"] == OPERATOR_LOGIN
    assert body["source_ip"] == TAILNET_IP


async def test_login_comparison_is_case_insensitive(operator_client):
    response = await operator_client.get(
        "/v1/whoami", headers=serve_headers(login=OPERATOR_LOGIN.upper())
    )
    assert response.status_code == 200
    # Canonical identity comes from whois, not the header.
    assert response.json()["login"] == OPERATOR_LOGIN


async def test_hello_page_serves_html(operator_client):
    response = await operator_client.get("/", headers=serve_headers())
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Mission Control" in response.text


async def test_multi_hop_xff_uses_rightmost_entry(operator_client):
    """Only the rightmost X-Forwarded-For entry was appended by Serve;
    anything left of it is attacker-suppliable."""
    response = await operator_client.get(
        "/v1/whoami",
        headers=serve_headers() | {"X-Forwarded-For": f"6.6.6.6, {TAILNET_IP}"},
    )
    assert response.status_code == 200
    assert response.json()["source_ip"] == TAILNET_IP


async def test_multi_hop_xff_spoofed_rightmost_rejected():
    """Rightmost entry not resolvable by whois → reject, even though an
    earlier entry names a legitimate tailnet address."""
    async with client_for(build_app()) as client:
        response = await client.get(
            "/v1/whoami",
            headers=serve_headers() | {"X-Forwarded-For": f"{TAILNET_IP}, 203.0.113.9"},
        )
    assert response.status_code == 403


# ── Fail-closed verification paths ───────────────────────────────────────


async def test_no_identity_headers_is_unauthenticated():
    async with client_for(build_app()) as client:
        response = await client.get("/")
    assert response.status_code == 401


async def test_whois_returning_none_rejected():
    async def unknown(_ip: str) -> None:
        return None

    async with client_for(build_app(resolver=unknown)) as client:
        response = await client.get("/", headers=serve_headers())
    assert response.status_code == 403


async def test_whois_error_fails_closed():
    async def broken(_ip: str) -> WhoisIdentity:
        raise RuntimeError("tailscaled unreachable")

    async with client_for(build_app(resolver=broken)) as client:
        response = await client.get("/", headers=serve_headers())
    assert response.status_code == 403


async def test_whois_timeout_fails_closed():
    async def hangs(_ip: str) -> WhoisIdentity:
        await asyncio.sleep(5)
        return WhoisIdentity(login=OPERATOR_LOGIN)

    async with client_for(build_app(resolver=hangs, whois_timeout_s=0.05)) as client:
        response = await client.get("/", headers=serve_headers())
    assert response.status_code == 403


async def test_non_loopback_peer_rejected_even_with_valid_headers():
    """Layer 1: if the socket peer is not loopback, the request bypassed
    tailscale serve entirely — reject before believing anything."""
    async with client_for(build_app(), peer_ip="100.64.0.9") as client:
        response = await client.get("/", headers=serve_headers())
    assert response.status_code == 403


async def test_loopback_forwarded_source_rejected(operator_client):
    response = await operator_client.get(
        "/", headers=serve_headers(source_ip="127.0.0.1")
    )
    assert response.status_code == 403


async def test_non_https_forwarded_proto_rejected(operator_client):
    response = await operator_client.get("/", headers=serve_headers(proto="http"))
    assert response.status_code == 403


async def test_funnel_marker_rejected(operator_client):
    """C1: Funnel traffic (or anything claiming to be) never authenticates."""
    response = await operator_client.get(
        "/", headers=serve_headers() | {"Tailscale-Funnel-Request": "1"}
    )
    assert response.status_code == 403


async def test_unparseable_forwarded_for_rejected(operator_client):
    response = await operator_client.get(
        "/", headers=serve_headers(source_ip="not-an-ip")
    )
    assert response.status_code == 403


async def test_forwarded_host_pin_enforced_when_configured():
    app = build_app(expected_forwarded_host="vps.tail1234.ts.net")
    async with client_for(app) as client:
        good = await client.get("/v1/whoami", headers=serve_headers())
        bad = await client.get("/", headers=serve_headers(host="evil.example.com"))
    assert good.status_code == 200
    assert bad.status_code == 403


async def test_health_is_exempt_and_db_free():
    async with client_for(build_app()) as client:
        response = await client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_health_from_non_loopback_peer_still_served():
    """The exemption is for liveness, not identity — but layer 1 lives in
    the bind (C2): the middleware exempts /health entirely, so document the
    behavior explicitly here."""
    async with client_for(build_app(), peer_ip="10.0.0.5") as client:
        response = await client.get("/health")
    assert response.status_code == 200


# ── C4: a local process must not be able to forge operator identity ──────
#
# This is the attack an adversarial review found, and it is the one that
# matters most: the agent runtimes run on this same host, so "the peer is
# loopback and the Serve headers are present" proves nothing. Serve injects
# no secret, so a compromised agent can set every header itself — including
# pointing X-Forwarded-For at the operator's own tailnet address, which
# makes `tailscale whois` agree. Without a check the kernel backs, the
# agent authenticates as the operator and approves its own requests.


async def test_local_process_cannot_forge_operator_identity():
    """The full forgery: correct headers, a real operator login, an
    X-Forwarded-For that whois genuinely resolves to that operator — and
    still refused, because the socket was opened by an agent, not by
    tailscale serve."""
    from .conftest import agent_peer

    app = build_app(peer_resolver=agent_peer)
    async with client_for(app) as client:
        response = await client.get("/v1/whoami", headers=serve_headers())
    assert response.status_code == 403


async def test_forged_identity_cannot_decide_an_approval():
    """The same attack aimed at the thing that actually matters (C4)."""
    from .conftest import agent_peer

    app = build_app(peer_resolver=agent_peer)
    async with client_for(app) as client:
        response = await client.post(
            "/v1/approvals/00000000-0000-0000-0000-000000000001/decision",
            headers=serve_headers(),
            json={"approve": True},
        )
    # Rejected at the middleware, before the route or the service is reached.
    assert response.status_code == 403


async def test_unresolvable_peer_is_refused_by_default():
    """If the kernel cannot tell us who opened the socket, we do not guess."""
    def unknown_peer(*_args):
        return None

    app = build_app(peer_resolver=unknown_peer)
    async with client_for(app) as client:
        response = await client.get("/v1/whoami", headers=serve_headers())
    assert response.status_code == 403


async def test_peer_check_can_be_relaxed_only_deliberately():
    """`require_peer_uid=false` is an explicit, documented choice for hosts
    where no untrusted local code runs — never a silent default."""
    def unknown_peer(*_args):
        return None

    app = build_app(peer_resolver=unknown_peer, require_peer_uid=False)
    async with client_for(app) as client:
        response = await client.get("/v1/whoami", headers=serve_headers())
    assert response.status_code == 200


async def test_additional_operator_uid_can_be_permitted():
    """Serve does not have to run as root on every host."""
    from mission_control.auth.peer import PeerCredentials

    def serve_as_uid_115(*_args):
        return PeerCredentials(uid=115, source="test")

    app = build_app(peer_resolver=serve_as_uid_115, operator_peer_uids="0,115")
    async with client_for(app) as client:
        response = await client.get("/v1/whoami", headers=serve_headers())
    assert response.status_code == 200


async def test_agent_routes_stay_reachable_from_an_agent_peer():
    """The peer check guards the OPERATOR surface only — agents must still
    be able to create and poll their own approvals from the same host."""
    from .conftest import agent_peer

    app = build_app(peer_resolver=agent_peer)
    async with client_for(app) as client:
        response = await client.post(
            "/v1/approvals",
            json={"idempotency_key": "x" * 12, "tool_name": "t", "tool_args": {}},
        )
    # 401 for the missing service token, NOT 403 for the peer — proof the
    # agent path is not collateral damage of the identity fix.
    assert response.status_code == 401
