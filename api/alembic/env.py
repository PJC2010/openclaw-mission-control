"""Alembic environment.

Migrations run as the `mc_migrate` role (owns all DDL — §12 S4); the URL
comes from the environment, never from a committed file:

    MC_MIGRATE_DATABASE_URL  (preferred)
    MC_DATABASE_URL          (fallback, e.g. throwaway dev databases)
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from mission_control.models import Base

config = context.config
if config.config_file_name is not None:
    # disable_existing_loggers defaults to True, which would silently switch
    # off every already-configured logger in the process — including
    # `mission_control.security`. Harmless when alembic runs as its own
    # systemd oneshot, but catastrophic (and invisible) if migrations are
    # ever run in-process: security events would simply stop being emitted.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata

# Dialect placeholder keeps `alembic upgrade --sql head` (offline SQL review)
# working without credentials.
OFFLINE_PLACEHOLDER_URL = "postgresql+psycopg://offline/mission_control"


def _database_url() -> str | None:
    return (
        os.environ.get("MC_MIGRATE_DATABASE_URL")
        or os.environ.get("MC_DATABASE_URL")
        or (config.get_main_option("sqlalchemy.url") or None)
    )


def run_migrations_offline() -> None:
    url = _database_url() or OFFLINE_PLACEHOLDER_URL
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    url = _database_url()
    if not url:
        raise SystemExit(
            "No database URL: set MC_MIGRATE_DATABASE_URL (see .env.example)."
        )
    engine = create_engine(url, poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
