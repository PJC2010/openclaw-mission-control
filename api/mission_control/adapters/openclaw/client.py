"""Single-connection OpenClaw Gateway client.

Owns one WebSocket: handshake, RPC request/response correlation, and event
dispatch. The adapter owns the reconnect loop (backoff policy is §6.1's
concern, not the transport's).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

import websockets

from .protocol import (
    GatewayEvent,
    ProtocolError,
    Request,
    Response,
    build_connect_params,
    build_request,
    parse_frame,
)

log = logging.getLogger("mission_control.adapters.openclaw")

EventHandler = Callable[[GatewayEvent], Awaitable[None]]


class GatewayError(RuntimeError):
    def __init__(self, error: dict[str, Any] | None, context: str) -> None:
        error = error or {}
        self.code = str(error.get("code") or "UNKNOWN")
        self.retryable = bool(error.get("retryable"))
        self.retry_after_ms = error.get("retryAfterMs")
        self.details = error.get("details") if isinstance(error.get("details"), dict) else {}
        super().__init__(f"{context}: {self.code}: {error.get('message', '')}")


class OpenClawClient:
    def __init__(
        self,
        url: str,
        token: str,
        client_version: str,
        on_event: EventHandler,
        rpc_timeout_s: float = 10.0,
        scopes: list[str] | None = None,
    ) -> None:
        self._url = url
        self._token = token
        self._client_version = client_version
        self._on_event = on_event
        self._rpc_timeout_s = rpc_timeout_s
        self._scopes = scopes or ["operator.read"]
        self._ws: websockets.ClientConnection | None = None
        self._pending: dict[str, asyncio.Future[Response]] = {}
        self._reader: asyncio.Task[None] | None = None
        self.hello: dict[str, Any] = {}
        self.closed = asyncio.Event()

    async def connect(self, handshake_timeout_s: float = 15.0) -> dict[str, Any]:
        """Open the socket, perform the v4 handshake, start the reader.
        Returns the hello-ok payload. Raises on any failure (caller retries)."""
        self._ws = await websockets.connect(self._url, max_size=32 * 1024 * 1024)
        try:
            # Gateway pushes connect.challenge first; absorb it if present.
            try:
                raw = await asyncio.wait_for(self._ws.recv(), timeout=2.0)
                frame = parse_frame(raw)
                if not (isinstance(frame, GatewayEvent) and frame.event == "connect.challenge"):
                    log.debug("pre-connect frame was %s, continuing", frame)
            except asyncio.TimeoutError:
                pass  # older gateways may not send a challenge

            connect_id = "connect-1"
            await self._ws.send(
                build_request(
                    "connect",
                    build_connect_params(self._token, self._client_version, self._scopes),
                    req_id=connect_id,
                )
            )
            hello = await asyncio.wait_for(
                self._await_response(connect_id), timeout=handshake_timeout_s
            )
            if not hello.ok:
                raise GatewayError(hello.error, "connect rejected")
            payload = hello.payload or {}
            if payload.get("type") != "hello-ok":
                raise ProtocolError(f"unexpected connect payload type: {payload.get('type')!r}")
            self.hello = payload
            self._reader = asyncio.create_task(self._read_loop(), name="openclaw-reader")
            return payload
        except BaseException:
            await self.close()
            raise

    async def _await_response(self, req_id: str) -> Response:
        """Pre-reader-loop response wait (handshake only)."""
        assert self._ws is not None
        while True:
            frame = parse_frame(await self._ws.recv())
            if isinstance(frame, Response) and frame.id == req_id:
                return frame
            if isinstance(frame, GatewayEvent):
                continue  # pre-hello events are uninteresting

    async def _read_loop(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                try:
                    frame = parse_frame(raw)
                except ProtocolError as exc:
                    log.warning("dropping bad frame: %s", exc)
                    continue
                if isinstance(frame, Response):
                    future = self._pending.pop(frame.id, None)
                    if future is not None and not future.done():
                        future.set_result(frame)
                elif isinstance(frame, GatewayEvent):
                    try:
                        await self._on_event(frame)
                    except Exception:  # noqa: BLE001 — one bad handler must not kill the socket
                        log.exception("event handler failed for %s", frame.event)
                # Server-initiated Requests are node-role machinery; ignored.
        except websockets.ConnectionClosed as exc:
            log.info("gateway connection closed: %s", exc)
        finally:
            self.closed.set()
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(ConnectionError("gateway connection closed"))
            self._pending.clear()

    async def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if self._ws is None or self.closed.is_set():
            raise ConnectionError("not connected")
        req_id = f"{method}-{id(params)}-{len(self._pending)}"
        future: asyncio.Future[Response] = asyncio.get_running_loop().create_future()
        self._pending[req_id] = future
        await self._ws.send(build_request(method, params, req_id=req_id))
        try:
            response = await asyncio.wait_for(future, timeout=self._rpc_timeout_s)
        finally:
            self._pending.pop(req_id, None)
        if not response.ok:
            raise GatewayError(response.error, f"{method} failed")
        return response.payload or {}

    async def close(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
            try:
                await self._reader
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._reader = None
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass
            self._ws = None
        self.closed.set()
