# Mission Control — Requirements Specification

**Project:** Mission Control — unified operations dashboard for self-hosted AI agents
**Operator:** Pete (single user, single tenant)
**Date:** 2026-08-18
**Audience:** A coding agent implementing this from scratch

---

## 0. How to read this document

- **MUST / MUST NOT** — hard requirement. Do not negotiate these away for convenience.
- **SHOULD** — strong default. Deviate only with a stated reason.
- **MAY** — optional.
- `[DECIDED]` — the operator has made this call. Implement as written.
- `[OPEN]` — unresolved. Do not guess silently; surface it (see §18).

**Verify before you build.** OpenClaw and Hermes Agent are both fast-moving 2026 projects. Every version-specific detail in §2 and §6 — config keys, header names, DB paths, event shapes — MUST be checked against the currently installed versions and their live documentation before you write code against them. Treat this document as intent, not as an API reference.

---

## 1. Purpose and success criteria

A single dashboard that lets one operator **observe, approve, and steer** two self-hosted agent runtimes on one VPS, from any of his devices, with the phone as the primary surface.

The system exists to answer three questions and support one action:

1. What are my agents doing right now, and what did they do overnight?
2. Is that work serving a goal I actually care about — or is it drift?
3. What is waiting on my decision?
4. **Action:** approve or deny a gated agent action, from a phone, in under 30 seconds.

### Success criteria (measurable)

| # | Criterion |
|---|---|
| SC1 | An approval request created by an agent appears on the operator's phone and can be decided in under 30s end to end |
| SC2 | Every agent run in the last 30 days is attributable to an objective, or explicitly flagged as untagged |
| SC3 | All scheduled work across both runtimes is visible in one list with next fire time and last outcome |
| SC4 | No inbound port is open on the VPS to the public internet |
| SC5 | An agent cannot approve, cancel, or alter the outcome of its own approval request |

### Explicit non-goals

Not a general-purpose observability platform. Not multi-tenant. Not a replacement for either runtime's own UI. Not a place to author or edit agent skills.

---

## 2. Environment and existing components

**Host:** one Linux VPS. All components in this spec run on it.

**Runtime A — OpenClaw.** Self-hosted personal agent framework. A central Gateway process multiplexes WebSocket and HTTP on a single port and manages session lifecycle, tool dispatch, channel routing, and agent orchestration. Extensible via skills; can spawn subagents; connects to messaging channels. Notably, it already ships a Tailscale Serve integration for its own gateway dashboard (see §11.4) — read that code path before writing your own auth.

**Runtime B — Hermes Agent** (Nous Research). A persistent daemon that keeps memory, skills, and conversation history in a local SQLite database, runs scheduled cron tasks, connects to messaging platforms, and supports several execution backends (local, Docker, SSH, and others).

**Network:** Tailscale is already installed and in active use. `[DECIDED]`

**Operator context.** Pete runs three parallel tracks, and agent work belongs to one of them: his day job (VillageMD, healthcare data), his agency (Castillo & Co.), and his SaaS product (Axon CRM). The objectives model in §8 MUST support this three-track split as a first-class field, because "which venture did this serve" is the primary lens the operator will use.

---

## 3. Hard constraints

| ID | Constraint |
|---|---|
| C1 | **No public internet exposure.** Access is over Tailscale only. Tailscale Funnel MUST NOT be enabled for any component. |
| C2 | The API MUST bind to `127.0.0.1` only. Never `0.0.0.0`. This is load-bearing for the auth model (§11). |
| C3 | The phone is the **primary** design target, not a responsive afterthought. |
| C4 | Agents MUST NOT be able to decide their own approval requests, directly or transitively. |
| C5 | All data stays on the VPS. No third-party analytics, error reporting, or telemetry. |
| C6 | Integrate through supported extension points (plugins, hooks, wrappers, read-only DB access). MUST NOT fork or patch either runtime's core in a way that blocks upgrades. |
| C7 | If Mission Control is down or unreachable, gated agent actions MUST fail closed (deny), never fail open. |

---

## 4. Architecture

