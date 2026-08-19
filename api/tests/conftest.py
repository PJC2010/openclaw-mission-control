"""Shared fixtures.

Requests are driven through httpx.ASGITransport so the ASGI `client` peer
address is controllable — the §11.3 layer-1 loopback check depends on it.
"""

from __future__ import annotations

import os
import secrets
import uuid
from pathlib import Path
from typing import AsyncIterator

import httpx
import pytest

from mission_control.app import create_app
from mission_control.auth.tailscale import WhoisIdentity
from mission_control.config import Settings

OPERATOR_LOGIN = "castillop92@gmail.com"
TAILNET_IP = "100.101.102.103"
SERVE_HOST = "vps.tail1234.ts.net"

API_DIR = Path(__file__).resolve().parent.parent

# Matches the dev credentials in the repo-root .env workflow; override for
# other environments. DB-backed tests skip cleanly when Postgres is absent.
TEST_ADMIN_URL = os.environ.get(
    "MC_TEST_ADMIN_URL", "postgresql://postgres:devsuper123@127.0.0.1:5432/postgres"
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def make_settings(**overrides) -> Settings:
    overrides.setdefault("adapters_enabled", False)
    return Settings(_env_file=None, **overrides)


# ── Postgres-backed fixtures ─────────────────────────────────────────────


def _admin_connect():
    import psycopg

    return psycopg.connect(TEST_ADMIN_URL, autocommit=True)


@pytest.fixture(scope="session")
def test_db_url() -> str:
    """Fresh database migrated to head; dropped afterwards."""
    try:
        conn = _admin_connect()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"postgres unavailable for db tests: {exc}")
    db_name = f"mc_test_{secrets.token_hex(4)}"
    with conn:
        conn.execute(f'CREATE DATABASE "{db_name}"')
    admin_dsn = TEST_ADMIN_URL.rsplit("/", 1)[0]
    url = f"postgresql+psycopg://{admin_dsn.split('://', 1)[1]}/{db_name}"

    from alembic import command
    from alembic.config import Config

    previous = os.environ.get("MC_MIGRATE_DATABASE_URL")
    os.environ["MC_MIGRATE_DATABASE_URL"] = url
    try:
        cfg = Config(str(API_DIR / "alembic.ini"))
        cfg.set_main_option("script_location", str(API_DIR / "alembic"))
        command.upgrade(cfg, "head")
    finally:
        if previous is None:
            os.environ.pop("MC_MIGRATE_DATABASE_URL", None)
        else:
            os.environ["MC_MIGRATE_DATABASE_URL"] = previous

    yield url

    with _admin_connect() as conn:
        conn.execute(f'DROP DATABASE "{db_name}" WITH (FORCE)')


@pytest.fixture
async def db_sessions(test_db_url):
    """Async sessionmaker on a truncated (clean) schema."""
    from sqlalchemy import text
    from mission_control.db import build_async_engine, build_async_session_factory

    settings = make_settings(database_url=test_db_url)
    engine = build_async_engine(settings)
    factory = build_async_session_factory(engine)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE notifications, audit_log, outcomes, approval_policies, "
                "approvals, adapter_cursors, scheduled_tasks, events, runs, agents, "
                "objectives RESTART IDENTITY CASCADE"
            )
        )
    yield factory
    await engine.dispose()


@pytest.fixture
async def normalizer(db_sessions):
    from mission_control.normalizer import Normalizer

    return Normalizer(db_sessions)


@pytest.fixture
async def hermes_agent_id(normalizer) -> uuid.UUID:
    from mission_control.models.enums import AgentRuntime

    return await normalizer.register_agent(AgentRuntime.HERMES, "hermes:test", "Hermes Test")


async def ok_resolver(source_ip: str) -> WhoisIdentity | None:
    """Happy-path fake tailscaled: the tailnet IP belongs to the operator."""
    if source_ip == TAILNET_IP:
        return WhoisIdentity(login=OPERATOR_LOGIN, display_name="Pete", node_name="phone.ts.net.")
    return None


def build_app(resolver=ok_resolver, **settings_overrides):
    return create_app(settings=make_settings(**settings_overrides), whois_resolver=resolver)


def client_for(app, peer_ip: str = "127.0.0.1") -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, client=(peer_ip, 54321))
    return httpx.AsyncClient(transport=transport, base_url="http://mission-control.test")


def serve_headers(
    login: str = OPERATOR_LOGIN,
    source_ip: str = TAILNET_IP,
    host: str = SERVE_HOST,
    proto: str = "https",
) -> dict[str, str]:
    """The header shape tailscale serve puts on a proxied request."""
    return {
        "Tailscale-User-Login": login,
        "Tailscale-User-Name": "Pete Castillo",
        "X-Forwarded-For": source_ip,
        "X-Forwarded-Proto": proto,
        "X-Forwarded-Host": host,
    }


@pytest.fixture
async def operator_client() -> AsyncIterator[httpx.AsyncClient]:
    async with client_for(build_app()) as client:
        yield client


@pytest.fixture
def security_events():
    """Collect `mission_control.security` records directly.

    A dedicated handler rather than `caplog`: security logging is a hard
    requirement (§17 tests 5/8), so the assertion should not depend on
    pytest's root-propagation behaviour.
    """
    import logging

    records: list[logging.LogRecord] = []

    class Collector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger("mission_control.security")
    handler = Collector(level=logging.DEBUG)
    previous = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)


def security_text(records) -> str:
    # getMessage() already interpolates record.args — do not format twice.
    return "\n".join(record.getMessage() for record in records)
