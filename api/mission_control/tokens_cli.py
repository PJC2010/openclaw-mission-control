"""Bootstrap CLI for agent service tokens (§7.5).

    python -m mission_control.tokens_cli list
    python -m mission_control.tokens_cli create --name openclaw --agent <agent-uuid>
    python -m mission_control.tokens_cli revoke --id <token-uuid>

The plaintext is printed once, at creation, and never stored — only its
sha256 lives in the database (§12 S5). Minting is a shell operation on the
VPS on purpose: it is the credential that lets an agent ask for approval,
and it should not be obtainable over the network.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid

from sqlalchemy import select

from .auth.service_token import (
    MINTABLE_SCOPES,
    ScopeNotMintable,
    generate_token,
    validate_scopes,
)
from .config import Settings
from .db import build_async_engine, build_async_session_factory
from .models import Agent, AuditLog, ServiceToken
from .models.enums import ActorSource


async def _run(args: argparse.Namespace) -> int:
    settings = Settings()
    engine = build_async_engine(settings)
    sessions = build_async_session_factory(engine)
    try:
        async with sessions() as session:
            if args.command == "list":
                tokens = (await session.scalars(select(ServiceToken))).all()
                if not tokens:
                    print("no service tokens")
                for token in tokens:
                    state = "REVOKED" if token.revoked_at else "active"
                    print(
                        f"{token.id}  {token.name:<20} {token.token_prefix}  "
                        f"{state:<8} scopes={','.join(token.scopes)} agent={token.agent_id}"
                    )
                return 0

            if args.command == "create":
                agent = await session.get(Agent, uuid.UUID(args.agent))
                if agent is None:
                    print(
                        f"error: no agent {args.agent}. Start the API once so the "
                        "adapters register their agents, then list them with "
                        "`curl -s localhost:8100/v1/agents` or in the dashboard.",
                        file=sys.stderr,
                    )
                    return 2
                try:
                    scopes = validate_scopes(args.scopes.split(","))
                except ScopeNotMintable as exc:
                    print(f"error: {exc}", file=sys.stderr)
                    return 2
                plaintext, prefix, digest = generate_token()
                token = ServiceToken(
                    name=args.name,
                    token_hash=digest,
                    token_prefix=prefix,
                    scopes=scopes,
                    agent_id=agent.id,
                    created_by=args.created_by,
                )
                session.add(token)
                session.add(
                    AuditLog(
                        actor=args.created_by,
                        actor_source=ActorSource.SYSTEM,
                        action="service_token.created",
                        entity_type="service_token",
                        entity_id=prefix,
                        after={"name": args.name, "scopes": scopes, "agent": str(agent.id)},
                    )
                )
                await session.commit()
                print(f"agent:  {agent.display_name} ({agent.id})")
                print(f"scopes: {','.join(scopes)}")
                print("\ntoken (shown once — copy it now):\n")
                print(f"  {plaintext}\n")
                return 0

            if args.command == "revoke":
                token = await session.get(ServiceToken, uuid.UUID(args.id))
                if token is None:
                    print("error: no such token", file=sys.stderr)
                    return 2
                import datetime

                token.revoked_at = datetime.datetime.now(datetime.timezone.utc)
                session.add(
                    AuditLog(
                        actor=args.created_by,
                        actor_source=ActorSource.SYSTEM,
                        action="service_token.revoked",
                        entity_type="service_token",
                        entity_id=str(token.id),
                        after={"name": token.name},
                    )
                )
                await session.commit()
                print(f"revoked {token.name} ({token.id})")
                return 0
        return 1
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Mission Control service tokens")
    parser.add_argument("--created-by", default="cli", help="recorded in the audit log")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    create = sub.add_parser("create")
    create.add_argument("--name", required=True)
    create.add_argument("--agent", required=True, help="agents.id UUID this token speaks for")
    create.add_argument(
        "--scopes",
        default=",".join(sorted(MINTABLE_SCOPES)),
        help=f"comma-separated; only {sorted(MINTABLE_SCOPES)} are mintable",
    )
    revoke = sub.add_parser("revoke")
    revoke.add_argument("--id", required=True)
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
