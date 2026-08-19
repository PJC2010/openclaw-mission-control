"""OpenClaw Gateway WS protocol v4 — frames and handshake payloads.

Verified against docs.openclaw.ai/gateway/protocol (2026-08-18; gateway
2026.7.x line):

- Text frames carrying JSON. First client frame MUST be a `connect` req.
- Request:  {type:"req", id, method, params}
- Response: {type:"res", id, ok, payload|error}
- Event:    {type:"event", event, payload, seq?, stateVersion?}
- Handshake: gateway pushes `connect.challenge`; client sends `connect`
  with protocol 4, role "operator", scopes and auth token; gateway
  answers hello-ok (payload.type == "hello-ok").
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any

PROTOCOL_VERSION = 4


@dataclass(frozen=True)
class Request:
    id: str
    method: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Response:
    id: str
    ok: bool
    payload: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


@dataclass(frozen=True)
class GatewayEvent:
    event: str
    payload: dict[str, Any] = field(default_factory=dict)
    seq: int | None = None


class ProtocolError(RuntimeError):
    pass


def parse_frame(raw: str | bytes) -> Request | Response | GatewayEvent:
    try:
        doc = json.loads(raw)
    except ValueError as exc:
        raise ProtocolError(f"unparseable frame: {exc}") from exc
    if not isinstance(doc, dict):
        raise ProtocolError(f"frame is {type(doc).__name__}, expected object")
    frame_type = doc.get("type")
    if frame_type == "res":
        return Response(
            id=str(doc.get("id")),
            ok=bool(doc.get("ok")),
            payload=doc.get("payload") if isinstance(doc.get("payload"), dict) else None,
            error=doc.get("error") if isinstance(doc.get("error"), dict) else None,
        )
    if frame_type == "event":
        return GatewayEvent(
            event=str(doc.get("event")),
            payload=doc.get("payload") if isinstance(doc.get("payload"), dict) else {},
            seq=doc.get("seq") if isinstance(doc.get("seq"), int) else None,
        )
    if frame_type == "req":
        return Request(
            id=str(doc.get("id")),
            method=str(doc.get("method")),
            params=doc.get("params") if isinstance(doc.get("params"), dict) else {},
        )
    raise ProtocolError(f"unknown frame type: {frame_type!r}")


def build_request(method: str, params: dict[str, Any] | None = None, req_id: str | None = None) -> str:
    return json.dumps(
        {
            "type": "req",
            "id": req_id or uuid.uuid4().hex,
            "method": method,
            "params": params or {},
        }
    )


def build_connect_params(token: str, client_version: str, scopes: list[str]) -> dict[str, Any]:
    """Loopback operator connect. Plain token clients on loopback do not go
    through device pairing (docs: pairing applies to remote/browser/node/
    device-token clients)."""
    return {
        "minProtocol": PROTOCOL_VERSION,
        "maxProtocol": PROTOCOL_VERSION,
        "client": {
            "id": "mission-control",
            "version": client_version,
            "platform": "linux",
            "mode": "operator",
        },
        "role": "operator",
        "scopes": scopes,
        "auth": {"token": token},
        "userAgent": f"mission-control/{client_version}",
    }
