# Phase 1 runbook — observation (read-only)

Adds on top of the Phase 0 install (docs/phase-0-runbook.md). All on the VPS.

## 0. Pre-flight — verify installed runtime versions (decisions.md A4)

The adapters were built against **OpenClaw 2026.7.x** (gateway protocol 4,
`audit.activity.list`) and **Hermes Agent 0.20.x** (state.db schema v26).
Before enabling them:

```bash
openclaw --version
hermes --version 2>/dev/null || echo "check hermes install"
sqlite3 ~pete/.hermes/state.db "SELECT version FROM schema_version;"   # expect 26
ls ~pete/.hermes/cron/jobs.json ~pete/.hermes/cron/executions.db
```

A different Hermes schema version makes the adapter refuse to ingest (by
design, §6.2) until `MC_HERMES_EXPECTED_SCHEMA_VERSION` is deliberately
updated after reviewing upstream changes. An older OpenClaw without
`audit.activity.list` degrades the adapter with a clear health message.

## 1. Update the deployment

```bash
cd /opt/mission-control && sudo git pull
sudo -u missionctl api/.venv/bin/pip install -e "api"
sudo systemctl restart mission-control-migrate     # applies 0002 (adapter_cursors)
```

## 2. Configure the adapters

In `/etc/mission-control/env` (see .env.example):

- `MC_OPENCLAW_URL=ws://127.0.0.1:18789` (or the gateway's configured port)
- `MC_OPENCLAW_TOKEN=…` — an operator token for the gateway
  (`gateway.auth.mode: token`). Mission Control connects with role
  `operator`, scope `operator.read` only.
- `MC_HERMES_HOME=/home/pete/.hermes`

Filesystem access for the Hermes adapter (read-only, enforced twice):

```bash
# let missionctl traverse+read the hermes state
sudo setfacl -R -m u:missionctl:rX /home/pete/.hermes
sudo setfacl -d -m u:missionctl:rX /home/pete/.hermes
# the unit additionally binds the path read-only inside the sandbox:
#   deploy/systemd/mission-control-api.service → BindReadOnlyPaths=-/home/pete/.hermes
sudo cp deploy/systemd/mission-control-api.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl restart mission-control-api
```

## 3. Build the web app (once per update)

Requires Node 22+ on the VPS (or build elsewhere and rsync `web/out`):

```bash
cd /opt/mission-control/web
npm ci && npm run build          # output: web/out, served by the API at /
sudo systemctl restart mission-control-api
```

Without a build the API falls back to the Phase 0 hello page; `/v1/*` works
either way.

## 4. Acceptance checks (§16 Phase 1)

1. **Activity appears within 10s.** Send a message to each runtime (any
   channel) and watch `https://<vps>.<tailnet>.ts.net/` — the run appears in
   Recent runs and the Live tail moves. (OpenClaw audit poll is 3s; Hermes
   poll is 5s.)
2. **Kill/restart is visible.**
   `sudo systemctl stop <openclaw unit>` → agent card flips to `down` within
   ~30s (missed ticks); start it again → `up`, with the transition recorded
   as a lifecycle event in the tail. Same for the Hermes daemon (heartbeat
   staleness threshold: 180s).
3. **Adapter reconnects after a drop.** While tailing
   `journalctl -u mission-control-api -f`, restart the OpenClaw gateway:
   expect `gateway connection lost`, backoff logs, then
   `connected to openclaw gateway …` and no gap in run history (the audit
   cursor resumes; §6.1).
4. **Read-only invariant.** `sudo -u missionctl touch /home/pete/.hermes/x`
   must fail (read-only bind).

## 5. What is deliberately NOT here yet

Approvals (Phase 2), objectives/tagging (Phase 3), agenda view & write-back
(Phase 4), push notifications and PWA install polish (Phase 5).
