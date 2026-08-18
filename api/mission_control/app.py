"""FastAPI application factory."""

from __future__ import annotations

import logging

from fastapi import FastAPI

from . import __version__
from .auth.identity import OperatorIdentityMiddleware
from .auth.tailscale import CliWhoisResolver, TtlCachingResolver, WhoisResolver
from .config import Settings
from .routes import health, home


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

    app = FastAPI(
        title="Mission Control",
        version=__version__,
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

    app.include_router(health.router)
    app.include_router(home.router)

    # Outermost: nothing except /health is reachable without verified identity.
    app.add_middleware(
        OperatorIdentityMiddleware,
        settings=settings,
        whois_resolver=whois_resolver,
    )
    return app
