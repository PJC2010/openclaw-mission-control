"""Liveness endpoint.

Deliberately identity-exempt and DB-free: it must answer while the database
is still coming up (systemd ordering) and it discloses nothing sensitive.
Phase 0 acceptance: reachable from the operator's phone at the ts.net URL,
and NOT reachable from off-tailnet.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter

from .. import __version__

router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str]:
    return {
        "status": "ok",
        "service": "mission-control",
        "version": __version__,
        "time": datetime.now(timezone.utc).isoformat(),
    }
