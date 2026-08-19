# Phase 2 runbook — approvals

Builds on Phase 0 and Phase 1. All steps on the VPS.

Phase 2 is the security boundary: after this, an agent cannot take a gated
action without an operator's answer. Read `docs/decisions.md` §P2 first —
two runtime settings can silently defeat the whole gate, and neither is
Mission Control's to enforce from code.

## 1. Update and migrate

```bash
cd /opt/mission-control && sudo git pull
sudo -u missionctl api/.venv/bin/pip install -e "api"
sudo systemctl restart mission-control-migrate     # applies 0003 + 0004
sudo systemctl restart mission-control-api
cd web && npm ci && npm run build && sudo systemctl restart mission-control-api
```

## 2. Configure the workspace allowlist

Writes outside these roots classify as `critical` and can never be
auto-approved (§7.4). Leaving it empty is safe but noisy — nothing can be
proven contained, so every path write asks.

```
MC_WORKSPACE_ALLOWLIST=/home/pete/work,/opt/agent-workspace
```

## 3. Mint service tokens (one per agent)

Agents must already be registered, which happens automatically the first
time the adapters connect. Then:

```bash
curl -s --unix-socket /dev/null http://127.0.0.1:8100/v1/agents   # or read the dashboard
cd /opt/mission-control/api
sudo -u missionctl bash -c 'set -a; . /etc/mission-control/env; set +a; \
  .venv/bin/python -m mission_control.tokens_cli create \
    --name hermes --agent <hermes-agent-uuid> --created-by pete'
```

The token prints once. `approvals:decide` is not a mintable scope — the
CLI refuses it — so a leaked agent token can create and poll requests but
can never answer one (C4).

## 4. OpenClaw — the RPC route

Mission Control joins the gateway as an operator client holding
`operator.approvals` and resolves approvals over the same socket. Nothing
is installed into OpenClaw.

**4a. Arm the gate.** These are the settings that decide whether approvals
are raised at all, and whether a missed decision denies:

```bash
openclaw exec-policy get --json          # inspect
openclaw exec-policy set --ask-fallback deny
```

`askFallback` must be `deny` at `defaults` **and every** `agents.<id>`
entry. With `full`, a timed-out or unrouted approval EXECUTES — the
opposite of a gate. The `yolo` preset sets exactly that, so check after
any preset change. Mission Control re-checks on every reconnect and shows
the adapter degraded when it cannot verify, but the bridge deliberately
holds only `operator.approvals` while `exec.approvals.*` requires
`operator.admin`, so the authoritative check is this CLI, run by you.

Also confirm approvals are actually raised: `security: "full"` or
`ask: "off"` means nothing ever reaches the queue.

**4b. Make Mission Control a visible reviewer.** Pending approvals are
only visible to admins, the internal runtime, an exact requester, or a
paired device listed in `approvalReviewerDeviceIds`. Pair Mission Control
as a device and add its id there. Do **not** grant `operator.admin`
instead — that would make the bridge credential remote-execution-grade.

**4c. Liveness drill (do this once, and after any OpenClaw upgrade).**
An empty queue is indistinguishable from a broken subscription, so prove
the path end to end:

```bash
# Trigger any gated command in OpenClaw, then within 15 minutes:
#   1. it appears in Mission Control's queue
#   2. deny it from the phone
#   3. the command is refused in OpenClaw
```

If step 1 fails, 4b is not done. Nothing else in Phase 2 matters until
this drill passes.

**Known gap:** `system-agent` approvals have no enumeration path and are
**not** gated by Mission Control. Only exec and plugin approvals are.

## 5. Hermes — the transport plugin

```bash
sudo -u pete mkdir -p ~pete/.hermes/plugins/mission-control-approval
sudo -u pete cp /opt/mission-control/agents/hermes-approval-transport/{plugin.yaml,__init__.py} \
  ~pete/.hermes/plugins/mission-control-approval/
sudo -u pete install -m 0600 /dev/stdin ~pete/.hermes/mission-control.token <<< 'mc_...'
```

In `~pete/.hermes/cli-config.yaml`:

```yaml
plugins:
  enabled: [mission-control-approval]
security:
  approval:
    transport: mission-control
    transport_fallback: deny
approvals:
  mode: manual        # REQUIRED — see below
  timeout: 300
```

`approvals.mode: manual` is not optional. In `smart` mode an auxiliary LLM
approves or denies *before* the transport runs, so Mission Control never
sees those requests — the queue would look calm while commands executed.
The plugin logs CRITICAL at startup if the mode is anything else; check
`~pete/.hermes/logs/agent.log` after restarting Hermes.

## 6. Verification — the §17 list

Automated (`cd api && .venv/bin/pytest`, 114 tests) covers 1, 2, 3, 4, 5,
8, 9, 11, 12, plus single-use claims and verdict delivery. `cd web && npm
test` covers 10. Tests 6 and 7 are the Phase 0 identity suite.

On the device, with the app installed to the home screen:

- **13 — airplane mode.** Open a pending approval, enable airplane mode,
  wait past 60s. The buttons must disable and say why, and the header must
  show the last sync time. Re-enable; the buttons return.
- **1 end to end, timed.** From the notification-less cold state: unlock,
  open, decide. SC1 wants this under 30 seconds. Push notifications land
  in Phase 5 — until then the queue is pull-only, so measure honestly.
- **Undo.** Tap Approve, then Undo within 5 seconds. Nothing is sent — the
  server should show the approval still pending.
- **Kill switch.** Home → Approvals → Pause all agents. Confirm the red
  banner appears everywhere, pending requests flip to denied, and a new
  agent request is refused. Then resume.

## 7. Operating notes

- **Undelivered verdicts.** If the queue shows "decisions could not be
  delivered", the runtime never received your answer and is falling back
  to its own timeout. Check the agent's connection; delivery retries every
  30s automatically.
- **Expiry rate is a health signal** (§7.2). A rising number of `expired`
  approvals means the notification path is broken, not that you are
  indecisive.
- **Rate-limit denials** (20/hour/agent by default) raise a distinct alert
  in the live tail. A flood is a known attack shape — treat a burst of
  benign-looking requests as suspicious, not as noise.
