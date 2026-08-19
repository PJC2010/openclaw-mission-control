# Mission Control — plan to completion

Status as of 2026-08-19. Phases 0–2 are built and tested; 3–6 remain.
This is the working plan, not a promise about dates — sequencing and
dependencies are the useful parts.

## Where things stand

| Phase | Scope | State |
|---|---|---|
| 0 | Foundation: schema, loopback API, §11.3 identity, systemd, serve | ✅ built, tested |
| 1 | Observation: both adapters, normalizer, SSE, run views | ✅ built, tested |
| 2 | Approvals: gateway, tokens, both wrappers, policy, kill switch, decision UI | ✅ built, tested |
| 3 | Objectives & alignment | ⬜ next |
| 4 | Agenda | ⬜ |
| 5 | Notifications & PWA | ⬜ |
| 6 | Hardening | ⬜ |

**Nothing is deployed.** All three phases were built and verified in an
isolated container against a real PostgreSQL 16, a fake OpenClaw gateway,
and an on-disk Hermes fixture. The VPS has never run this code. The
deployment path is Phase 0 → 1 → 2 runbooks in order, and it is the single
highest-value next action regardless of which phase comes next: until it
runs on the VPS, every remaining phase is being built against assumptions.

## The critical path

```
   deploy 0→1→2 on the VPS ─── the §17 device checks ─── SC1 measured
            │
            ├── needs: your objectives ──→ Phase 3 ──→ Phase 4
            │
            └── needs: notification channel ──→ Phase 5 ──→ Phase 6
```

Phases 3 and 5 are independent of each other; 4 depends on 3 (tasks are
tagged to objectives); 6 depends on everything.

---

## Phase 3 — Objectives and alignment

**Blocked on you.** §18.3: the five-ish live goals across VillageMD,
Castillo & Co. and Axon, with horizons. The alignment layer is inert
without them, and inventing placeholders would produce a tagging model
fitted to fiction.

Build:
- Objectives CRUD (the schema already exists) with the soft warning above
  five active.
- **Inference rules first** (§8.1): a rules table mapping (agent,
  skill/task name, channel, keyword) → objective. Manual tagging is the
  correction path, not the primary one — §8.1 is explicit that if manual
  tagging is required, it stops by week three.
- Untagged-work report on the home screen (§8.2) — count, tokens, cost of
  runs matching no active objective, with drill-down.
- Spend by objective and by track, weekly roll-up (§8.3).
- One-tap useful / not-useful on completed runs (§8.4).
- Weekly review flow, target under two minutes (§8.5).
- Drift indicators (§8.6): untagged share over 30%, an objective with zero
  runs in 14 days, one objective eating a configurable share of spend.

Accept: a run from each runtime lands on the right objective with no
manual tagging.

Notes: Phase 1 has been recording `token_input`/`token_output`/`cost_usd`
from Hermes since day one, so the spend roll-up has real history to work
with immediately. OpenClaw's audit ledger is metadata-only (P1-D2), so
OpenClaw cost attribution needs `sessions.usage` — a small addition to
that adapter, worth doing as the first task of this phase.

## Phase 4 — Agenda

Depends on Phase 3 for objective ownership of tasks.

- One list across both runtimes: name, runtime, cron in plain English,
  next fire in your timezone, last outcome, consecutive failures, owning
  objective. `scheduled_tasks` is already populated by the Hermes adapter;
  the OpenClaw side needs `cron.list`/`cron.runs` ingestion.
- **Staleness flag** (§9): a task that keeps "succeeding" while producing
  no captured outcome or artifact for N runs (default 5). This is the
  zombie cron job, the most common silent drift.
- Pause/resume per task, with write-back where the runtime supports it.
  **Blocked on you** (§18.6): is Mission Control allowed to mutate cron
  state in the runtimes, or should it stay read-only and show the exact
  manual command? Everything currently ships `writeback_supported=false`,
  which is the honest default until you decide.
- 24-hour timeline on the home screen.

Accept: every cron task in both runtimes appears exactly once with a
correct next-fire time.

## Phase 5 — Notifications and PWA

**Blocked on you** (§18.1): ntfy self-hosted on the tailnet, or an
existing messaging channel, as the fallback behind Web Push.

- Web Push with VAPID (`pywebpush`), which is what makes SC1 real — until
  this ships, the queue is pull-only and "under 30 seconds" depends on you
  happening to look.
- The fallback channel.
- Quiet hours with `critical` always overriding (§10).
- Escalation: a `critical` approval still pending at 50% of its TTL
  re-notifies on all channels.
- Notification bodies carry tool name, risk level and objective **only** —
  never arguments; they appear on a lock screen (§10).
- Deep-link into the dashboard; no approve/deny buttons in the payload,
  because that path carries no tailnet identity.
- PWA polish: manifest, maskable icons, install prompt, service worker for
  the shell (never caching approval responses), offline states.

Accept: SC1 measured on a real phone, on cellular, with the app closed.

One deployment note: the API unit currently sets `IPAddressDeny=any` with
only localhost allowed. Web Push needs outbound internet, so that unit
gets loosened deliberately in this phase — it is called out in the unit's
comments already.

## Phase 6 — Hardening

- **Backups and restore drill for PostgreSQL.** Worth pulling forward:
  Phase 1 is already writing real history, and there is no backup today.
- Audit-log verification, log rotation, restart-survival test, ACL review.
- The retention roll-up (§18.4): 30 days full fidelity, 90 days
  aggregated — this also settles the `events` partitioning question
  deferred in D4.
- Close the carried debt from P2-F: restart divergence (block deciding on
  a bridged row whose runtime binding is unconfirmed), and code-enforced
  provisioning assertions for the "gate armed" tuple on both runtimes.
- Documented operator runbook, consolidated.

Accept: the §17 security tests pass and a full VPS reboot brings
everything back with no manual steps.

---

## What I need from you, in the order it blocks work

1. **Deploy, or tell me to keep building.** Nothing is on the VPS. The
   three runbooks are written; the four things only you can do are the
   tailnet HTTPS cert, the ACL, the OpenClaw token, and the version
   pre-flight.
2. **Which tools to gate by default** (§18.2). Phase 2 currently gates
   everything unknown and classifies six critical categories server-side.
   Phase 1's observation data now gives you the evidence to loosen from —
   that was the intended sequence.
3. **Your actual objectives** (§18.3) — blocks Phase 3 entirely.
4. **Fallback notification channel** (§18.1) — blocks Phase 5.
5. **Cron write-back, yes or no** (§18.6) — blocks half of Phase 4.
6. Retention windows (§18.4) and peak concurrency (§18.5) — I can default
   both; say so if you would rather choose.

## Risks I am carrying

- **Everything runtime-facing is verified against source and docs, not
  against your installed versions.** The Hermes schema guard and the
  OpenClaw method-availability check both fail loudly rather than
  silently, but a version mismatch is discovered on deployment, not
  before.
- **Two runtime settings can defeat the gate** and neither is enforceable
  from our side: OpenClaw's `askFallback` and Hermes's `approvals.mode`.
  Both are runbook steps with startup warnings; §P2-B and §P2-D explain
  the reasoning.
- **`system-agent` approvals in OpenClaw are not gated** — no enumeration
  path exists. Stated in the UI docs and the runbook rather than papered
  over.
- **SC1 is not yet achievable** — it needs Phase 5's push notifications.
  Phase 2 makes the decision possible; Phase 5 makes it fast.
