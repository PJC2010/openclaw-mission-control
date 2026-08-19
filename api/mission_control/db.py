"""Database engine/session factories.

Phase 0 keeps request handlers DB-free (health is a pure liveness probe);
Phase 1 wires sessions into routes. Defined now so alembic and future code
share one construction path.
"""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session, sessionmaker

from .config import Settings


def build_engine(settings: Settings) -> Engine:
    return create_engine(settings.database_url, pool_pre_ping=True)


def build_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


def build_async_engine(settings: Settings) -> AsyncEngine:
    # postgresql+psycopg:// serves both sync (alembic) and async (app) —
    # SQLAlchemy selects psycopg's async side here.
    return create_async_engine(settings.database_url, pool_pre_ping=True)


def build_async_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(bind=engine, expire_on_commit=False)
