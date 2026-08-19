"""§17 test 4 — the agent-side wrapper must fail closed.

Exercises the real Hermes transport plugin against unreachable, broken,
and hostile Mission Control responses. Every path must deny (§7.6, C7).
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

PLUGIN_PATH = (
    pathlib.Path(__file__).resolve().parent.parent.parent
    / "agents"
    / "hermes-approval-transport"
    / "__init__.py"
)


def load_plugin():
    spec = importlib.util.spec_from_file_location("mc_hermes_transport", PLUGIN_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


plugin = load_plugin()


class FakeRequest:
    """Mirrors the frozen ApprovalRequest dataclass Hermes passes in."""

    def __init__(self, **overrides):
        self.schema_version = 1
        self.request_id = overrides.get("request_id", "abc123")
        self.digest = "d" * 64
        self.command = overrides.get("command", "rm -rf /tmp/x")
        self.description = "delete scratch dir"
        self.pattern_key = "rm"
        self.pattern_keys = ("rm",)
        self.surface = "cli"
        self.timeout_seconds = overrides.get("timeout_seconds", 3.0)
        self.allowed_choices = overrides.get("allowed_choices", ("once", "deny"))
        self.responded: str | None = None

    def respond(self, choice: str) -> str:
        self.responded = choice
        return choice


class Handler(BaseHTTPRequestHandler):
    """Scripted Mission Control stand-in."""

    create_response: tuple[int, dict] = (201, {})
    poll_response: tuple[int, dict] = (200, {})

    def _send(self, pair):
        status, body = pair
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):  # noqa: N802
        self._send(self.create_response)

    def do_GET(self):  # noqa: N802
        self._send(self.poll_response)

    def log_message(self, *args):  # silence
        return


@pytest.fixture
def server(monkeypatch):
    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    port = httpd.server_address[1]
    monkeypatch.setenv("MISSION_CONTROL_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("MISSION_CONTROL_TOKEN", "mc_test_token")
    yield Handler
    httpd.shutdown()


# ── §17.4 and its neighbours ─────────────────────────────────────────────


def test_unreachable_mission_control_denies(monkeypatch):
    """Mission Control stopped mid-request → the wrapper fails closed."""
    # Port 1 is reserved and refuses instantly.
    monkeypatch.setenv("MISSION_CONTROL_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("MISSION_CONTROL_TOKEN", "mc_test_token")
    request = FakeRequest()
    assert plugin.present(request) == "deny"
    assert request.responded == "deny"


def test_missing_token_denies(monkeypatch):
    monkeypatch.setenv("MISSION_CONTROL_URL", "http://127.0.0.1:1")
    monkeypatch.delenv("MISSION_CONTROL_TOKEN", raising=False)
    monkeypatch.setenv("MISSION_CONTROL_TOKEN_FILE", "/nonexistent/token")
    assert plugin.present(FakeRequest()) == "deny"


def test_http_error_denies(server):
    server.create_response = (409, {"detail": "idempotency conflict"})
    assert plugin.present(FakeRequest()) == "deny"


def test_unparseable_response_denies(server):
    class Broken(Handler):
        def do_POST(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Length", "7")
            self.end_headers()
            self.wfile.write(b"NOTJSON")

    server.create_response = (200, {})
    # Swap the handler behaviour by monkeypatching the class method.
    original = Handler.do_POST
    Handler.do_POST = Broken.do_POST
    try:
        assert plugin.present(FakeRequest()) == "deny"
    finally:
        Handler.do_POST = original


def test_missing_approval_id_denies(server):
    server.create_response = (201, {"state": "pending"})  # no approval_id
    assert plugin.present(FakeRequest()) == "deny"


def test_never_decided_before_deadline_denies(server):
    server.create_response = (201, {"approval_id": "a-1", "state": "pending"})
    server.poll_response = (200, {"state": "pending", "allowed": False})
    request = FakeRequest(timeout_seconds=1.0)
    assert plugin.present(request) == "deny"


def test_denied_decision_denies(server):
    server.create_response = (201, {"approval_id": "a-2", "state": "pending"})
    server.poll_response = (200, {"state": "denied", "allowed": False})
    assert plugin.present(FakeRequest()) == "deny"


def test_expired_decision_denies(server):
    """§17.3 — the wrapper treats `expired` exactly as deny."""
    server.create_response = (201, {"approval_id": "a-3", "state": "pending"})
    server.poll_response = (200, {"state": "expired", "allowed": False})
    assert plugin.present(FakeRequest()) == "deny"


def test_approved_decision_allows_once(server):
    server.create_response = (201, {"approval_id": "a-4", "state": "pending"})
    server.poll_response = (200, {"state": "approved", "allowed": True})
    request = FakeRequest()
    assert plugin.present(request) == "once"
    assert request.responded == "once"


def test_forged_allowed_flag_without_approved_state_denies(server):
    """A response claiming `allowed` while the state is not `approved` is
    incoherent — trust the state, deny."""
    server.create_response = (201, {"approval_id": "a-5", "state": "pending"})
    server.poll_response = (200, {"state": "denied", "allowed": True})
    assert plugin.present(FakeRequest()) == "deny"


def test_never_returns_session_or_always(server):
    """§7.6 — one approval, one execution. Standing grants are not ours."""
    server.create_response = (201, {"approval_id": "a-6", "state": "pending"})
    server.poll_response = (200, {"state": "approved", "allowed": True})
    request = FakeRequest(allowed_choices=("once", "session", "always", "deny"))
    assert plugin.present(request) == "once"


def test_approved_but_once_not_offered_denies(server):
    server.create_response = (201, {"approval_id": "a-7", "state": "pending"})
    server.poll_response = (200, {"state": "approved", "allowed": True})
    request = FakeRequest(allowed_choices=("deny",))
    assert plugin.present(request) == "deny"


def test_immediate_auto_approval_is_honoured(server):
    """A matching auto_approve policy answers at creation time."""
    server.create_response = (201, {"approval_id": "a-8", "state": "approved", "allowed": True})
    assert plugin.present(FakeRequest()) == "once"


def test_immediate_denial_is_honoured(server):
    """Kill switch or rate limit denies at creation time."""
    server.create_response = (201, {"approval_id": "a-9", "state": "denied", "allowed": False})
    assert plugin.present(FakeRequest()) == "deny"


def test_correlation_hook_only_captures_transport_surface():
    plugin._correlation.clear()
    plugin._capture_correlation(surface="gateway", request_id="r1", session_id="s1")
    assert "r1" not in plugin._correlation
    plugin._capture_correlation(
        surface="transport:mission-control", request_id="r2", session_id="s2", session_key="k2"
    )
    assert plugin._correlation["r2"]["session_id"] == "s2"
    # Recall is destructive so a replayed id cannot reuse stale context.
    assert plugin._recall("r2")["session_key"] == "k2"
    assert plugin._recall("r2") == {}
