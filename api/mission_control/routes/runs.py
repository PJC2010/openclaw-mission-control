"""Run list and detail (§16 Phase 1: 'Basic run list and detail view')."""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import func, select

from ..models import Agent, Event, Run
from ..models.enums import RunStatus

router = APIRouter()


def _run_json(run: Run) -> dict[str, Any]:
    return {
        "id": str(run.id),
        "agent_id": str(run.agent_id),
        "external_id": run.external_id,
        "objective_id": str(run.objective_id) if run.objective_id else None,
        "objective_source": run.objective_source.value,
        "trigger": run.trigger.value,
        "parent_run_id": str(run.parent_run_id) if run.parent_run_id else None,
        "started_at": run.started_at.isoformat(),
        "ended_at": run.ended_at.isoformat() if run.ended_at else None,
        "status": run.status.value,
        "error_summary": run.error_summary,
        "token_input": run.token_input,
        "token_output": run.token_output,
        "cost_usd": float(run.cost_usd) if run.cost_usd is not None else None,
    }


@router.get("/v1/runs")
async def list_runs(
    request: Request,
    agent_id: uuid.UUID | None = None,
    status: RunStatus | None = None,
    before: datetime.datetime | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    sessions = request.app.state.db_sessions
    query = select(Run).order_by(Run.started_at.desc()).limit(limit)
    if agent_id is not None:
        query = query.where(Run.agent_id == agent_id)
    if status is not None:
        query = query.where(Run.status == status)
    if before is not None:
        query = query.where(Run.started_at < before)
    async with sessions() as session:
        runs = (await session.scalars(query)).all()
        agents = {
            str(row.id): {"display_name": row.display_name, "runtime": row.runtime.value}
            for row in (await session.scalars(select(Agent))).all()
        }
    items = [_run_json(run) for run in runs]
    next_before = items[-1]["started_at"] if len(items) == limit else None
    return {"runs": items, "agents": agents, "next_before": next_before}


@router.get("/v1/runs/{run_id}")
async def run_detail(request: Request, run_id: uuid.UUID) -> dict[str, Any]:
    sessions = request.app.state.db_sessions
    async with sessions() as session:
        run = await session.get(Run, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        children = (
            await session.scalars(
                select(Run).where(Run.parent_run_id == run_id).order_by(Run.started_at)
            )
        ).all()
        event_count = await session.scalar(
            select(func.count()).select_from(Event).where(Event.run_id == run_id)
        )
        agent = await session.get(Agent, run.agent_id)
    body = _run_json(run)
    body["children"] = [_run_json(child) for child in children]
    body["event_count"] = int(event_count or 0)
    if agent is not None:
        body["agent"] = {"display_name": agent.display_name, "runtime": agent.runtime.value}
    return body


@router.get("/v1/runs/{run_id}/events")
async def run_events(
    request: Request,
    run_id: uuid.UUID,
    after_id: int = Query(default=0, ge=0),
    limit: int = Query(default=500, ge=1, le=1000),
) -> dict[str, Any]:
    sessions = request.app.state.db_sessions
    async with sessions() as session:
        if await session.get(Run, run_id) is None:
            raise HTTPException(status_code=404, detail="run not found")
        events = (
            await session.scalars(
                select(Event)
                .where(Event.run_id == run_id, Event.id > after_id)
                .order_by(Event.id)
                .limit(limit)
            )
        ).all()
    return {
        "events": [
            {
                "id": event.id,
                "seq": event.seq,
                "ts": event.ts.isoformat(),
                "kind": event.kind.value,
                "payload": event.payload,
            }
            for event in events
        ]
    }
