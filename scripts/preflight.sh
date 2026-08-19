#!/usr/bin/env bash
# Mission Control — pre-installation check.
#
# Run this FIRST, on the VPS, before installing anything. It changes
# nothing. It answers one question: will this deployment actually work,
# and will the approval gate actually be a gate?
#
#   ./scripts/preflight.sh
#
# Exit 0 = clear to install. Exit 1 = at least one blocker.
set -uo pipefail

PASS=0; WARN=0; FAIL=0
ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; PASS=$((PASS+1)); }
warn() { printf '  \033[33m!\033[0m %s\n' "$1"; WARN=$((WARN+1)); }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$1"; FAIL=$((FAIL+1)); }
note() { printf '    %s\n' "$1"; }
head_() { printf '\n\033[1m%s\033[0m\n' "$1"; }

head_ "Host prerequisites"

if command -v systemctl >/dev/null 2>&1; then ok "systemd present"
else bad "systemd not found — the units in deploy/systemd assume it"; fi

PY=""
for cand in python3.12 python3.13 python3; do
  if command -v "$cand" >/dev/null 2>&1; then
    v=$("$cand" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null)
    if [ "${v%%.*}" = "3" ] && [ "${v#*.}" -ge 12 ] 2>/dev/null; then PY="$cand"; break; fi
  fi
done
if [ -n "$PY" ]; then ok "python $($PY -V 2>&1 | cut -d' ' -f2) ($PY)"
else bad "python 3.12+ not found — the API requires it"; fi

if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  if docker info >/dev/null 2>&1; then ok "docker + compose plugin, daemon running"
  else bad "docker installed but the daemon is not reachable (try: systemctl start docker)"; fi
else bad "docker with the compose plugin is required for PostgreSQL 16"; fi

if command -v node >/dev/null 2>&1; then
  nv=$(node -p 'process.versions.node.split(".")[0]')
  if [ "$nv" -ge 20 ]; then ok "node $(node -v) — can build the web app here"
  else warn "node $(node -v) is old; build web/ elsewhere and rsync web/out"; fi
else
  warn "node not installed — build web/ elsewhere and copy web/out, or the API"
  note "serves the Phase 0 hello page and /v1/* only"
fi

# Probe by connecting rather than trusting `ss` to exist — a silent
# "free" on a port that is actually taken sends you debugging the wrong
# thing after install.
port_busy() {
  if command -v python3 >/dev/null 2>&1; then
    python3 - "$1" <<'PY' 2>/dev/null
import socket, sys
s = socket.socket(); s.settimeout(0.4)
sys.exit(0 if s.connect_ex(("127.0.0.1", int(sys.argv[1]))) == 0 else 1)
PY
    return $?
  fi
  command -v ss >/dev/null 2>&1 && ss -ltn 2>/dev/null | grep -q ":$1 "
}
for port in 8100 5432; do
  if port_busy "$port"; then
    if [ "$port" = 5432 ]; then
      warn "port 5432 is in use — fine if it is our own Postgres container, a"
      note "conflict otherwise (docker compose ps)"
    else
      warn "port $port is already in use"
    fi
  else ok "port $port free"; fi
done

avail=$(df -Pk /opt 2>/dev/null | awk 'NR==2{print int($4/1024/1024)}')
if [ -n "${avail:-}" ] && [ "$avail" -ge 5 ]; then ok "${avail}G free on /opt"
elif [ -n "${avail:-}" ]; then warn "only ${avail}G free on /opt (Postgres + images want ~5G)"; fi

head_ "Tailscale (C1: tailnet only, never Funnel)"

if command -v tailscale >/dev/null 2>&1; then
  ok "tailscale $(tailscale version 2>/dev/null | head -1)"
  if tailscale status >/dev/null 2>&1; then
    ok "tailscaled running and logged in"
    dns=$(tailscale status --json 2>/dev/null | sed -n 's/.*"DNSName": *"\([^"]*\)".*/\1/p' | head -1 | sed 's/\.$//')
    [ -n "$dns" ] && note "this node: https://$dns/"
  else
    bad "tailscaled is not running or not logged in (tailscale up)"
  fi
  if tailscale funnel status 2>/dev/null | grep -qi "funnel on"; then
    bad "FUNNEL IS ENABLED — C1 forbids any public exposure (tailscale funnel reset)"
  else ok "Funnel is off"; fi
  if tailscale serve status 2>/dev/null | grep -q .; then
    warn "tailscale serve already has configuration; review it before adding ours"
    tailscale serve status 2>/dev/null | sed 's/^/    /'
  else ok "no existing serve configuration to conflict with"; fi
  note "HTTPS certificates must be enabled in the tailnet admin console —"
  note "this script cannot check that; Serve cannot get a cert without it."
