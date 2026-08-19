"""Live event stream over SSE with Last-Event-ID resume (§13, §16 P1).

The SSE `id:` is the global `events.id`. Clients (including the browser's
native EventSource) send it back as `Last-Event-ID` on reconnect; missed
events replay from the database before the live feed resumes, so a
network drop never silently gaps the tail. A slow consumer that overflows
its queue is resynchronized from the database the same way.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import AsyncIterator

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import StreamingResponse

from ..normalizer import EventRecord, Normalizer

router = APIRouter()

KEEPALIVE_S = 15.0


def _format(record: EventRecord) -> str:
    return f"id: {record.id}\nevent: {record.kind}\ndata: {json.dumps(record.as_json())}\n\n"


@router.get("/v1/stream")
async def stream(
    request: Request,
    agent_id: uuid.UUID | None = None,
    run_id: uuid.UUID | None = None,
    last_event_id: int | None = Query(default=None, ge=0),
    last_event_id_header: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    normalizer: Normalizer = request.app.state.normalizer

    resume_from: int | None = last_event_id
    if resume_from is None and last_event_id_header:
        try:
            resume_from = int(last_event_id_header)
        except ValueError:
            resume_from = None

    def matches(record: EventRecord) -> bool:
        if agent_id is not None and record.agent_id != agent_id:
            return False
        if run_id is not None and record.run_id != run_id:
            return False
        return True

    async def generate() -> AsyncIterator[str]:
        yield "retry: 3000\n\n"
        last_sent = resume_from
        # Subscribe FIRST, then replay: anything published during replay is
        # caught by the id > last_sent filter instead of being lost.
        key, queue = normalizer.broadcast.subscribe()
        try:
            if last_sent is not None:
                async for record in normalizer.events_after(
                    last_sent, limit=1000, agent_id=agent_id, run_id=run_id
                ):
                    yield _format(record)
                    last_sent = record.id
            while True:
                if await request.is_disconnected():
                    return
                try:
                    record = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_S)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if record is None:  # overflowed — resync from the database
                    normalizer.broadcast.unsubscribe(key)
                    key, queue = normalizer.broadcast.subscribe()
                    async for replay in normalizer.events_after(
                        last_sent or 0, limit=1000, agent_id=agent_id, run_id=run_id
                    ):
                        yield _format(replay)
                        last_sent = replay.id
                    continue
                if not matches(record):
                    continue
                if last_sent is not None and record.id <= last_sent:
                    continue
                yield _format(record)
                last_sent = record.id
        finally:
            normalizer.broadcast.unsubscribe(key)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
