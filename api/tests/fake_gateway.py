"""A scripted OpenClaw-gateway stand-in speaking protocol v4 over a real
WebSocket, for adapter tests: challenge → connect → hello-ok, RPC replies
from a method table, push-side events, and controllable disconnects."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable

import websockets


class FakeGateway:
    def __init__(self, methods: dict[str, Callable[[dict], dict]] | None = None) -> None:
        self.methods: dict[str, Callable[[dict], dict]] = methods or {}
        self.connections: list[websockets.ServerConnection] = []
        self.connect_params: list[dict] = []
        self.accept_token = "test-token"
        self._server: websockets.Server | None = None
        self.port = 0

    async def start(self) -> str:
        self._server = await websockets.serve(self._handler, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return f"ws://127.0.0.1:{self.port}"

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def drop_all(self) -> None:
        for conn in list(self.connections):
            await conn.close()

    async def push_event(self, event: str, payload: dict | None = None) -> None:
        frame = json.dumps({"type": "event", "event": event, "payload": payload or {}})
        for conn in list(self.connections):
            await conn.send(frame)

    async def _handler(self, ws: websockets.ServerConnection) -> None:
        self.connections.append(ws)
        try:
            await ws.send(
                json.dumps(
                    {
                        "type": "event",
                        "event": "connect.challenge",
                        "payload": {"nonce": "n", "ts": 1},
                    }
                )
            )
            raw = await ws.recv()
            frame = json.loads(raw)
            assert frame.get("method") == "connect", f"first frame was {frame}"
            self.connect_params.append(frame.get("params") or {})
            token = ((frame.get("params") or {}).get("auth") or {}).get("token")
            if token != self.accept_token:
                await ws.send(
                    json.dumps(
                        {
                            "type": "res",
                            "id": frame["id"],
                            "ok": False,
                            "error": {"code": "UNAUTHORIZED", "message": "bad token"},
                        }
                    )
                )
                return
            await ws.send(
                json.dumps(
                    {
                        "type": "res",
                        "id": frame["id"],
                        "ok": True,
                        "payload": {
                            "type": "hello-ok",
                            "protocol": 4,
                            "server": {"version": "2026.7.1-2", "connId": "c1"},
                            "features": {"methods": ["audit.activity.list", "sessions.list"], "events": []},
                            "snapshot": {},
                            "auth": {"role": "operator", "scopes": ["operator.read"]},
                            "policy": {"maxPayload": 26214400, "maxBufferedBytes": 52428800, "tickIntervalMs": 15000},
                        },
                    }
                )
            )
            async for raw in ws:
                frame = json.loads(raw)
                if frame.get("type") != "req":
                    continue
                handler = self.methods.get(frame["method"])
                if handler is None:
                    response: dict[str, Any] = {
                        "type": "res",
                        "id": frame["id"],
                        "ok": False,
                        "error": {"code": "UNKNOWN_METHOD", "message": f"unknown method: {frame['method']}"},
                    }
                else:
                    response = {
                        "type": "res",
                        "id": frame["id"],
                        "ok": True,
                        "payload": handler(frame.get("params") or {}),
                    }
                await ws.send(json.dumps(response))
        except websockets.ConnectionClosed:
            pass
        finally:
            if ws in self.connections:
                self.connections.remove(ws)


def audit_row(
    seq: int,
    event_type: str = "agent_run",
    status: str = "started",
    run_id: str = "run-1",
    session_key: str | None = "sess-A",
    occurred_ms: int = 1_766_000_000_000,
    **extra: Any,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "eventType": event_type,
        "schemaVersion": 1,
        "eventId": f"evt-{seq}",
        "sequence": seq,
        "sourceSequence": seq,
        "occurredAt": occurred_ms + seq * 1000,
        "kind": "agent_run" if event_type == "agent_run" else ("tool_action" if event_type == "tool_action" else "message"),
        "action": "run",
        "status": status,
        "actor": {"type": "agent"},
        "redaction": "metadata_only",
        "agentId": "main",
        "runId": run_id,
    }
    if session_key is not None:
        row["sessionKey"] = session_key
    row.update(extra)
    return row
