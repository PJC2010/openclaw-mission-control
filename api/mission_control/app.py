"""FastAPI application factory."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import __version__
from .auth.identity import OperatorIdentityMiddleware
from .auth.tailscale import CliWhoisResolver, TtlCachingResolver, WhoisResolver
from .config import Settings
from .db import build_async_engine, build_async_session_factory
from .approvals import ApprovalService
from .normalizer import Normalizer
from .routes import agents, approvals, health, home, runs, stream, system
from .runtime import AdapterSupervisor

log = logging.getLogger("mission_control.app")

# Static export of the web app (web/ → `npm run build` → out/), served from
# the same origin (§4: no CORS, one deploy target). Falls back to the Phase 0
# hello page when no build is present.
WEB_BUILD_DIR = Path(__file__).resolve().parent.parent.parent / "web" / "out"


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # Security events must surface regardless of the app log level.
    logging.getLogger("mission_control.security").setLevel(logging.INFO)


def create_app(
    settings: Settings | None = None,
    whois_resolver: WhoisResolver | None = None,
    peer_resolver=None,
) -> FastAPI:
    """Build the app.

    `whois_resolver` is injectable so the §11.3 verification logic is unit
    testable without a tailscaled; production composes the real CLI resolver
    behind a short positive-result cache.
    """
    settings = settings or Settings()
    configure_logging(settings.log_level)

    if whois_resolver is None:
        whois_resolver = TtlCachingResolver(
            CliWhoisResolver(settings.tailscale_bin, settings.whois_timeout_s),
            ttl_s=settings.whois_cache_ttl_s,
        )

    # Engine construction is lazy (no connection until first use), so building
    # it eagerly keeps app.state complete for tests without a database.
    engine = build_async_engine(settings)
    db_sessions = build_async_session_factory(engine)
    normalizer = Normalizer(db_sessions)
    approval_service = ApprovalService(db_sessions, settings, normalizer=normalizer)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        supervisor: AdapterSupervisor | None = None
        # The TTL sweeper runs whenever the app runs: an approval that
        # nobody answers must expire (and therefore deny) even if every
        # adapter is disabled (§7.2).
        await approval_service.start()
        if settings.adapters_enabled:
            supervisor = AdapterSupervisor(settings, normalizer, approvals=approval_service)
            app.state.supervisor = supervisor
            await supervisor.start()
        try:
            yield
        finally:
            if supervisor is not None:
                await supervisor.stop()
            await approval_service.stop()
            await engine.dispose()

    app = FastAPI(
        title="Mission Control",
        version=__version__,
        lifespan=lifespan,
        # No interactive docs surface for now — everything served is
        # identity-gated and phone-first; revisit if the operator wants them.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    # Note: no CORS middleware, by design — the PWA is served from the same
    # origin (§4) and nothing else may call this API.
    app.state.settings = settings
    app.state.whois_resolver = whois_resolver
    app.state.db_engine = engine
    app.state.db_sessions = db_sessions
    app.state.normalizer = normalizer
    app.state.approvals = approval_service

    app.include_router(health.router)
    app.include_router(agents.router)
    app.include_router(runs.router)
    app.include_router(stream.router)
    app.include_router(system.router)
    app.include_router(approvals.router)
    app.include_router(home.router)  # /v1/whoami

    if WEB_BUILD_DIR.is_dir():
        # html=True serves index.html for directory paths; still behind the
        # identity middleware like everything except /health.
        app.mount("/", StaticFiles(directory=WEB_BUILD_DIR, html=True), name="web")
        log.info("serving web build from %s", WEB_BUILD_DIR)
    else:
        app.include_router(home.hello_router)  # Phase 0 hello page fallback

    # Outermost: nothing except /health is reachable without verified identity.
    app.add_middleware(
        OperatorIdentityMiddleware,
        settings=settings,
        whois_resolver=whois_resolver,
        peer_resolver=peer_resolver,
    )
    return app
