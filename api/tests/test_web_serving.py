"""The static web export is served from the same origin, identity-gated.
Skips when web/out has not been built (build artifacts are not committed)."""

from __future__ import annotations

import pytest

from mission_control.app import WEB_BUILD_DIR, create_app

from .conftest import make_settings, ok_resolver, root_peer, serve_headers, client_for

pytestmark = pytest.mark.anyio

needs_build = pytest.mark.skipif(
    not (WEB_BUILD_DIR / "index.html").exists(), reason="web/out not built"
)


@needs_build
async def test_web_index_served_behind_identity():
    app = create_app(settings=make_settings(), whois_resolver=ok_resolver, peer_resolver=root_peer)
    async with client_for(app) as client:
        anonymous = await client.get("/")
        assert anonymous.status_code == 401  # identity still required
        page = await client.get("/", headers=serve_headers())
        assert page.status_code == 200
        assert "text/html" in page.headers["content-type"]
        runs_page = await client.get("/runs/", headers=serve_headers())
        assert runs_page.status_code == 200
