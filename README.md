# Mission Control

Unified operations dashboard for the self-hosted agent runtimes on Pete's
VPS — **observe, approve, and steer** OpenClaw and Hermes Agent from any
device, phone first, tailnet only.

Spec: [`mission-control-requirements.md`](mission-control-requirements.md).
Read it before touching anything; constraints **C1** (no public exposure),
**C2** (API binds 127.0.0.1 only) and **C5** (all data stays on the VPS)
are non-negotiable.

## Status

| Phase | Scope | State |
|---|---|---|
| 0 | Foundation: scaffold, Postgres 16 + Alembic baseline (§5), loopback API, §11.3 identity middleware, systemd, tailscale serve | ✅ done |
| 1 | Observation (adapters, normalizer, SSE) | — |
| 2 | Approvals gateway | — |
| 3 | Objectives & alignment | — |
| 4 | Agenda | — |
| 5 | Notifications & PWA | — |
| 6 | Hardening | — |

## Layout

```
api/                    FastAPI app (Python 3.12) + SQLAlchemy models + Alembic
  mission_control/
    auth/               operator identity path (§11.3); agent tokens land in Phase 2
    models/             every table in spec §5
  alembic/versions/     0001 = full baseline
  tests/                pytest + httpx (includes §17 acceptance tests 6 & 7)
deploy/
  postgres/initdb/      first-boot role split (mc_migrate / mc_app / mc_readonly)
  systemd/              db, migrate, api units
  tailscale/            serve setup script + ACL snippet (§11.3 layer 2)
docs/
  phase-0-runbook.md    VPS install + verification steps
  decisions.md          verification findings, deviations, open questions
scripts/verify-phase0.sh  local end-to-end verification
```

## Architecture in one line

Phone → `tailscale serve` (TLS, identity headers) → FastAPI on
`127.0.0.1:8100` (verifies the header against `tailscale whois` before
trusting it) → PostgreSQL 16 (Docker, loopback-mapped).

## Quickstart (dev)

```bash
cp .env.example .env        # fill in passwords
make setup                  # venv + deps
make test                   # 42 tests, includes §17 #6/#7 rejection cases
make db-up migrate          # Postgres 16 + schema baseline
make verify-phase0          # the whole Phase 0 checklist, locally
```

Production install is `docs/phase-0-runbook.md` — systemd units, tailscale
serve, ACL, and the tailnet-side verification (§16 Phase 0 acceptance).

## Security model (Phase 0 slice)

- The API refuses to start on a non-loopback bind; there is no override.
- Nothing except `/health` answers without a verified operator identity.
- Identity = `Tailscale-User-Login` header **and** Serve's forwarded
  headers present **and** `tailscale whois` on the forwarded source address
  agreeing with the claimed login. Any failure anywhere → reject (fail
  closed). Funnel-marked requests are rejected outright.
- DB roles: `mc_migrate` owns DDL; `mc_app` runs the API and cannot
  UPDATE/DELETE `audit_log`; `mc_readonly` reads.
