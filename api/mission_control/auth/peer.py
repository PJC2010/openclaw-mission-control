"""Who actually opened this socket? (§11.3, layer 1 — strengthened.)

The original layer-1 check asked only whether the peer address was
loopback. On this host that proves nothing: the agent runtimes run on the
same machine, so a compromised agent can open a loopback connection and
set `Tailscale-User-Login` together with the `X-Forwarded-*` trio itself.
Serve injects no secret, so no header can distinguish its traffic from a
local forgery — pointing `X-Forwarded-For` at the operator's own tailnet
address makes `whois` agree, and the agent authenticates as the operator.

That is a complete break of C4: the agent decides its own approvals. The
spec anticipates it in §11.4 — "tokenless header auth assumes the host is
trusted, so if untrusted local code may run on the VPS, require token auth
instead" — and §12's threat model says untrusted local code is exactly
what runs here.

The kernel knows what headers cannot express: which UID owns the socket
on the other end. `tailscale serve` proxies as root; the agent runtimes do
not. /proc/net/tcp exposes that UID directly and an unprivileged process
cannot falsify its own entry, so this is checked before any header is
believed.

Linux-specific by design — the VPS is Linux, and a platform without this
information must fail closed rather than degrade silently.
"""

from __future__ import annotations

import ipaddress
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

log = logging.getLogger("mission_control.auth")

PROC_TCP = (Path("/proc/net/tcp"), Path("/proc/net/tcp6"))

# Contract shared by the real resolver and test fakes.
PeerResolver = Callable[[str, int, str, int], "PeerCredentials | None"]


@dataclass(frozen=True)
class PeerCredentials:
    uid: int
    source: str = "proc"


def _parse_hex_addr(token: str) -> tuple[str, int] | None:
    """`0100007F:1F90` → ("127.0.0.1", 8080). Words are little-endian."""
    try:
        raw_ip, raw_port = token.split(":")
        port = int(raw_port, 16)
    except (ValueError, AttributeError):
        return None
    try:
        octets = bytes.fromhex(raw_ip)
    except ValueError:
        return None
    if len(octets) == 4:
        address = ipaddress.IPv4Address(bytes(reversed(octets)))
    elif len(octets) == 16:
        # Each 32-bit word is byte-reversed independently.
        reordered = b"".join(bytes(reversed(octets[i : i + 4])) for i in range(0, 16, 4))
        address = ipaddress.IPv6Address(reordered)
        if address.ipv4_mapped:
            address = address.ipv4_mapped
    else:
        return None
    return str(address), port


def resolve_peer_uid(
    peer_ip: str, peer_port: int, local_ip: str, local_port: int
) -> PeerCredentials | None:
    """UID of the process at the other end, or None if it cannot be proven.

    The peer's own socket appears in /proc/net/tcp with local_address =
    the peer's side and rem_address = ours, and carries its owner's UID.
    """
    try:
        want_peer = (str(ipaddress.ip_address(peer_ip)), int(peer_port))
        want_local = (str(ipaddress.ip_address(local_ip)), int(local_port))
    except (ValueError, TypeError):
        return None

    for path in PROC_TCP:
        try:
            lines = path.read_text().splitlines()
        except OSError:
            continue
        for line in lines[1:]:
            fields = line.split()
            if len(fields) < 8:
                continue
            local = _parse_hex_addr(fields[1])
            remote = _parse_hex_addr(fields[2])
            if local != want_peer or remote != want_local:
                continue
            try:
                return PeerCredentials(uid=int(fields[7]))
            except (ValueError, IndexError):
                return None
    return None