```
┌─────────────────────── VPS ───────────────────────┐
│                                                   │
│  OpenClaw Gateway ──┐                             │
│   (WS + HTTP)       │                             │
│                     ├──► Adapters ──► Normalizer  │
│  Hermes daemon ─────┘                     │       │
│   (SQLite + cron)                         ▼       │
│                                      PostgreSQL   │
│                                           ▲       │
│  Agent tool wrapper ──► Approval Gateway ─┤       │
│      (blocks on decision)                 │       │
│                                           ▼       │
│                              FastAPI (127.0.0.1)  │
│                                  │  REST + SSE    │
│                                  ▼                │
│                          Next.js PWA (static)     │
│                                  ▲                │
│                          tailscale serve (TLS)    │
└──────────────────────────────────┼────────────────┘
                                   │ tailnet only
                        phone / laptop / tablet
```

**Component responsibilities**

- **Adapters** (one per runtime) — pull raw state and events from a runtime, emit normalized events. Isolated so a third runtime can be added without touching the core.
- **Normalizer** — maps runtime-specific events into the common schema (§6.3), deduplicates, persists.
- **Approval Gateway** — the blocking endpoint agents call before executing a gated action. Separate module with its own auth path and its own tests. This is the security boundary of the entire system.
- **API** — REST for state and mutations, SSE for live streams.
- **Web app** — Next.js PWA, served as static output from the same origin. No CORS, one deploy target.
- **Notifier** — web push plus fallback channel.

---

## 5. Data model

PostgreSQL. Use SQLAlchemy 2.x with Alembic migrations from commit one — no ad-hoc schema changes.

### 5.1 `objectives`
| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `title` | text | short, human |
| `track` | enum | `villagemd` \| `castillo` \| `axon` \| `personal` |
| `description` | text | what "done" looks like |
| `horizon_start` / `horizon_end` | date | typically a quarter |
| `status` | enum | `active` \| `paused` \| `archived` |
| `priority` | int | 1–5 |
| `created_at` / `updated_at` | timestamptz | |

Soft-constraint: warn in the UI above 5 `active` objectives. Do not enforce in the DB.

### 5.2 `agents`
Registered runtime instances. `id`, `runtime` (`openclaw` \| `hermes`), `display_name`, `instance_key`, `status`, `last_heartbeat_at`, `config_snapshot` (jsonb).

