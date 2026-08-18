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
