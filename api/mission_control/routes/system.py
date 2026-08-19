"""Operator-facing system health: adapter states, DB reachability."""

from __future__ import annotations

from fastapi import APIRouter, Request
from sqlalchemy import text

router = APIRouter()


@router.get("/v1/system/health")
async def system_health(request: Request) -> dict:
    supervisor = getattr(request.app.state, "supervisor", None)
    database = {"ok": False}
    try:
        async with request.app.state.db_sessions() as session:
            await session.execute(text("SELECT 1"))
        database = {"ok": True}
    except Exception as exc:  # noqa: BLE001 — health endpoints report, not raise
        database = {"ok": False, "error": repr(exc)}
    return {
        "database": database,
        "adapters": supervisor.health() if supervisor is not None else {},
    }
