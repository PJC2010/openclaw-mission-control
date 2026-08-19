"""Agent-side credentials — the SECOND, separate auth path (§7.5).

This module deliberately shares no code path with `auth.identity`. The
spec is explicit: "Two distinct middleware paths. Do not build one 'is
authenticated' check that both flow through." The reason is C4 — an agent
must not be able to decide, cancel, or alter its own approval request,
directly or transitively. A shared abstraction is exactly how that
property erodes over time.

The strongest guarantee here is structural rather than procedural:
`approvals:decide` is not a mintable scope. A service token cannot carry
it even if an operator (or an attacker who reached the mint path) asks for
it, so C4 does not depend on remembering to check a list.
"""

from __future__ import annotations

import datetime
import hashlib
import logging
import secrets
from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import ServiceToken

security_log = logging.getLogger("mission_control.security")

SCOPE_CREATE = "approvals:create"
SCOPE_POLL = "approvals:poll"

# The complete set of scopes a service token may ever hold (§7.5).
MINTABLE_SCOPES = frozenset({SCOPE_CREATE, SCOPE_POLL})

# Named only so the mint path can refuse it loudly and so tests can assert
# it is unreachable. It is never granted to a service token (C4/S1).
SCOPE_DECIDE = "approvals:decide"

TOKEN_PREFIX = "mc"


class ScopeNotMintable(ValueError):
    """Raised when someone tries to mint a token outside MINTABLE_SCOPES."""


@dataclass(frozen=True)
class AuthenticatedAgent:
    """The result of a successful service-token check."""

    token_id: str
    name: str
    scopes: frozenset[str]
    agent_id: str | None

    def has(self, scope: str) -> bool:
        # Defense in depth: even a corrupted stored scope list cannot grant
        # decide, because decide is never in MINTABLE_SCOPES.
        return scope in self.scopes and scope in MINTABLE_SCOPES


def hash_token(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def generate_token() -> tuple[str, str, str]:
    """Return (plaintext, prefix, hash). The plaintext is shown once."""
    secret = secrets.token_urlsafe(32)
    prefix = secrets.token_hex(4)
    plaintext = f"{TOKEN_PREFIX}_{prefix}_{secret}"
    return plaintext, prefix, hash_token(plaintext)


def validate_scopes(scopes: list[str]) -> list[str]:
    """Reject anything outside the mintable set — loudly."""
    requested = set(scopes)
    forbidden = requested - MINTABLE_SCOPES
    if forbidden:
        raise ScopeNotMintable(
            f"service tokens may never hold {sorted(forbidden)}; "
            f"mintable scopes are {sorted(MINTABLE_SCOPES)} (§7.5, C4)"
        )
    if not requested:
        raise ScopeNotMintable("a service token with no scopes is useless")
    return sorted(requested)


def extract_bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    candidate = parts[1].strip()
    return candidate or None


async def authenticate(session: AsyncSession, plaintext: str) -> AuthenticatedAgent | None:
    """Resolve a presented token. Any failure returns None (fail closed)."""
    digest = hash_token(plaintext)
    row = await session.scalar(select(ServiceToken).where(ServiceToken.token_hash == digest))
    if row is None:
        return None
    if row.revoked_at is not None:
        security_log.warning("revoked service token presented: name=%s id=%s", row.name, row.id)
        return None

    stored = row.scopes if isinstance(row.scopes, list) else []
    # Intersect with the mintable set at read time too: a row edited
    # directly in the database cannot smuggle in a scope C4 forbids.
    effective = frozenset(scope for scope in stored if scope in MINTABLE_SCOPES)

    await session.execute(
        update(ServiceToken)
        .where(ServiceToken.id == row.id)
        .values(last_used_at=datetime.datetime.now(datetime.timezone.utc))
    )
    await session.commit()

    return AuthenticatedAgent(
        token_id=str(row.id),
        name=row.name,
        scopes=effective,
        agent_id=str(row.agent_id) if row.agent_id else None,
    )
