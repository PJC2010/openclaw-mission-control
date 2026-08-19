"use client";

import { useCallback, useState } from "react";
import { ApprovalRow, KillSwitchBanner } from "@/components/ApprovalBits";
import { useFreshness } from "@/lib/useFreshness";
import { getJSON } from "@/lib/api";
import type { Approval, KillSwitch } from "@/lib/approvals";

export default function ApprovalsPage() {
  const [approvals, setApprovals] = useState<Approval[]>([]);
  const [killSwitch, setKillSwitch] = useState<KillSwitch>({ engaged: false });
  const [showAll, setShowAll] = useState(false);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    const query = showAll ? "" : "?state=pending";
    const body = await getJSON<{ approvals: Approval[]; kill_switch: KillSwitch }>(
      `/v1/approvals${query}`
    );
    setApprovals(body.approvals);
    setKillSwitch(body.kill_switch);
  }, [showAll]);

  const { lastSyncLabel, stale, error, refresh: run } = useFreshness(refresh, 8000);

  // §7.7 — Pause All Agents, two taps from home: home → queue → this.
  const toggleKillSwitch = async () => {
    setBusy(true);
    try {
      await fetch("/v1/system/kill-switch", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ engaged: !killSwitch.engaged }),
      });
      await run();
    } finally {
      setBusy(false);
    }
  };

  return (
    <main className="flex flex-col gap-3">
      <KillSwitchBanner engaged={killSwitch.engaged} />

      <div className="flex items-center gap-2 text-xs" style={{ color: stale ? "var(--mc-amber)" : "var(--mc-muted)" }}>
        <span>synced {lastSyncLabel}</span>
        {stale ? <span>· stale</span> : null}
        <button onClick={() => setShowAll((value) => !value)} className="ml-auto" style={{ color: "var(--mc-blue)" }}>
          {showAll ? "pending only" : "show all"}
        </button>
      </div>

      {error ? (
        <div className="card p-3 text-sm" style={{ color: "var(--mc-red)" }}>{error}</div>
      ) : null}

      <div className="card">
        {approvals.length === 0 ? (
          <p className="p-4 text-sm" style={{ color: "var(--mc-muted)" }}>
            {showAll ? "No approvals recorded." : "Nothing waiting on you."}
          </p>
        ) : (
          approvals.map((approval) => <ApprovalRow key={approval.id} approval={approval} />)
        )}
      </div>

      <button
        onClick={toggleKillSwitch}
        disabled={busy}
        className="mt-2 w-full rounded-xl border-2 py-4 text-sm font-semibold disabled:opacity-50"
        style={{
          borderColor: killSwitch.engaged ? "var(--mc-green)" : "var(--mc-red)",
          color: killSwitch.engaged ? "var(--mc-green)" : "var(--mc-red)",
          minHeight: 56,
        }}
      >
        {killSwitch.engaged ? "Resume all agents" : "Pause all agents"}
      </button>
    </main>
  );
}
