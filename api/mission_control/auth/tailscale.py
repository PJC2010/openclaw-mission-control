"""Resolve a tailnet source address to a verified identity via `tailscale whois`.

This is layer 3 of the §11.3 header-trust model: the identity *claimed* by
the `Tailscale-User-Login` header is only believed after the local Tailscale
daemon confirms that the forwarded source address belongs to that login.

Every failure mode — CLI missing, daemon down, timeout, unparseable output,
unknown address — resolves to ``None``, and the caller treats ``None`` as
"reject". Fail closed (C7 spirit applies to auth as much as to approvals).

Verified against Tailscale docs, 2026-08-18:
  - `tailscale whois --json <ip[:port]>` prints JSON with `Node` (incl.
    `Tags` for tagged devices) and `UserProfile.LoginName` / `DisplayName`.
  - Serve's identity headers are populated for users, never tagged devices,
    so a tagged-node whois result must NOT satisfy a user-identity claim.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

log = logging.getLogger("mission_control.auth")

# Contract shared by the real resolver, the caching wrapper, and test fakes.
WhoisResolver = Callable[[str], Awaitable[Optional["WhoisIdentity"]]]


@dataclass(frozen=True)
class WhoisIdentity:
    """What the local tailscaled asserts about a source address."""

    login: str
    display_name: str = ""
    node_name: str = ""


def parse_whois_json(raw: str | bytes) -> WhoisIdentity | None:
    """Parse `tailscale whois --json` output into an identity, or None.

    Returns None for tagged nodes: identity headers are only ever populated
    for users, so a user-login claim from a tagged device is a spoof.
    """
    try:
        doc = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(doc, dict):
        return None

    node = doc.get("Node") or {}
    if isinstance(node, dict) and node.get("Tags"):
        return None

    profile = doc.get("UserProfile") or {}
    if not isinstance(profile, dict):
        return None
    login = (profile.get("LoginName") or "").strip()
    if not login:
        return None
    return WhoisIdentity(
        login=login,
        display_name=(profile.get("DisplayName") or "").strip(),
        node_name=(node.get("Name") or "").strip() if isinstance(node, dict) else "",
    )


class CliWhoisResolver:
    """whois via the `tailscale` CLI.

    The CLI talks to tailscaled over its local socket; the systemd unit must
    run as a user with socket access (`tailscale set --operator=missionctl`,
    see docs/phase-0-runbook.md).
    """

    def __init__(self, tailscale_bin: str = "tailscale", timeout_s: float = 2.0) -> None:
        self._bin = tailscale_bin
        self._timeout_s = timeout_s

    async def __call__(self, source_ip: str) -> WhoisIdentity | None:
        try:
            proc = await asyncio.create_subprocess_exec(
                self._bin,
                "whois",
                "--json",
                source_ip,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            log.error("whois: cannot execute %r: %s", self._bin, exc)
            return None
        try:
            async with asyncio.timeout(self._timeout_s):
                stdout, stderr = await proc.communicate()
        except TimeoutError:
            proc.kill()
            await proc.wait()
            log.warning("whois: timed out after %.1fs for %s", self._timeout_s, source_ip)
            return None
        if proc.returncode != 0:
            log.warning(
                "whois: exit %s for %s: %s",
                proc.returncode,
                source_ip,
                stderr.decode(errors="replace").strip()[:200],
            )
            return None
        return parse_whois_json(stdout)


class TtlCachingResolver:
    """Small positive-result cache so per-request whois stays cheap.

    Only successful lookups are cached (a transient tailscaled hiccup must
    not stick as a denial, and a denial must never stick as an approval).
    TTL is short (default 60s) because device→user mappings can change on
    node re-auth.
    """

    def __init__(self, inner: WhoisResolver, ttl_s: float = 60.0) -> None:
        self._inner = inner
        self._ttl_s = ttl_s
        self._cache: dict[str, tuple[float, WhoisIdentity]] = {}

    async def __call__(self, source_ip: str) -> WhoisIdentity | None:
        now = time.monotonic()
        hit = self._cache.get(source_ip)
        if hit is not None and hit[0] > now:
            return hit[1]
        result = await self._inner(source_ip)
        if result is not None and self._ttl_s > 0:
            self._cache[source_ip] = (now + self._ttl_s, result)
        return result
