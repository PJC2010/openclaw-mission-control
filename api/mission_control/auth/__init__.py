"""Authentication.

Two deliberately separate paths (§7.5 — do not merge them into one
"is authenticated" check):

- `identity` — the OPERATOR path: Tailscale Serve identity headers verified
  against `tailscale whois` (§11). Only this path may ever decide approvals.
- service tokens — the AGENT path (`approvals:create` / `approvals:poll`
  only). Lands in Phase 2 as its own module with its own middleware and its
  own tests; it must never gain decide/objectives/policies/audit scopes.
"""
