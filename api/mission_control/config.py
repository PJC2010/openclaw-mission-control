"""Application settings.

Constraint C2 is enforced here, not just in deploy config: the process
refuses to start on a non-loopback bind address. The identity model in §11
assumes the only way to reach this app is through `tailscale serve` proxying
to loopback; a `0.0.0.0` bind would let any tailnet (or, misconfigured,
LAN) peer bypass the proxy and forge identity headers.
"""

from __future__ import annotations

import ipaddress

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LOOPBACK_ERROR = (
    "MC_BIND_HOST must be a literal loopback IP (127.0.0.1 or ::1). "
    "Binding beyond loopback violates constraint C2 and breaks the "
    "identity-header trust model (§11.3). There is deliberately no override."
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MC_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    bind_host: str = "127.0.0.1"
    bind_port: int = 8100

    # Runtime role (mc_app). Migrations use MC_MIGRATE_DATABASE_URL via alembic/env.py.
    database_url: str = "postgresql+psycopg://mc_app@127.0.0.1:5432/mission_control"

    # §11.3 layer 3 — whois verification of the forwarded source address.
    tailscale_bin: str = "tailscale"
    whois_timeout_s: float = 2.0
    whois_cache_ttl_s: float = 60.0
    # Optional pin of the Serve-fronted MagicDNS name (X-Forwarded-Host). Empty = not checked.
    expected_forwarded_host: str = ""

    log_level: str = "INFO"

    # ── Phase 1: adapters (§6) ───────────────────────────────────────────
    # Master switch; tests build the app with adapters off.
    adapters_enabled: bool = True

    # OpenClaw gateway (§6.1). Empty URL disables the adapter.
    openclaw_url: str = "ws://127.0.0.1:18789"
    openclaw_token: str = ""  # gateway token (gateway.auth.mode: token)
    openclaw_display_name: str = "OpenClaw"
    # audit.activity.list poll over the WS; live broadcasts arrive push-side.
    openclaw_poll_interval_s: float = 3.0
    openclaw_rpc_timeout_s: float = 10.0

    # Hermes Agent (§6.2). Empty home disables the adapter.
    hermes_home: str = ""  # e.g. /home/pete/.hermes
    hermes_display_name: str = "Hermes"
    hermes_poll_interval_s: float = 5.0  # spec default
    # Fail-loud guard: refuse to ingest if state.db schema_version differs
    # (§6.2 'MUST NOT silently ingest garbage'). Bump deliberately after
    # reviewing upstream schema changes.
    hermes_expected_schema_version: int = 26

    # Bounded event payload text (observability copies, not approval args —
    # S7's no-truncation rule applies to approvals.tool_args, stored fully).
    event_payload_text_limit: int = 16384

    @field_validator("bind_host")
    @classmethod
    def _require_loopback(cls, value: str) -> str:
        try:
            addr = ipaddress.ip_address(value.strip())
        except ValueError as exc:  # hostname, empty, or garbage — not auditable, reject
            raise ValueError(LOOPBACK_ERROR) from exc
        if not addr.is_loopback:
            raise ValueError(LOOPBACK_ERROR)
        return value.strip()
