# Decisions, verification findings, and deviations

Per spec §0/§19: version-specific details were verified against current
documentation and source before building; discrepancies are reported here,
not silently adapted around. Verified 2026-08-18.

## A. Verification findings

### A1. Tailscale Serve identity model — CONFIRMED, spec §11 is accurate

- Identity headers on proxied tailnet requests: `Tailscale-User-Login`,
  `Tailscale-User-Name`, `Tailscale-User-Profile-Pic`. Serve **strips**
  inbound copies of these (plus `Tailscale-App-Capabilities`) to prevent
  spoofing. Funnel traffic gets no identity headers; tagged devices get
  none either. (tailscale.com/docs/features/tailscale-serve)
- Serve sets `X-Forwarded-For`, `X-Forwarded-Proto`, `X-Forwarded-Host` on
  proxied requests; backends are expected to verify the forwarded address
  via `tailscale whois` — exactly the §11.3 layer-3 design.
- CLI verified: `tailscale serve --bg --https=443 <target>`,
  `tailscale serve status [--json]`, `tailscale serve reset`;
  `tailscale whois --json <ip[:port]>` returns `UserProfile.LoginName` /
  `DisplayName` and `Node.Tags`.
- HTTPS certs must be enabled in the tailnet admin console before Serve
  can terminate TLS — called out in §11.1, confirmed.

### A2. OpenClaw (runtime A) — CONFIRMED with useful detail

Current stable **2026.7.1-2** (CalVer; npm dist-tag `latest`); docs at
docs.openclaw.ai; repo github.com/openclaw/openclaw.

- Gateway multiplexes WS + HTTP on one port, default **18789**, default
  bind loopback. WS protocol is custom JSON frames (`req`/`res`/`event`),
  not JSON-RPC. §2's description is accurate.
- Observation surface for the Phase 1 adapter: WS events (`chat`,
  `session.message`, `session.tool`, `session.operation`,
  `sessions.changed`, `cron`, `presence`, `exec.approval.*`) and RPCs
  (`sessions.list/get/subscribe`, `chat.history`, `audit.activity.list`
  with cursor pagination and 30-day retention, `tasks.list`). Backfill per
  §6.1 can use `audit.activity.list` + `chat.history`.
- §11.4 reference implementation confirmed in source
  (`src/gateway/ingress-attribution.ts`, `src/gateway/auth.ts`,
  `src/infra/tailscale.ts`): requires loopback peer + all three
  `x-forwarded-*` headers, resolves the client IP from XFF trusting only
  loopback hops, rejects loopback-valued client IPs, then compares
  `tailscale whois --json` login case-insensitively against
  `tailscale-user-login`; fails closed on any error; 60s positive/5s
  negative whois cache. Our middleware mirrors this, including the
  loopback-XFF rejection and treating `tailscale-funnel-request` as a
  distinguishing marker (we reject it outright — C1).
