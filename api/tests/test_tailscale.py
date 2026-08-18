"""whois output parsing and the positive-result cache."""

from __future__ import annotations

import json

import pytest

from mission_control.auth.tailscale import (
    TtlCachingResolver,
    WhoisIdentity,
    parse_whois_json,
)

# Shape confirmed against `tailscale whois --json` docs (UserProfile.LoginName,
# Node.Tags for tagged devices), 2026-08-18.
USER_WHOIS = {
    "Node": {"ID": 123, "Name": "pixel.tail1234.ts.net.", "Tags": None},
    "UserProfile": {
        "ID": 456,
        "LoginName": "castillop92@gmail.com",
        "DisplayName": "Pete Castillo",
    },
    "CapMap": None,
}

TAGGED_WHOIS = {
    "Node": {"ID": 789, "Name": "ci-runner.tail1234.ts.net.", "Tags": ["tag:server"]},
    "UserProfile": {"ID": 0, "LoginName": "tagged-devices", "DisplayName": "Tagged Devices"},
}


def test_parse_user_node():
    identity = parse_whois_json(json.dumps(USER_WHOIS))
    assert identity == WhoisIdentity(
        login="castillop92@gmail.com",
        display_name="Pete Castillo",
        node_name="pixel.tail1234.ts.net.",
    )


def test_tagged_node_yields_no_identity():
    """Identity headers are never populated for tagged devices, so a tagged
    source must not satisfy a user-identity claim."""
    assert parse_whois_json(json.dumps(TAGGED_WHOIS)) is None


@pytest.mark.parametrize("raw", ["", "not json", "[]", "42", json.dumps({"Node": {}})])
def test_garbage_yields_no_identity(raw: str):
    assert parse_whois_json(raw) is None


@pytest.mark.anyio
async def test_cache_serves_repeat_lookups_without_reresolving():
    calls = 0

    async def counting(_ip: str) -> WhoisIdentity:
        nonlocal calls
        calls += 1
        return WhoisIdentity(login="castillop92@gmail.com")

    resolver = TtlCachingResolver(counting, ttl_s=60)
    assert await resolver("100.0.0.1") is not None
    assert await resolver("100.0.0.1") is not None
    assert calls == 1


@pytest.mark.anyio
async def test_failures_are_not_cached():
    calls = 0

    async def failing(_ip: str) -> None:
        nonlocal calls
        calls += 1
        return None

    resolver = TtlCachingResolver(failing, ttl_s=60)
    assert await resolver("100.0.0.2") is None
    assert await resolver("100.0.0.2") is None
    assert calls == 2