else
  bad "tailscale is not installed"
fi

head_ "OpenClaw — is the gate armed? (§P2-B)"

if command -v openclaw >/dev/null 2>&1; then
  ok "openclaw $(openclaw --version 2>/dev/null | head -1)"
  policy=$(openclaw exec-policy get --json 2>/dev/null)
  if [ -n "$policy" ]; then
    if printf '%s' "$policy" | grep -q '"askFallback"[[:space:]]*:[[:space:]]*"deny"' &&
       ! printf '%s' "$policy" | grep -qE '"askFallback"[[:space:]]*:[[:space:]]*"(full|allowlist)"'; then
      ok "askFallback = deny at every scope"
    else
      bad "askFallback is NOT deny everywhere — a timed-out approval would EXECUTE"
      note "fix: openclaw exec-policy set --ask-fallback deny"
      printf '%s' "$policy" | grep -o '"askFallback"[^,}]*' | sed 's/^/    /'
    fi
  else
    warn "could not read exec policy — check by hand: openclaw exec-policy get --json"
  fi
  for u in $(pgrep -f "openclaw" 2>/dev/null | head -3); do
    owner=$(ps -o user= -p "$u" 2>/dev/null | tr -d ' ')
    if [ "$owner" = "root" ]; then
      bad "an openclaw process runs as root (pid $u) — it could forge operator identity (§P2-G)"
    elif [ -n "$owner" ]; then ok "openclaw runs as '$owner', not root"; fi
  done
else
  warn "openclaw not on PATH — skip if this runtime is not in use"
fi

head_ "Hermes — schema and mode (§P2-D)"

HERMES_HOME="${MC_HERMES_HOME:-$HOME/.hermes}"
if [ -d "$HERMES_HOME" ]; then
  ok "hermes home at $HERMES_HOME"
  if command -v sqlite3 >/dev/null 2>&1 && [ -f "$HERMES_HOME/state.db" ]; then
    sv=$(sqlite3 "file:$HERMES_HOME/state.db?mode=ro" "SELECT version FROM schema_version LIMIT 1" 2>/dev/null)
    if [ "$sv" = "26" ]; then ok "state.db schema_version = 26 (what the adapter was built against)"
    elif [ -n "$sv" ]; then
      bad "state.db schema_version = $sv, adapter expects 26 — it will refuse to ingest"
      note "review upstream changes, then set MC_HERMES_EXPECTED_SCHEMA_VERSION deliberately"
    else warn "could not read schema_version from state.db"; fi
  else
    warn "sqlite3 not installed or state.db missing — cannot check the schema version"
  fi
  for f in cron/jobs.json cron/executions.db; do
    [ -e "$HERMES_HOME/$f" ] && ok "found $f" || warn "missing $f (fine if cron is unused)"
  done
  cfg="$HERMES_HOME/cli-config.yaml"
  if [ -f "$cfg" ]; then
    if grep -qE '^\s*mode:\s*manual' "$cfg"; then ok "approvals.mode: manual"
    elif grep -qE '^\s*mode:\s*(smart|off)' "$cfg"; then
      bad "approvals.mode is not manual — an LLM would approve before Mission Control sees it"
    else warn "approvals.mode not set in cli-config.yaml; it must be 'manual'"; fi
  fi
  for u in $(pgrep -f "hermes" 2>/dev/null | head -3); do
    owner=$(ps -o user= -p "$u" 2>/dev/null | tr -d ' ')
    if [ "$owner" = "root" ]; then
      bad "a hermes process runs as root (pid $u) — it could forge operator identity (§P2-G)"
    elif [ -n "$owner" ]; then ok "hermes runs as '$owner', not root"; fi
  done
else
  warn "no hermes home at $HERMES_HOME — set MC_HERMES_HOME if it lives elsewhere"
fi

head_ "Summary"
printf '  %d passed, %d warnings, %d blockers\n\n' "$PASS" "$WARN" "$FAIL"
if [ "$FAIL" -gt 0 ]; then
  printf '  \033[31mFix the blockers above before installing.\033[0m\n'
  printf '  A blocker in the OpenClaw or Hermes sections means the approval gate\n'
  printf '  would not actually gate anything.\n\n'
  exit 1
fi
printf '  \033[32mClear to install:\033[0m sudo ./scripts/install.sh\n\n'
