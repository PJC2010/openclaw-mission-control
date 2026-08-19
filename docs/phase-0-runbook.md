# Phase 0 runbook — VPS install and verification

Everything below runs **on the VPS**. Steps are idempotent unless marked.
Assumes: Linux with systemd, Docker + compose plugin, Tailscale installed
and logged in (§2 — already true on this host), Python 3.12.

## 1. Deploy the repo

```bash
sudo mkdir -p /opt/mission-control /etc/mission-control
sudo git clone <repo-url> /opt/mission-control    # or rsync a checkout
cd /opt/mission-control
```

Service user (no shell, no home writes):

```bash
sudo useradd --system --home-dir /opt/mission-control --shell /usr/sbin/nologin missionctl || true
sudo usermod -aG docker missionctl   # only if the db unit should run unprivileged; else run db unit as root
```

## 2. Secrets

```bash
sudo cp .env.example /etc/mission-control/env
sudo chown root:missionctl /etc/mission-control/env
sudo chmod 0640 /etc/mission-control/env
sudo nano /etc/mission-control/env     # set all four passwords (openssl rand -hex 32) and both DB URLs
```

`.env` in the repo root is for dev only and is git-ignored (S5). If you
prefer systemd credentials over EnvironmentFile later, the units are the
only place that reference the path.

## 3. Python environment

```bash
cd /opt/mission-control
python3.12 -m venv api/.venv
api/.venv/bin/pip install -e "api"
sudo chown -R missionctl:missionctl api/.venv
```

## 4. tailscaled socket access for whois (§11.3 layer 3)

The API shells out to `tailscale whois`; a non-root user needs operator
access to the local daemon:

```bash
sudo tailscale set --operator=missionctl
# sanity, as missionctl:
sudo -u missionctl tailscale whois --json <your-phone-tailnet-ip> | head
```

## 5. systemd units

```bash
sudo cp deploy/systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mission-control-db
sudo systemctl enable --now mission-control-migrate
sudo systemctl enable --now mission-control-api
systemctl status mission-control-api --no-pager
```

First DB start creates the role split via
`deploy/postgres/initdb/01-roles.sh` (first boot of the data volume only —
wipe the `pgdata` volume if you need it to re-run).

## 6. tailscale serve (§11.1)

One-time tailnet admin console prerequisites:
- **MagicDNS** enabled
- **HTTPS certificates** enabled (DNS → HTTPS Certificates)

Then:

```bash
sudo MC_BIND_PORT=8100 deploy/tailscale/serve-setup.sh
```

The script refuses to run if Funnel is active (C1) and prints
`tailscale serve status` plus the dashboard URL.

## 7. Tailnet ACL (§11.3 layer 2)

Merge `deploy/tailscale/acl-snippet.hujson` into the tailnet policy
(admin console → Access Controls): only the operator's login may reach
`<vps>:443`, and the default allow-all rule must be gone. Identity headers
are only populated for user-owned devices, so tagged nodes could never
authenticate anyway — the ACL keeps them from even knocking.

## 8. Verification (the §19 hand-back)

**a. serve status** — capture the output:

```bash
tailscale serve status
# expected shape:
# https://<vps>.<tailnet>.ts.net (tailnet only)
# |-- / proxy http://127.0.0.1:8100
tailscale funnel status   # must show nothing serving
```

**b. migration head:**

```bash
cd /opt/mission-control/api
sudo -u missionctl bash -c 'set -a; . /etc/mission-control/env; set +a; .venv/bin/alembic current'
# expected: 0001 (head)
```

**c. on-tailnet, from the phone** (§16 Phase 0 acceptance): open
`https://<vps>.<tailnet>.ts.net/` — the hello page renders over HTTPS and
shows the operator login under "Operator" (that value came from
`tailscale whois`, not from the header — §11.3 layer 3 working end to end).
`/health` also answers.

**d. off-tailnet — must fail:** from a device with Tailscale disconnected
(or any non-tailnet network):

```bash
curl -v --max-time 10 https://<vps>.<tailnet>.ts.net/health
# expected: DNS resolution failure or connect timeout. Anything else means
# the name is public — stop and investigate Funnel/DNS immediately (C1).
```

Also confirm the raw API port is not exposed: from another tailnet device,
`curl --max-time 5 http://<vps-tailnet-ip>:8100/health` must fail (C2 —
the bind is loopback; SC4 requires no public inbound port at all, which the
VPS firewall / provider config must uphold independently).

**e. local checks** (same box):

```bash
cd /opt/mission-control && cp .env.example .env  # dev copy for the script, or point it at /etc/mission-control/env
./scripts/verify-phase0.sh
```

## 9. Operations notes

- **Logs:** `journalctl -u mission-control-api -f`. Identity rejections log
  as `mission_control.security` warnings with an `event=` tag.
- **Restart order** is encoded in the units (db → migrate → api); a full
  `sudo reboot` must bring everything back with no manual steps (that's the
  Phase 6 acceptance, but the wiring exists now).
- **Backups, log rotation, restore drills:** Phase 6.
- The whois result is cached for 60s per source IP; a device re-auth as a
  different user is picked up within that window.
