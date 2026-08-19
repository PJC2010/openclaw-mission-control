"""Durable adapter high-water marks (§6.2 'track a high-water mark per
table'; §6.1 reconnect backfill).

One row per (agent, source stream). `cursor` is adapter-defined JSON —
e.g. {"sequence": 812} for the OpenClaw audit ledger, {"rowid": 90412}
for Hermes messages — so adapters resume exactly where they stopped
across process restarts instead of re-ingesting or gapping.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import ForeignKey, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class AdapterCursor(Base):
    __tablename__ = "adapter_cursors"

    agent_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), primary_key=True
    )
    source: Mapped[str] = mapped_column(Text, primary_key=True)
    cursor: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    updated_at: Mapped[datetime.datetime] = mapped_column(
        server_default=text("now()"), onupdate=text("now()")
    )