### 5.3 `runs`
One agent session or task execution. `id`, `agent_id` FK, `external_id` (runtime's own id, unique per agent), `objective_id` FK nullable, `objective_source` (`inferred` \| `manual` \| `none`), `trigger` (`schedule` \| `message` \| `manual` \| `subagent`), `parent_run_id` self-FK (subagents), `started_at`, `ended_at`, `status` (`running` \| `succeeded` \| `failed` \| `cancelled`), `error_summary`, `token_input`, `token_output`, `cost_usd` numeric.

Index on `(agent_id, started_at desc)` and `(objective_id, started_at desc)`.

### 5.4 `events`
Append-only event stream. `id` bigserial, `run_id` FK nullable, `agent_id` FK, `seq` (monotonic per agent, for SSE resume), `ts`, `kind` (`tool_call` \| `tool_result` \| `message` \| `log` \| `error` \| `lifecycle`), `payload` jsonb, `dedupe_key` unique nullable.

Partition or time-bucket this table; it will be the largest by an order of magnitude.

### 5.5 `scheduled_tasks`
Unified agenda across runtimes. `id`, `agent_id` FK, `external_id`, `name`, `cron_expr`, `timezone`, `next_fire_at`, `last_run_id` FK, `last_outcome`, `consecutive_failures` int, `enabled` bool, `objective_id` FK nullable, `staleness_flag` bool, `writeback_supported` bool.

### 5.6 `approvals`
The critical table. See §7.
`id` uuid, `agent_id` FK, `run_id` FK, `idempotency_key` unique, `tool_name`, `tool_args` jsonb (**complete, never truncated**), `args_digest` (sha256, for detecting mutation), `risk_level` (`low` \| `medium` \| `high` \| `critical`), `objective_id` FK nullable, `rationale` text (agent's stated reason), `state` (`pending` \| `approved` \| `denied` \| `expired` \| `cancelled`), `created_at`, `expires_at`, `decided_at`, `decided_by` text (tailnet login), `decided_via` (`dashboard` \| `auto_policy` \| `timeout`), `decision_note` text.

### 5.7 `approval_policies`
Auto-approval rules. `id`, `objective_id` FK nullable, `agent_id` FK nullable, `tool_name_pattern`, `arg_matchers` jsonb, `action` (`auto_approve` \| `always_ask`), `max_risk_level`, `enabled`, `created_by`, `created_at`.

**Allowlist semantics only.** A request auto-approves *only* on an explicit matching `auto_approve` rule. Absence of a rule means ask.

### 5.8 `outcomes`
Operator's verdict on completed runs. `run_id` FK PK, `verdict` (`useful` \| `not_useful` \| `harmful`), `note`, `rated_at`, `rated_by`.

### 5.9 `audit_log`
Append-only, insert-only at the DB-role level. `id`, `ts`, `actor` (tailnet login or `system`), `actor_source` (`identity_header` \| `service_token` \| `system`), `action`, `entity_type`, `entity_id`, `before` jsonb, `after` jsonb, `request_ip`.

Every approval decision, policy change, objective change, and agent pause MUST write here. No UPDATE or DELETE grant on this table for the application role.

### 5.10 `notifications`
`id`, `approval_id` FK nullable, `channel`, `sent_at`, `delivered_at`, `failed_reason`, `payload_digest`.

---

## 6. Adapter requirements

### 6.1 OpenClaw adapter
- Subscribe to the Gateway's WebSocket for live session, tool, and lifecycle events. Do not scrape stdout when a structured channel exists.
- Reconnect with exponential backoff and jitter; cap at 60s.
- On reconnect, backfill missed events via the HTTP API rather than accepting a gap. Log the gap window if backfill is impossible.
- Deduplicate on the runtime's own event id via `events.dedupe_key`.
- Map subagent spawns to `runs.parent_run_id`.

### 6.2 Hermes adapter
- Open the Hermes SQLite database **read-only** (`file:...?mode=ro`, `immutable=0`). MUST NOT write, migrate, or lock it. Respect WAL mode; never run `VACUUM` or take exclusive locks.
- Poll on an interval (default 5s, configurable). Track a high-water mark per table so each poll is incremental.
- Read the cron task definitions into `scheduled_tasks`.
- Tail the daemon's logs (journald if run under systemd) for events not represented in SQLite.
- If the SQLite schema changes under you, fail loudly with a clear error — MUST NOT silently ingest garbage.

### 6.3 Normalized event contract
Every adapter emits:
```
{ agent_id, external_run_id, seq, ts, kind, payload, dedupe_key }
```
Adapters MUST NOT write directly to `runs` or `approvals`. They emit events; the normalizer owns persistence. This keeps a buggy adapter from corrupting the approval path.

### 6.4 Extensibility
Define an `AgentAdapter` protocol (`start()`, `stop()`, `backfill(since)`, `health()`). Adding a third runtime MUST require only a new adapter class and a row in `agents`.

---

## 7. Approvals subsystem

This is the highest-risk component. Build it second (right after read-only observation), test it hardest, and do not let feature work bleed into it.

### 7.1 Protocol

1. A gated tool call is intercepted by a wrapper on the agent side (§7.6).
2. Wrapper `POST`s to `/v1/approvals` with `idempotency_key`, `tool_name`, complete `tool_args`, `rationale`, `run_id`.
3. Server evaluates `approval_policies`. On an explicit `auto_approve` match, it returns `approved` immediately with `decided_via=auto_policy` and still writes an audit row.
4. Otherwise the row is `pending`, a notification fires, and the wrapper **blocks** — long-poll on `GET /v1/approvals/{id}/decision?wait=30` in a loop, or SSE.
5. Operator decides in the dashboard. Wrapper unblocks and either executes or raises.
6. On `expires_at` with no decision, state becomes `expired`, which the wrapper MUST treat as **deny**.

### 7.2 Timeouts
Default TTL **15 minutes** `[DECIDED — generous, because notification delivery over a private tailnet is best-effort]`. Configurable per policy. `expired` MUST be recorded and reported distinctly from `denied` — the operator needs to tell "I said no" apart from "I never saw it." Surface an expiry rate metric; a rising rate means the notification path is broken.

### 7.3 Idempotency
`idempotency_key` is unique. A retry with the same key returns the existing row and its current state. A retry with the same key but **different args** MUST be rejected with 409 and logged as a security event — that is either a bug or an attempt to swap the payload after approval. `args_digest` exists for exactly this check.

### 7.4 Risk classification
Classify on the server, not from an agent-supplied field (the agent may be compromised — an agent-declared `risk_level` is a hint at best, stored separately as `claimed_risk`).

`critical` — and therefore **never auto-approvable regardless of policy or objective**:
- anything reading or writing credentials, tokens, key files, `.env`
- payments or financial APIs
- outbound email, SMS, or posting to external accounts
- destructive filesystem operations, or writes outside a configured workspace allowlist
- package installation, or modification of the agent's own skills, config, or memory
- `sudo`, systemd unit changes, firewall or Tailscale config changes

### 7.5 Auth split
- **Agents** authenticate with a service token scoped to `approvals:create` and `approvals:poll`. This token MUST NOT grant `approvals:decide`, and MUST NOT grant write access to `objectives`, `approval_policies`, or `audit_log`. (C4)
- **The operator** authenticates via Tailscale identity headers (§11). Only an identity-header-authenticated request may decide.
- Two distinct middleware paths. Do not build one "is authenticated" check that both flow through.

### 7.6 Agent-side wrapper
For OpenClaw, implement as a plugin/hook at a supported extension point. For Hermes, wrap the gated tools. In both cases:
- The wrapper MUST fail closed on network error, timeout, non-2xx, or unparseable response.
- The wrapper MUST NOT cache approvals. One approval, one execution.
- Configuration lists which tools are gated; the default for an unknown tool is **gated**.

### 7.7 Kill switch
A single **Pause All Agents** control, reachable in two taps from the dashboard home. It sets a global flag that makes the approval gateway deny everything pending and new, and (where the runtime supports it) stops the daemons. Add an obvious visual state so the operator cannot forget it is on.

### 7.8 Flooding
Rate-limit approval creation per agent (default 20/hour, configurable). Beyond the limit, additional requests auto-deny and raise a distinct alert. Approval fatigue is a real attack: a compromised agent that generates fifty benign requests to slip one bad one past a tired operator is a known pattern.

---

## 8. Objectives and alignment layer

The spine of the product. Without it, "alignment" is decoration.

### 8.1 Tagging
- Runs are tagged to an objective by **inference first**: a rules table mapping (agent, skill/task name, channel, keyword) → objective. Manual tagging is a correction, not the primary path.
- If the operator has to tag manually, he will stop by week three. Design accordingly.
- `objective_source` records whether the tag was inferred or corrected, so inference rules can be scored later.

### 8.2 Untagged work report
The single most important view in the alignment layer. For a selected window: count of runs, total tokens, and total cost that map to **no** active objective, with a drill-down list. Show it on the dashboard home, not buried in a reports tab.

### 8.3 Spend by objective
Weekly roll-up of `cost_usd` and token counts grouped by objective and by track. Both runtimes bill the same providers; this answers whether the agent budget matches stated priorities.

### 8.4 Outcome capture
One-tap `useful` / `not useful` on completed runs, available from the run list and from the daily digest. Activity metrics lie; six weeks of verdicts tells the operator which scheduled tasks to kill.

### 8.5 Weekly review
A scheduled prompt (default Monday morning) that walks the operator through: confirm active objectives, archive dead ones, review untagged work, review tasks with `consecutive_failures > 0` or `staleness_flag`. Target: under 2 minutes. If it takes longer, it will be skipped and the objectives table will rot.

### 8.6 Drift indicators
Flag on the home screen when: untagged share exceeds a threshold (default 30%), an objective has had zero runs in 14 days, or a single objective consumes more than a configurable share of total spend.

---

## 9. Agenda / unified schedule

- One list of everything scheduled across both runtimes: name, runtime, cron expression in plain English, next fire time in the operator's timezone, last outcome, consecutive failure count, owning objective.
- **Staleness flag:** a task that keeps completing "successfully" but has produced no captured outcome or artifact in N consecutive runs (default 5). This catches the zombie cron job, which is the most common form of silent drift.
- Pause/resume per task. Where the runtime supports programmatic write-back, do it; where it does not, mark the task `writeback_supported=false` and show the exact manual command instead of pretending.
- Timeline view for the next 24 hours on the home screen.

---

## 10. Notifications

- **Primary:** Web Push (VAPID) to the installed PWA. Works on iOS when installed to the home screen.
- **Fallback:** ntfy, or the messaging channel the runtimes already connect to. `[OPEN — operator to pick, see §18]`
- **The alert is a wake-up, not the decision surface.** Deep-link into the dashboard; do not build approve/deny buttons into the notification payload, because that path does not carry tailnet identity.
- **Notification bodies MUST NOT contain full tool arguments.** They appear on a lock screen. Send tool name, risk level, and objective only.
- Quiet hours, configurable, with `critical` risk always overriding.
- Escalation: if a `critical` approval is still `pending` at 50% of its TTL, re-notify on all channels.

---

## 11. Access and authentication

### 11.1 Transport `[DECIDED]`
`tailscale serve` — **not** Funnel. Serve terminates TLS with an auto-provisioned cert on the tailnet MagicDNS name. HTTPS certs must be enabled in the tailnet admin console. The HTTPS origin is not optional: a PWA will not install or register a service worker without a secure context.

### 11.2 Identity
Tailscale Serve injects identity headers on proxied requests — `Tailscale-User-Login` (the login email) and `Tailscale-User-Name` (display name). Use `Tailscale-User-Login` as the operator identity. No password, no separate login page.

Two facts that MUST shape the implementation:
- Funnel does **not** populate these headers. Another reason for C1.
- Identity headers are populated for **users, not tagged devices**. Agent processes therefore cannot authenticate this way — they use service tokens (§7.5).

### 11.3 Header trust — three layers
1. **Bind to `127.0.0.1` only** (C2). Otherwise anyone who can reach the service directly instead of through the Serve URL can simply set the headers themselves.
2. **Tailnet ACL** restricting access to port 443 on this node, so nothing on the tailnet can bypass the proxy and hit the app.
3. **Verify the header** by resolving the request's forwarded source address via `tailscale whois` and confirming it matches the claimed login. Reject on mismatch.

Additionally: reject any request carrying identity headers that did **not** arrive with the forwarded headers Serve sets. Never trust a loopback peer by itself — a misconfigured local reverse proxy forwarding external traffic to loopback would otherwise let anyone spoof tailnet identity.

### 11.4 Reference implementation
OpenClaw already solves this exact problem for its own gateway dashboard: with Serve mode enabled it authenticates the Control UI and WebSocket from the identity header, verified by resolving the forwarded address through the local Tailscale daemon. Read that code path before writing your own. Note its caveat, which applies here too: tokenless header auth assumes the host is trusted, so if untrusted local code may run on the VPS, require token auth instead.

---

## 12. Security requirements

**Threat model.** Both runtimes execute with broad access to the filesystem, shell, and browser, and both ingest untrusted content from the web and from messaging channels. Prompt injection leading to attacker-influenced tool calls is a realistic, documented scenario — not a hypothetical. Mission Control's approval queue is the control that stands between an injected instruction and a destructive action. Build it like that is true, because it is.

| ID | Requirement |
|---|---|
| S1 | Agents MUST NOT hold any credential that can decide, cancel, or expire an approval. |
| S2 | Agents MUST NOT be able to create, modify, or delete objectives or approval policies. |
| S3 | Agent-supplied content (tool args, rationale, log lines) MUST be rendered as inert text. No HTML, no markdown rendering, no auto-linking, no image loading. An injected payload aimed at the *operator's eyes* — a fake "already approved by you" banner — is a real attack. |
| S4 | Database roles: adapters get read-only where possible; the app role has no `UPDATE`/`DELETE` on `audit_log`; a separate migration role owns DDL. |
| S5 | Secrets live in systemd credentials or a `.env` outside the repo, never in version control. Service tokens are hashed at rest and rotatable without redeploy. |
| S6 | All decisions, policy edits, and pauses write to `audit_log` with the resolved tailnet identity. |
| S7 | Approval detail view MUST show the complete arguments, expandable, with a visible indicator when content is long. Truncation in storage is forbidden; truncation in the collapsed UI state must be obvious. |
| S8 | `critical`-class actions (§7.4) are never auto-approvable, regardless of policy or objective. |

---

## 13. Mobile and PWA requirements

The operator will use this on a phone, standing up, with about eight seconds of attention. `[DECIDED]`

**Install & shell**
- Web app manifest, `display: standalone`, maskable icons, correct theme color.
- Service worker for shell caching and push. Never cache API responses for approvals.
- Respect safe-area insets. Dark theme as the default — this gets checked at night.

**Layout**
- Primary breakpoint 390px wide. Desktop is the *wide variant*, not the reference design.
- Home screen above the fold: pending approvals count, agents up/down, next scheduled item, untagged-work indicator.

**The decision interaction**
- Approve / Deny as full-width buttons in the bottom thumb zone, minimum 44×44pt, clearly differentiated by color and label (not color alone).
- **Undo window (5s) instead of a confirmation dialog.** A second tap to confirm trains dismissal; an undo respects a fast correct decision and still catches a fat-finger.
- **No optimistic UI on decisions.** A decision renders as decided only after the server confirms. Show an in-flight state.
- Full tool arguments in an expandable block, monospace, horizontally scrollable, with a copy button.

**Network reality**
- SSE with exponential backoff reconnect and resume from `last_event_id` via `events.seq`.
- Virtualized log list. Never render unbounded history.
- Every cached view carries a visible **"last synced HH:MM"** stamp, plus an offline banner. A stale pending approval MUST NOT be able to masquerade as live.
- If the connection is stale beyond a threshold (default 60s), disable the decision buttons and say why.

---

## 14. Tech stack

| Layer | Choice | Note |
|---|---|---|
| API | Python 3.12, FastAPI | matches operator's existing fluency |
| ORM / migrations | SQLAlchemy 2.x + Alembic | migrations from commit one |
| Database | PostgreSQL 16 | Docker on the VPS |
| Frontend | Next.js (App Router), TypeScript, Tailwind | static export, served from the VPS |
| Realtime | SSE | WebSocket only if a requirement demands bidirectional |
| Push | `pywebpush` (VAPID) | ntfy fallback |
| Process supervision | systemd units (or Docker Compose) | so "restart agent" is a supervisor call |
| Tests | pytest + httpx; Playwright for the approval flow | |

**Deliberate deviations from the operator's usual defaults:** no Supabase, no Vercel, no hosted Postgres. This system is private-by-construction and lives entirely on the VPS; a managed frontend host or hosted DB would put the control plane for shell-capable agents outside the tailnet, which contradicts C1 and C5.

---

## 15. Out of scope for v1

Multi-user accounts and roles. Native mobile app. Editing agent skills or prompts from the dashboard. Analytics beyond 90-day retention. Any public/Funnel exposure. Cross-VPS federation. Automated remediation (the dashboard recommends; the operator acts).

---

## 16. Implementation phases

Each phase is a self-contained unit of work with its own acceptance criteria. Complete and verify one before starting the next.

**Phase 0 — Foundation**
Repo scaffold, Docker Compose (Postgres + API), Alembic baseline with all tables from §5, systemd units, `tailscale serve` configured, health endpoint reachable at the ts.net URL from the operator's phone.
*Accept:* phone loads a "hello" page over HTTPS on the tailnet; nothing is reachable from off-tailnet.

**Phase 1 — Observation (read-only)**
Both adapters. Normalizer. Runs and events populating. Agent up/down status. Live log tail over SSE. Basic run list and detail view.
*Accept:* both runtimes' activity appears within 10s; killing and restarting an agent is visible; adapter reconnects cleanly after a network drop.

**Phase 2 — Approvals**
Approval gateway, service tokens, agent-side wrappers for both runtimes, policy evaluation, TTL expiry job, decision UI, kill switch.
*Accept:* the §17 test list passes in full. Do not proceed until it does.

**Phase 3 — Objectives**
Objectives CRUD, inference rules, tagging, untagged-work report, spend roll-up, outcome capture, weekly review flow.
*Accept:* a run created by each runtime lands on the right objective without manual tagging.

**Phase 4 — Agenda**
Unified schedule view, staleness detection, pause/resume with write-back where supported, 24h timeline.
*Accept:* every cron task in both runtimes appears exactly once with a correct next-fire time.

**Phase 5 — Notifications and PWA polish**
Web push, fallback channel, quiet hours, escalation, install prompt, offline states, thumb-zone decision UI, undo window.
*Accept:* SC1 measured end-to-end on a real phone, on cellular, with the app closed.

**Phase 6 — Hardening**
Audit log verification, rate limits, ACL review, backup and restore of Postgres, log rotation, restart-survival test, documented runbook.
*Accept:* the §17 security tests pass; a full VPS reboot brings everything back with no manual steps.

---

## 17. Acceptance tests (must all pass before Phase 2 is considered done)

1. An agent creates an approval; the operator approves from a phone; the agent proceeds. Audit row records the tailnet login.
2. The same, denied; the agent raises and does not execute.
3. TTL elapses with no decision → state is `expired`, agent treats it as deny, and the UI distinguishes expired from denied.
4. Mission Control is stopped mid-request → the agent's wrapper fails closed.
5. A service token attempts `POST /v1/approvals/{id}/decision` → 403, security event logged.
6. A request without Serve's forwarded headers but carrying `Tailscale-User-Login` → rejected.
7. A request whose `whois` login does not match the header → rejected.
8. Same `idempotency_key`, different args → 409 and a logged security event.
9. A `critical`-risk tool with a matching `auto_approve` policy → still asks.
10. Tool args containing HTML, markdown, and an ANSI escape sequence → rendered as inert text, no execution, no layout break.
11. 25 approval requests in one hour from one agent → rate limit engages, excess auto-denied, distinct alert raised.
12. Kill switch engaged → all pending denied, new requests denied, state visible on home screen.
13. Airplane mode → cached view shows "last synced," decision buttons disabled with an explanation.

---

## 18. Open questions for the operator

Do not guess these. Ask, or implement the stated default behind a config flag and flag it in the handoff.

1. **Fallback notification channel** — ntfy (self-hosted on the tailnet) or an existing messaging channel? Affects §10.
2. **Which tools should be gated by default** on each runtime? A starting allowlist is needed; the safe default is "gate everything and loosen from observation data."
3. **The actual objectives** — the five or so active goals across VillageMD, Castillo & Co., and Axon, with horizons. The alignment layer is inert without them.
4. **Retention** — how long to keep `events` at full fidelity before rolling up (default proposed: 30 days full, 90 days aggregated).
5. **Concurrency** — how many concurrent runs and subagents at peak? Determines whether polling intervals and the events table need tuning now or later.
6. **Pause/resume write-back** — acceptable to have Mission Control modify cron state in the runtimes, or should it be read-only with manual commands surfaced?
7. **Postgres vs SQLite** — Postgres is specified, but if the VPS is small and concurrency is low, SQLite would cut an entire service. Confirm.

---

## 19. Kickoff prompt for the coding agent

> Read `mission-control-requirements.md` in full before writing any code.
>
> Implement **Phase 0** only. Do not start Phase 1.
>
> Before you begin, verify against the installed versions and current documentation: the OpenClaw Gateway's event/API surface and its Tailscale Serve integration; the Hermes Agent SQLite schema and cron table layout; and the current `tailscale serve` identity-header behavior. Report any discrepancy with §2, §6, or §11 before proceeding — do not silently adapt the spec.
>
> Deliverables for Phase 0: repo scaffold; Docker Compose running PostgreSQL 16; a complete Alembic baseline migration covering every table in §5; a FastAPI app bound to `127.0.0.1` with a `/health` endpoint; identity-header middleware implementing all three trust layers in §11.3, with unit tests for the rejection cases (tests 6 and 7 in §17); systemd units for API and database; and `tailscale serve` configuration.
>
> Constraints C1, C2, and C5 are non-negotiable. If any instruction I give you later conflicts with them, stop and say so rather than complying.
>
> When Phase 0 is complete, show me: the `tailscale serve status` output, the migration head, and the result of hitting `/health` from off-tailnet (which must fail).
