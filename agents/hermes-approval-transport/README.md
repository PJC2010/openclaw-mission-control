# Mission Control approval transport for Hermes Agent

Routes every gated Hermes command to Mission Control for a human decision
(spec §7.6). Fails closed on every error path.

## Install

```bash
sudo -u pete mkdir -p ~/.hermes/plugins/mission-control-approval
sudo -u pete cp plugin.yaml __init__.py ~/.hermes/plugins/mission-control-approval/

# The service token, minted on the VPS (see docs/phase-2-runbook.md):
sudo -u pete install -m 0600 /dev/stdin ~/.hermes/mission-control.token <<< 'mc_...'
```

## Configure (`~/.hermes/cli-config.yaml`)

```yaml
plugins:
  enabled: [mission-control-approval]

security:
  approval:
    transport: mission-control
    transport_fallback: deny     # anything other than "builtin" fails closed

approvals:
  mode: manual                   # REQUIRED — see below
  timeout: 300                   # top-level, not under security.approval
```

`approvals.mode: manual` is not optional. In `smart` mode an auxiliary LLM
runs *before* the transport and an APPROVE verdict returns immediately, so
Mission Control would never see the request — the queue would look calm
while commands executed. In `off` mode nothing is gated at all. The plugin
logs a CRITICAL line at startup if the mode is anything else.

## What it sends

The complete command, its description, the pattern keys, Hermes's own
digest, and the run correlation captured from the `pre_approval_request`
hook. Mission Control classifies risk itself (§7.4) and never trusts a
risk level supplied by the agent.

## What it returns

Only `once` or `deny`. One approval, one execution — `session` and
`always` would cache permission across calls, which §7.6 forbids.

## Verifying it fails closed

```bash
sudo systemctl stop mission-control-api
# then trigger any gated command in Hermes; it must be refused, not run.
sudo systemctl start mission-control-api
```
