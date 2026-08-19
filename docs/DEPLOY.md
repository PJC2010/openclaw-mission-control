# Deploying Mission Control

One page. Run it in order on the VPS. The three phase runbooks stay as
reference for what each step is doing and why.

Everything mechanical is scripted. Four steps are not, because they need
credentials or an admin console only you can reach — they are called out
in bold and the installer reminds you of them at the end.

## 0. Check before you install

```bash
git clone <repo> /opt/mission-control
cd /opt/mission-control
./scripts/preflight.sh
```

This changes nothing and answers one question: will the deployment work,
and **will the approval gate actually gate anything**. It checks the host
prerequisites, that Funnel is off (C1), that OpenClaw's `askFallback` is
`deny`, that Hermes's `state.db` schema matches what the adapter was built
against, that `approvals.mode` is `manual`, and — the one people miss —
that neither runtime is running as root.

That last check matters more than it looks. Operator identity is verified
against the UID that opened the socket, because `tailscale serve` injects
no secret and headers alone cannot prove a request came through it. An
agent running as root could set those headers itself and approve its own
requests. Fix any blocker before continuing; a blocker in the OpenClaw or
Hermes section means the gate would be decorative.

## 1. Install

```bash
sudo ./scripts/install.sh --dry-run --hermes-home /home/pete/.hermes   # look first
sudo ./scripts/install.sh --hermes-home /home/pete/.hermes
```

Creates the `missionctl` service user, generates database passwords into
`/etc/mission-control/env` (root:missionctl, 0640), builds the virtualenv,
templates and installs the three systemd units, grants tailscale operator
access, starts PostgreSQL, migrates to head, builds the web app, and
starts the API.

Idempotent: re-run it after any `git pull`. It never regenerates a secret
that already exists.

## 2. The four steps that are yours

**a. Enable HTTPS certificates** in the tailnet admin console
(DNS → HTTPS Certificates). Serve cannot provision a TLS certificate
without it, and without HTTPS there is no PWA later.

**b. Replace the tailnet's default ACL rule** with
`deploy/tailscale/acl-snippet.hujson`, scoped to your login and this
node's port 443. Layer 2 of the trust model does not exist while the
default allow-all rule is in place.

**c. Start Serve and arm the OpenClaw gate:**

```bash
sudo MC_BIND_PORT=8100 ./deploy/tailscale/serve-setup.sh   # refuses if Funnel is on
openclaw exec-policy set --ask-fallback deny
sudo -e /etc/mission-control/env    # set MC_OPENCLAW_TOKEN, MC_WORKSPACE_ALLOWLIST
sudo systemctl restart mission-control-api
```

**d. Pair Mission Control as an OpenClaw approval reviewer** and add its
device id to `approvalReviewerDeviceIds`. Pending approvals are only
visible to admins, the internal runtime, an exact requester, or a paired
reviewer device — so without this the queue is empty and looks idle. Do
not grant `operator.admin` instead; that would make the bridge credential
remote-execution-grade.

Then install the Hermes side (`agents/hermes-approval-transport/README.md`)
and mint its token:

```bash
cd /opt/mission-control/api
sudo -u missionctl bash -c 'set -a; . /etc/mission-control/env; set +a; \
  .venv/bin/python -m mission_control.tokens_cli create \
    --name hermes --agent <agent-uuid> --created-by pete'
```

The agent uuid comes from the dashboard or `/v1/agents` once the adapters
have connected once.

## 3. Verify

```bash
./scripts/preflight.sh        # should now be all green
./scripts/verify-phase2.sh    # create → decide → claim → replay refused, over real HTTP
```

Then the checks only a human can do:

- **From your phone, on the tailnet:** the dashboard loads over HTTPS and
  shows your login. That login came from `tailscale whois`, not from a
  header.
- **From off the tailnet:** `curl https://<vps>.<tailnet>.ts.net/health`
  must fail to resolve or connect. Anything else means the name is public
  — stop and check Funnel and DNS (C1, SC4).
- **The liveness drill** in `docs/phase-2-runbook.md` §4c: trigger a gated
  command in each runtime, confirm it reaches the queue, deny it, confirm
  the runtime refuses it. An empty queue and a broken subscription look
  identical from the dashboard, so this is the only way to know.
- **Airplane mode** on an open approval: the buttons must disable and say
  why, and the last-synced time must be visible.

## If something is wrong

```bash
journalctl -u mission-control-api -f          # identity rejections log with an event= tag
systemctl status mission-control-{db,migrate,api}
docker compose --env-file /etc/mission-control/env ps
```

Common causes, in the order they actually happen:

| Symptom | Cause |
|---|---|
| Every request 403s with `whois_error` | `tailscale set --operator=missionctl` did not take, or tailscaled is down |
| Every request 403s with `peer_uid_not_permitted` | Serve is not proxying as root, or something else is connecting; check `MC_OPERATOR_PEER_UIDS` |
| Dashboard loads, queue always empty | Step 2d not done — Mission Control is not a reviewer device |
| Hermes adapter degraded, "schema_version" | Hermes upgraded; review the change, then set `MC_HERMES_EXPECTED_SCHEMA_VERSION` |
| Approvals appear but never reach the runtime | Check the queue's undelivered banner; the bridge retries every 30s |

## What is not deployed by this

Push notifications (Phase 5) — until then the queue is pull-only. Database
backups (Phase 6) — worth adding early, since real history accrues from
the first adapter connection.
