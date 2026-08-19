#!/usr/bin/env bash
# Phase 0 local verification — everything checkable without a tailnet.
# (The tailnet-side checks — serve status, phone loads the hello page,
# off-tailnet failure — are in docs/phase-0-runbook.md §Verification.)
#
# Requires: .env at repo root (copy .env.example), docker, python3.12 venv
# via `make setup`.
set -euo pipefail
cd "$(dirname "$0")/.."

PORT="$(grep -E '^MC_BIND_PORT=' .env | cut -d= -f2 || true)"
PORT="${PORT:-8100}"
PASS=0; FAIL=0
check() { # check <description> <expected> <actual>
    if [ "$2" = "$3" ]; then PASS=$((PASS+1)); echo "  ok: $1"
    else FAIL=$((FAIL+1)); echo "  FAIL: $1 (expected $2, got $3)"; fi
}

echo "[1/5] unit tests (§17 tests 6 & 7 included)"
(cd api && .venv/bin/pytest -q)

echo "[2/5] postgres 16 + role split"
docker compose --env-file .env up -d --wait postgres >/dev/null

echo "[3/5] alembic upgrade head"
(cd api && set -a && . ../.env && set +a && .venv/bin/alembic upgrade head >/dev/null 2>&1 && .venv/bin/alembic current)

echo "[4/5] audit_log is append-only for mc_app"
set -a; . ./.env; set +a
export PGPASSWORD="$MC_DB_APP_PASSWORD"
psql -h 127.0.0.1 -U mc_app -d mission_control -qc \
  "INSERT INTO audit_log (actor, actor_source, action, entity_type) VALUES ('system','system','phase0.verify','none')" \
  && echo "  ok: INSERT allowed"
if psql -h 127.0.0.1 -U mc_app -d mission_control -qc "DELETE FROM audit_log" 2>/dev/null; then
    echo "  FAIL: DELETE was allowed"; FAIL=$((FAIL+1))
else
    echo "  ok: DELETE denied"; PASS=$((PASS+1))
fi

echo "[5/5] live API on 127.0.0.1:${PORT}"
(cd api && set -a && . ../.env && set +a && .venv/bin/python -m mission_control >/dev/null 2>&1 &)
sleep 2.5
check "/health answers"                      "200" "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${PORT}/health")"
check "no identity -> 401"                   "401" "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${PORT}/")"
check "spoofed identity header -> 403 (§17 #6)" "403" "$(curl -s -o /dev/null -w '%{http_code}' -H 'Tailscale-User-Login: x@y.z' "http://127.0.0.1:${PORT}/")"
pkill -f "python -m mission_control" 2>/dev/null || true

echo
echo "verify-phase0: ${PASS} extra checks passed, ${FAIL} failed"
exit $((FAIL > 0 ? 1 : 0))
