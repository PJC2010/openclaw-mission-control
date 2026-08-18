"""Shared fixtures.

Requests are driven through httpx.ASGITransport so the ASGI `client` peer
address is controllable — the §11.3 layer-1 loopback check depends on it.
"""

from __future__ import annotations

from typing import AsyncIterator

import httpx
import pytest

from mission_control.app import create_app
from mission_control.auth.tailscale import WhoisIdentity
from mission_control.config import Settings

OPERATOR_LOGIN = "castillop92@gmail.com"
TAILNET_IP = "100.101.102.103"
SERVE_HOST = "vps.tail1234.ts.net"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def make_settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


async def ok_resolver(source_ip: str) -> WhoisIdentity | None:
    """Happy-path fake tailscaled: the tailnet IP belongs to the operator."""
    if source_ip == TAILNET_IP:
        return WhoisIdentity(login=OPERATOR_LOGIN, display_name="Pete", node_name="phone.ts.net.")
    return None


def build_app(resolver=ok_resolver, **settings_overrides):
    return create_app(settings=make_settings(**settings_overrides), whois_resolver=resolver)


def client_for(app, peer_ip: str = "127.0.0.1") -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, client=(peer_ip, 54321))
    return httpx.AsyncClient(transport=transport, base_url="http://mission-control.test")


def serve_headers(
    login: str = OPERATOR_LOGIN,
    source_ip: str = TAILNET_IP,
    host: str = SERVE_HOST,
    proto: str = "https",
) -> dict[str, str]:
    """The header shape tailscale serve puts on a proxied request."""
    return {
        "Tailscale-User-Login": login,
        "Tailscale-User-Name": "Pete Castillo",
        "X-Forwarded-For": source_ip,
        "X-Forwarded-Proto": proto,
        "X-Forwarded-Host": host,
    }


@pytest.fixture
async def operator_client() -> AsyncIterator[httpx.AsyncClient]:
    async with client_for(build_app()) as client:
        yield client
