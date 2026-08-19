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
| 1 | Observation: OpenClaw + Hermes adapters, normalizer, runs/events, agent liveness, SSE live tail, run list/detail UI | ✅ done |
| 2 | Approvals: gateway, service tokens, OpenClaw RPC bridge + Hermes transport, policy engine, kill switch, decision UI | ✅ done |
| 3 | Objectives & alignment | next — needs your objectives (§18.3) |
| 4 | Agenda | — |
| 5 | Notifications & PWA | — |
| 6 | Hardening | — |

## Layout

```
api/                    FastAPI app (Python 3.12) + SQLAlchemy models + Alembic
  mission_control/
    auth/               two disjoint credential paths: operator identity (§11.3)
                        and agent service tokens (§7.5)
    models/             every table in spec §5 (+ adapter_cursors)
    adapters/           §6: openclaw (WS, audit ledger), hermes (read-only files)
    approvals/          §7: risk classifier, policy engine, gateway service
    bridges/            carries operator verdicts back into the runtimes
    normalizer/         §6.3: the only writer of runs/events; dedupe, seq, broadcast
    routes/             health, agents, runs, SSE stream, system health, whoami
  alembic/versions/     0001 baseline · 0002 cursors · 0003 approvals · 0004 delivery
  tests/                pytest + httpx; fake OpenClaw gateway; on-disk Hermes fixture
web/                    Next.js static export (dark, phone-first) served by the API
agents/                 the Hermes approval transport plugin (installed into Hermes)
deploy/
  postgres/initdb/      first-boot role split (mc_migrate / mc_app / mc_readonly)
  systemd/              db, migrate, api units
  tailscale/            serve setup script + ACL snippet (§11.3 layer 2)
docs/
  phase-0-runbook.md    VPS install + verification steps
  phase-1-runbook.md    adapter config, web build, §16 P1 acceptance checks
  phase-2-runbook.md    tokens, arming both runtimes' gates, §17 verification
  roadmap.md            plan to completion (phases 3-6) and what it needs from you
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
make db-up migrate          # Postgres 16 + schema to head
make test                   # 114 API tests, incl. most of the §17 list
cd web && npm ci && npm test && npm run build   # §17 #10 + the static export
make verify-phase0          # the Phase 0 checklist, locally
```

Deployment is `docs/phase-0-runbook.md` → `phase-1` → `phase-2`, in order.
`docs/roadmap.md` is the plan for what remains.

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
- Agents authenticate with a separate credential class that structurally
  cannot decide: `approvals:decide` is not a mintable scope (C4).
- A gated action needs an explicit human answer carrying a verified
  tailnet identity, or an explicit allowlist policy match. Critical-class
  actions (§7.4) never auto-approve. One approval authorises exactly one
  execution. Every failure path — unreachable, timed out, ambiguous,
  paused — denies.
