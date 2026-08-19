"""Entry point: `python -m mission_control` (used by the systemd unit)."""

from __future__ import annotations

import ipaddress

import uvicorn

from .app import create_app
from .config import Settings


def main() -> None:
    settings = Settings()
    # Belt over the Settings validator: refuse to serve on anything but loopback (C2).
    assert ipaddress.ip_address(settings.bind_host).is_loopback, "C2: loopback bind only"
    uvicorn.run(
        create_app(settings),
        host=settings.bind_host,
        port=settings.bind_port,
        log_level=settings.log_level.lower(),
        # CRITICAL: uvicorn must NOT rewrite scope["client"]/scheme from
        # X-Forwarded-*. The identity middleware parses those headers itself
        # and needs the true loopback peer for its layer-1 check; letting
        # uvicorn "trust" forwarded headers would hand the peer identity to
        # whoever set the header.
        proxy_headers=False,
        server_header=False,
        date_header=True,
    )


if __name__ == "__main__":
    main()
