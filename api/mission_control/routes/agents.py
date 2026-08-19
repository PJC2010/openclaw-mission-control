"""Agent roster and liveness (§16 Phase 1: 'Agent up/down status')."""

from __future__ import annotations

from fastapi import APIRouter, Request
from sqlalchemy import select

from ..models import Agent

router = APIRouter()


@router.get("/v1/agents")
async def list_agents(request: Request) -> dict:
    sessions = request.app.state.db_sessions
    supervisor = getattr(request.app.state, "supervisor", None)
    adapter_health = supervisor.health() if supervisor is not None else {}
    async with sessions() as session:
        agents = (await session.scalars(select(Agent).order_by(Agent.display_name))).all()
    return {
        "agents": [
            {
                "id": str(agent.id),
                "runtime": agent.runtime.value if hasattr(agent.runtime, "value") else str(agent.runtime),
                "display_name": agent.display_name,
                "instance_key": agent.instance_key,
                "status": agent.status,
                "last_heartbeat_at": agent.last_heartbeat_at.isoformat()
                if agent.last_heartbeat_at
                else None,
                "adapter": adapter_health.get(str(agent.id)),
            }
            for agent in agents
        ]
    }
