"use client";

// The decision surface. Standing up, on a phone, with about eight seconds
// of attention (§13):
//   · full-width Approve / Deny in the bottom thumb zone, ≥44pt, told
//     apart by label and shape as well as colour
//   · a 5s undo window instead of a confirm dialog — a second tap trains
//     dismissal, an undo respects a fast correct decision and still
//     catches a fat-finger
//   · NO optimistic UI: it renders as decided only once the server says so
//   · stale connection disables the buttons and says why

import Link from "next/link";
import { Suspense, useCallback, useEffect, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";
import { ArgumentBlock, KillSwitchBanner, RiskBadge } from "@/components/ApprovalBits";
import { useFreshness } from "@/lib/useFreshness";
import { countdown, type ApprovalDetail, type KillSwitch } from "@/lib/approvals";
import { getJSON } from "@/lib/api";

const UNDO_MS = 5000;

type Phase =
  | { kind: "idle" }
  | { kind: "undo"; approve: boolean; endsAt: number }
  | { kind: "sending"; approve: boolean }
  | { kind: "failed"; message: string };

export default function ApprovalPage() {
  return (
    <Suspense>
      <ApprovalView />
    </Suspense>
  );
}

function ApprovalView() {
  const approvalId = useSearchParams().get("id");
  const [approval, setApproval] = useState<ApprovalDetail | null>(null);
  const [killSwitch, setKillSwitch] = useState<KillSwitch>({ engaged: false });
  const [phase, setPhase] = useState<Phase>({ kind: "idle" });
  const [tick, setTick] = useState(0);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const refresh = useCallback(async () => {
    if (!approvalId) return;
    const [detail, ks] = await Promise.all([
      getJSON<ApprovalDetail>(`/v1/approvals/${approvalId}`),
      getJSON<KillSwitch>("/v1/system/kill-switch"),
    ]);
    setApproval(detail);
    setKillSwitch(ks);
  }, [approvalId]);

  const { stale, online, lastSyncLabel, error } = useFreshness(refresh, 8000);

  useEffect(() => {
    const id = setInterval(() => setTick((value) => value + 1), 1000);
    return () => clearInterval(id);
  }, []);

  const send = useCallback(
    async (approve: boolean) => {
      if (!approvalId) return;
      setPhase({ kind: "sending", approve });
      try {
        const response = await fetch(`/v1/approvals/${approvalId}/decision`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ approve }),
        });
        if (!response.ok) throw new Error(`server said ${response.status}`);
        // Only now does it render as decided — the server is the authority.
        const decided = (await response.json()) as { state: string };
        await refresh();
        if (decided.state !== (approve ? "approved" : "denied")) {
          setPhase({
            kind: "failed",
            message: `recorded as "${decided.state}" instead — the request may have expired or been paused`,
          });
        } else {
          setPhase({ kind: "idle" });
        }
      } catch (exc) {
        setPhase({ kind: "failed", message: String(exc) });
      }
    },
    [approvalId, refresh]
  );

  const beginDecision = (approve: boolean) => {
    if (timer.current) clearTimeout(timer.current);
    setPhase({ kind: "undo", approve, endsAt: Date.now() + UNDO_MS });
    timer.current = setTimeout(() => send(approve), UNDO_MS);
  };

  const undo = () => {
    if (timer.current) clearTimeout(timer.current);
    timer.current = null;
    setPhase({ kind: "idle" });
  };

  useEffect(() => () => { if (timer.current) clearTimeout(timer.current); }, []);

  if (!approvalId) return <p className="text-sm">No approval selected.</p>;
  if (error && !approval)
    return <div className="card p-3 text-sm" style={{ color: "var(--mc-red)" }}>{error}</div>;
  if (!approval) return <p className="text-sm" style={{ color: "var(--mc-muted)" }}>Loading…</p>;

  const pending = approval.state === "pending";
  const expired = approval.state === "expired";
  const blockedReason = !online
    ? "offline — reconnect to decide"
    : stale
      ? `connection stale (last synced ${lastSyncLabel}) — decisions are disabled until it recovers`
      : killSwitch.engaged
        ? "all agents are paused; release the kill switch to decide"
        : null;

  return (
    <main className="flex flex-col gap-4 pb-44">
      <KillSwitchBanner engaged={killSwitch.engaged} />

      <section className="card p-4">
        <div className="flex items-center justify-between gap-2">
          <RiskBadge level={approval.risk_level} claimed={approval.claimed_risk} />
          <span className="text-xs" style={{ color: "var(--mc-faint)" }}>
            {pending ? `expires in ${countdown(approval.expires_at)}` : approval.state}
          </span>
        </div>

        <h1 className="mono mt-3 break-all text-base">{approval.tool_name}</h1>
        <p className="mt-1 text-xs" style={{ color: "var(--mc-muted)" }}>
          {approval.agent_name} · requested {new Date(approval.created_at).toLocaleTimeString()}
        </p>

        {approval.risk_categories?.reasons?.length ? (
          <ul className="mt-3 flex flex-col gap-1">
            {approval.risk_categories.reasons.map((reason) => (
              <li key={reason} className="text-xs" style={{ color: "var(--mc-amber)" }}>
                • {reason}
              </li>
            ))}
          </ul>
        ) : null}

        {approval.rationale ? (
          <div className="mt-3">
            <div className="text-xs" style={{ color: "var(--mc-muted)" }}>
              agent&apos;s stated reason
            </div>
            {/* Inert text: an injected "already approved by you" banner is a
                real attack aimed at the operator's eyes (S3). */}
            <p className="mt-1 whitespace-pre-wrap break-words text-sm">{approval.rationale}</p>
          </div>
        ) : null}

        <ArgumentBlock args={approval.tool_args} />
      </section>

      {!pending ? (
        <section className="card p-4 text-sm">
          <div className="font-semibold" style={{ color: expired ? "var(--mc-amber)" : "var(--mc-muted)" }}>
            {expired
              ? "Expired — nobody decided in time, so the agent was denied."
              : `Decided: ${approval.state}`}
          </div>
          <div className="mt-1 text-xs" style={{ color: "var(--mc-faint)" }}>
            {approval.decided_by ? `by ${approval.decided_by}` : "automatically"}
            {approval.decided_via ? ` · via ${approval.decided_via}` : ""}
            {approval.decided_at ? ` · ${new Date(approval.decided_at).toLocaleString()}` : ""}
          </div>
          {approval.decision_note ? (
            <p className="mt-2 whitespace-pre-wrap text-xs">{approval.decision_note}</p>
          ) : null}
          {approval.resolution_state === "pending" || approval.resolution_state === "failed" ? (
            <p className="mt-2 text-xs" style={{ color: "var(--mc-red)" }}>
              Not yet delivered to the runtime
              {approval.resolution_attempts ? ` (${approval.resolution_attempts} attempts)` : ""}
              {approval.resolution_error ? `: ${approval.resolution_error}` : ""}
            </p>
          ) : null}
          {approval.consumed_at ? (
            <p className="mt-1 text-xs" style={{ color: "var(--mc-faint)" }}>
              used once at {new Date(approval.consumed_at).toLocaleTimeString()} — a second
              attempt would be refused
            </p>
          ) : null}
          <Link href="/approvals/" className="mt-3 inline-block text-xs" style={{ color: "var(--mc-blue)" }}>
            back to queue →
          </Link>
        </section>
      ) : null}

      {pending ? (
        <div
          className="fixed inset-x-0 bottom-0 border-t px-4 pt-3"
          style={{
            borderColor: "var(--mc-border)",
            background: "var(--mc-card)",
            paddingBottom: "calc(env(safe-area-inset-bottom) + 12px)",
          }}
        >
          <div className="mx-auto w-full max-w-[430px] md:max-w-3xl">
            {phase.kind === "undo" ? (
              <button
                onClick={undo}
                className="w-full rounded-xl border-2 py-4 text-base font-semibold"
                style={{ borderColor: "var(--mc-amber)", color: "var(--mc-amber)", minHeight: 56 }}
              >
                UNDO — {phase.approve ? "approving" : "denying"} in{" "}
                {Math.max(0, Math.ceil((phase.endsAt - Date.now()) / 1000))}s
              </button>
            ) : phase.kind === "sending" ? (
              <div
                className="w-full rounded-xl border-2 py-4 text-center text-base font-semibold"
                style={{ borderColor: "var(--mc-blue)", color: "var(--mc-blue)", minHeight: 56 }}
              >
                Sending decision…
              </div>
            ) : (
              <>
                {blockedReason ? (
                  <p className="mb-2 text-center text-xs" style={{ color: "var(--mc-amber)" }}>
                    {blockedReason}
                  </p>
                ) : null}
                {phase.kind === "failed" ? (
                  <p className="mb-2 text-center text-xs" style={{ color: "var(--mc-red)" }}>
                    {phase.message}
                  </p>
                ) : null}
                <div className="flex gap-3">
                  <button
                    disabled={Boolean(blockedReason)}
                    onClick={() => beginDecision(false)}
                    className="flex-1 rounded-xl border-2 py-4 text-base font-semibold disabled:opacity-40"
                    style={{ borderColor: "var(--mc-red)", color: "var(--mc-red)", minHeight: 56 }}
                  >
                    ✕ Deny
                  </button>
                  <button
                    disabled={Boolean(blockedReason)}
                    onClick={() => beginDecision(true)}
                    className="flex-1 rounded-xl py-4 text-base font-semibold disabled:opacity-40"
                    style={{ background: "var(--mc-green)", color: "#04140a", minHeight: 56 }}
                  >
                    ✓ Approve
                  </button>
                </div>
              </>
            )}
            <p className="mt-2 text-center text-xs" style={{ color: "var(--mc-faint)" }}>
              synced {lastSyncLabel}
              {tick < 0 ? "" : ""}
            </p>
          </div>
        </div>
      ) : null}
    </main>
  );
}
