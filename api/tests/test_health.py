"""Liveness endpoint contract."""

from __future__ import annotations

import pytest

from .conftest import build_app, client_for

pytestmark = pytest.mark.anyio


async def test_health_shape():
    async with client_for(build_app()) as client:
        response = await client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "mission-control"
    assert "version" in body and "time" in body
