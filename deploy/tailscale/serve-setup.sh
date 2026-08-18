#!/usr/bin/env bash
# Configure `tailscale serve` to front Mission Control on the tailnet (§11.1).
#
# Serve — never Funnel (C1). Serve terminates TLS with an auto-provisioned
# cert on the node's MagicDNS name and injects the identity + forwarded
# headers the API's §11.3 middleware verifies.
#
# Prerequisites (one-time, tailnet admin console):
#   - MagicDNS enabled
#   - HTTPS certificates enabled  (DNS → HTTPS Certificates → Enable)
#
# Run on the VPS as root (or a tailscale operator user):
#   MC_BIND_PORT=8100 ./serve-setup.sh
set -euo pipefail

PORT="${MC_BIND_PORT:-8100}"

# Hard stop if Funnel is active anywhere on this node — C1 is non-negotiable.
if tailscale funnel status 2>/dev/null | grep -qi "funnel on"; then
    echo "REFUSING: Tailscale Funnel is enabled on this node. Disable it first" >&2
    echo "(tailscale funnel reset) — Mission Control must never be public (C1)." >&2
    exit 1
fi

# CLI syntax verified against the tailscale serve reference, 2026-08-18:
#   tailscale serve [--bg] --https=<port> <target>
tailscale serve --bg --https=443 "http://127.0.0.1:${PORT}"

echo
tailscale serve status
echo
DNS_NAME="$(tailscale status --json | sed -n 's/.*"DNSName": *"\([^"]*\)".*/\1/p' | head -1 | sed 's/\.$//')"
echo "Dashboard URL: https://${DNS_NAME}/"
echo
echo "Verify from a tailnet device:  https://${DNS_NAME}/health"
echo "Verify from OFF the tailnet:   the same URL must not resolve/connect."
echo "Reminder: apply the port-443 ACL (see acl-snippet.hujson) — §11.3 layer 2."
