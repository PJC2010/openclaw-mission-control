#!/usr/bin/env bash
# Phase 2 end-to-end verification over real HTTP.
#
# Complements the pytest suite, which drives the app in-process through
# ASGITransport: this runs the actual uvicorn server, real sockets, the
# real service-token CLI, and the real whois resolver — only the tailscale
# DAEMON is stubbed (scripts/testing/fake-tailscale), never our own code.
#
# Requires: a running Postgres (make db-up) and a repo-root .env.
#   ./scripts/verify-phase2.sh
set -e
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT/api"
set -a; source ../.env; set +a
export MC_ADAPTERS_ENABLED=false MC_WORKSPACE_ALLOWLIST=/workspace MC_BIND_PORT=8131
# Fake the tailscale DAEMON, not our resolver: the real CliWhoisResolver
# still execs, parses and validates. No production code is relaxed.
export MC_TAILSCALE_BIN="$REPO_ROOT/scripts/testing/fake-tailscale"
SCRATCH="$(mktemp -d)"

PGPASSWORD=devmigrate123 psql -h 127.0.0.1 -U mc_migrate -d mission_control -qc \
  "TRUNCATE notifications, audit_log, outcomes, approval_policies, approvals, adapter_cursors, scheduled_tasks, events, runs, service_tokens, system_flags, agents, objectives RESTART IDENTITY CASCADE" >/dev/null

.venv/bin/python -m mission_control > "$SCRATCH/smoke-api.log" 2>&1 &
API_PID=$!
trap 'kill $API_PID 2>/dev/null || true' EXIT
sleep 3

AGENT_ID=$(.venv/bin/python - <<'PY'
import asyncio
from mission_control.config import Settings
from mission_control.db import build_async_engine, build_async_session_factory
from mission_control.normalizer import Normalizer
from mission_control.models.enums import AgentRuntime
async def main():
    s = Settings(); e = build_async_engine(s); f = build_async_session_factory(e)
    print(await Normalizer(f).register_agent(AgentRuntime.HERMES, "hermes:smoke", "Hermes Smoke"))
    await e.dispose()
asyncio.run(main())
PY
)
TOKEN=$(.venv/bin/python -m mission_control.tokens_cli create --name smoke --agent "$AGENT_ID" --created-by smoke 2>&1 | grep -oE "mc_[A-Za-z0-9_-]+" | head -1)
B="http://127.0.0.1:8131"
OP=(-H "Tailscale-User-Login: castillop92@gmail.com" -H "X-Forwarded-For: 100.101.102.103" -H "X-Forwarded-Proto: https" -H "X-Forwarded-Host: vps.ts.net")
J=(-H "Content-Type: application/json")
py() { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"; }

echo "token minted: ${TOKEN:0:14}..."
echo
echo "1. agent requests a credential read (should classify critical)"
CREATE=$(curl -s -X POST "$B/v1/approvals" -H "Authorization: Bearer $TOKEN" "${J[@]}" \
  -d '{"idempotency_key":"smoke-key-0001","tool_name":"bash","tool_args":{"command":"cat /home/pete/.ssh/id_ed25519"},"rationale":"needs the key"}')
echo "$CREATE" | py "'   state=%s risk=%s' % (d['state'], d['risk_level'])"
AID=$(echo "$CREATE" | py "d['approval_id']")

echo "2. agent tries to approve its own request (§17 test 5)"
curl -s -o /dev/null -w "   HTTP %{http_code}  (want 403)\n" -X POST "$B/v1/approvals/$AID/decision" \
  -H "Authorization: Bearer $TOKEN" "${J[@]}" -d '{"approve":true}'

echo "3. operator's queue"
curl -s "${OP[@]}" "$B/v1/approvals?state=pending" | py "'   %s  risk=%s  why=%s' % (d['approvals'][0]['tool_name'], d['approvals'][0]['risk_level'], d['approvals'][0]['risk_categories']['reasons'][0])"

echo "4. operator approves"
curl -s "${OP[@]}" -X POST "$B/v1/approvals/$AID/decision" "${J[@]}" -d '{"approve":true,"note":"ok"}' \
  | py "'   state=%s by=%s via=%s' % (d['state'], d['decided_by'], d['decided_via'])"

echo "5. agent claims it (first poll)"
curl -s "$B/v1/approvals/$AID/decision" -H "Authorization: Bearer $TOKEN" \
  | py "'   allowed=%s consumed=%s digest=%s…' % (d['allowed'], d['already_consumed'], d['args_digest'][:12])"

echo "6. agent replays the poll (§7.6: one approval, one execution)"
curl -s "$B/v1/approvals/$AID/decision" -H "Authorization: Bearer $TOKEN" \
  | py "'   allowed=%s consumed=%s' % (d['allowed'], d['already_consumed'])"

echo "7. payload swap on the same idempotency key (§17 test 8)"
curl -s -o /dev/null -w "   HTTP %{http_code}  (want 409)\n" -X POST "$B/v1/approvals" -H "Authorization: Bearer $TOKEN" "${J[@]}" \
  -d '{"idempotency_key":"smoke-key-0001","tool_name":"bash","tool_args":{"command":"rm -rf /"}}'

echo "8. kill switch (§17 test 12)"
curl -s "${OP[@]}" -X POST "$B/v1/system/kill-switch" "${J[@]}" -d '{"engaged":true}' \
  | py "'   engaged=%s pending_denied=%s' % (d['engaged'], d['pending_denied'])"
curl -s -X POST "$B/v1/approvals" -H "Authorization: Bearer $TOKEN" "${J[@]}" \
  -d '{"idempotency_key":"smoke-key-0002","tool_name":"read_file","tool_args":{"path":"/workspace/a.txt"}}' \
  | py "'   new request while paused: state=%s' % d['state']"

echo "9. audit trail (mc_app can INSERT but never UPDATE/DELETE)"
PGPASSWORD=devapp123 psql -h 127.0.0.1 -U mc_app -d mission_control -tAc \
  "SELECT '   ' || action || '  <- ' || actor FROM audit_log ORDER BY id"
