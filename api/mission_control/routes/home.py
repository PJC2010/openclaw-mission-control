"""Phase 0 hello page and identity echo.

The Next.js PWA replaces `/` in later phases; this static page exists to
prove the full path — phone → tailscale serve (TLS) → loopback API →
identity verification — end to end (§16 Phase 0 acceptance).
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse

router = APIRouter()

_INDEX = Path(__file__).resolve().parent.parent / "static" / "index.html"


@router.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(_INDEX, media_type="text/html")


@router.get("/v1/whoami")
async def whoami(request: Request) -> dict[str, str]:
    """Echo the verified operator identity (set by the identity middleware).

    Useful on-device check that layer 3 verification is actually running:
    the login shown comes from `tailscale whois`, not from the raw header.
    """
    operator = request.state.operator
    return {
        "login": operator.login,
        "display_name": operator.display_name,
        "source_ip": operator.source_ip,
        "verified_via": "tailscale-serve+whois",
    }