- Their documented caveat adopted here too: tokenless header auth assumes
  a trusted host; if untrusted local code may run on the VPS, switch to
  token auth (kept in mind for Phase 2's service tokens).

### A3. Hermes Agent (runtime B) — CONFIRMED, with one real §6 discrepancy

Current version **0.20.4** (github.com/NousResearch/hermes-agent; docs at
hermes-agent.nousresearch.com/docs). Persistent daemon, systemd-friendly
(`StandardOutput=journal` in its generated unit), 7 execution backends,
20+ messaging platforms. SQLite is WAL-mode by default.

**Discrepancy vs §6.2 — cron definitions are NOT in SQLite.**
- `~/.hermes/state.db` — sessions/messages (schema v26): `sessions`,
  `messages` (incl. `tool_calls`/`tool_name` columns — tool calls ARE in
  SQLite), `session_model_usage` (token/cost accounting per §5.3's
  `token_input`/`token_output`/`cost_usd`), FTS mirrors.
- Cron **job definitions** live in `~/.hermes/cron/jobs.json` (flat JSON,
  advisory-file-locked; `schedule.kind` ∈ `once`|`interval`|`cron`,
  croniter expressions, `next_run_at`, `last_status`, `failure_streak`).
- Cron **execution ledger** is a separate SQLite DB,
  `~/.hermes/cron/executions.db` (`executions` table: status ∈
  claimed/running/completed/failed/unknown; pruned to last 1000 terminal
  rows).

**Impact (Phase 1, no Phase 0 schema change needed):** the Hermes adapter
must watch three sources (state.db read-only, jobs.json, executions.db
read-only), not one. Our `scheduled_tasks` model already fits: `cron_expr`
is nullable (interval/one-shot jobs), `next_fire_at` carries truth,
`consecutive_failures` maps from `failure_streak`. The §6.2 requirement to
"read cron task definitions" simply points at jobs.json instead of a
SQLite table.

**Phase 2 note (good news):** Hermes has a first-class external-approval
slot — `security.approval.transport` config with `transport_fallback: deny`
(fail closed), and a synchronous `set_approval_callback()` whose return
value directly gates execution, deny on exception. A cleaner integration
point than wrapping individual tools. OpenClaw likewise: `before_tool_call`
plugin hook with `requireApproval` (fails closed, but its approval timeout
caps at 10 min — **below our §7.2 15-min TTL [DECIDED]**; either the
gateway-RPC route (`exec.approval.requested`/`exec.approval.resolve`
operator client) or a shorter effective TTL for OpenClaw must be chosen in
Phase 2 — flagged now so it isn't discovered mid-build).

### A4. What could NOT be verified from this environment

This work was done off-VPS; "currently installed versions" (§0) could not
be inspected. Before Phase 1, run on the VPS and compare against A2/A3:

```bash
openclaw --version          # expect 2026.7.x line
hermes --version            # expect 0.20.x
ls ~/.hermes/state.db ~/.hermes/cron/jobs.json ~/.hermes/cron/executions.db
sqlite3 ~/.hermes/state.db "PRAGMA user_version; SELECT version FROM schema_version;"
tailscale version
```

If the installed versions differ materially (especially the Hermes schema
version or the OpenClaw gateway protocol), stop and reconcile before
writing adapter code.

## B. Deviations from the spec text (each with its reason)

- **D1 — API runs natively under systemd; Docker Compose runs Postgres
  only.** §16 Phase 0 says "Docker Compose (Postgres + API)"; §19 (the
  operative kickoff) says "Docker Compose running PostgreSQL 16" +
  "systemd units for API and database". Followed §19. Native execution
  also lets the API reach the tailscaled socket for whois without
  host-networking games.
- **D2 — `/health` is identity-exempt** (liveness only, discloses name +
  version + time). Everything else, `/` included, requires verified
  identity.
- **D3 — no interactive API docs** (`/docs`, `/redoc`, `/openapi.json`
  disabled): smaller surface; revisit on request.
- **D4 — `events` is not declaratively partitioned in the baseline.**
  §5.4 says "partition or time-bucket". A PG partitioned table cannot
  carry the global unique `dedupe_key` (unique constraints must include
  the partition key), and dedupe correctness outranks partition hygiene.
  Shipped instead: BRIN index on `ts`, append-only usage, btree
  `(agent_id, seq)`, and the §18.4 retention roll-up will do time-bucket
  deletion. Revisit if event volume outgrows this before retention lands.
- **D5 — `agents.status` is free text** (`'unknown'` default): §5.2 does
  not enumerate agent states; the real lifecycle emerges from the Phase 1
  adapters. An enum now would be a guess baked into the schema.
- **D6 — `approvals.external_run_id` added** (nullable text, beyond §5.6):
  the agent-side wrapper knows its runtime-native run id, not Mission
  Control's `runs.id` uuid; the normalizer back-fills `run_id`. Without
  this column the §7.1 protocol has nowhere to put what the wrapper sends.
- **D7 — `scheduled_tasks.cron_expr` nullable**: Hermes schedules include
  `once` and `interval` kinds (A3); forcing a cron string would mean
  fabricating data. `next_fire_at` is the operative field.
- **D8 — uvicorn runs with `proxy_headers=False`** (explicitly): the
  middleware parses `X-Forwarded-For` itself; uvicorn must not rewrite the
  peer address from headers, or layer 1 would trust attacker input.

## P1. Phase 1 decisions (observation)

- **P1-D1 — Adapters emit; the normalizer assigns `events.seq`.** §6.3's
  contract includes `seq`; adapters pass the runtime's ordering hint
  (`source_seq`: audit-ledger sequence, SQLite rowid) but the stored,
  SSE-resume-bearing `events.seq` is assigned per agent by the normalizer.
  Runtime sequences reset across reconnects and interleave across source
  streams (Hermes has three), so a single writer must own the cursor space.
- **P1-D2 — OpenClaw observation is metadata-only in Phase 1.** Ingestion
  rides `audit.activity.list` (stable eventIds, monotonic sequence, 30-day
  retention): runs, tool actions, message dispatch metadata — but the
  ledger deliberately stores no prompts/tool args/bodies, and the push-side
  `chat` delta stream has no stable per-frame identity to dedupe on.
  Message *content* for OpenClaw comes later via `chat.history` on the run
  detail view if wanted; the approval path (Phase 2) carries its own
  complete `tool_args` regardless (S7). Hermes observation includes bounded
  message/tool content from state.db.
- **P1-D3 — event payload text is clipped at MC_EVENT_PAYLOAD_TEXT_LIMIT
  (16 KB default) with an explicit `truncated: true` + full-content sha256.**
  Observability copies, not records of authority; S7's no-truncation rule
  binds `approvals.tool_args` (Phase 2), which will be stored complete.
- **P1-D4 — migration 0002 adds `adapter_cursors`** (agent_id, source →
  jsonb cursor): §6.2's per-table high-water marks and §6.1's reconnect
  backfill need durable cursors that survive process restarts; deriving
  them from the events table would couple correctness to payload parsing.
- **P1-D5 — vanished Hermes cron jobs are disabled, not deleted** — history
  and future objective links survive; Phase 4 owns richer agenda semantics.
- **P1-D6 — subagent linkage** (§6.1): OpenClaw child runs link to the most
  recent run in the spawning session (`sessions.list` `spawnedBy`/
  `parentSessionKey` lineage + last-run tracking); Hermes uses
  `parent_session_id` directly. The OpenClaw mapping is best-effort and
  marked for refinement against real gateway traffic (tasks.list
  `parentTaskId` is the upgrade path if needed).
- **P1-D7 — agent liveness**: OpenClaw = WS connected + `tick`/`heartbeat`
  (throttled to one status write per 10s); Hermes = freshest mtime of
  cron/ticker_heartbeat, state.db, state.db-wal, threshold 180s. Status
  *transitions* are recorded as lifecycle events; steady heartbeats only
  update `agents.last_heartbeat_at`.
- **P1-D8 — SSE ids are the global `events.id`** (per-stream resume via
  `Last-Event-ID`); per-agent `events.seq` remains the per-agent cursor
  §13 names. A global stream cannot resume on a per-agent counter, and
  `events.id` is monotonic per the single-writer ingest path.
- **P1-D9 — the Hermes state path is bound read-only into the API's mount
  namespace** (`BindReadOnlyPaths`), so §6.2's MUST-NOT-write holds at the
  OS level too, not just by code discipline.

## C. §18 open questions — status

Untouched by Phase 0, still owed answers by the operator: fallback
notification channel (1), default gated-tool list (2), actual objectives
(3), retention windows (4 — interacts with D4), concurrency scale (5),
pause/resume write-back (6). On (7) Postgres-vs-SQLite: Postgres 16 is
implemented per spec; nothing in Phase 0 precludes revisiting, but the
role-split security model (S4) leans on real DB roles, which SQLite lacks.
