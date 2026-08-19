"use client";

import { statusColor } from "@/lib/api";

export function StatusPill({ status }: { status: string }) {
  const color = statusColor[status] ?? "var(--mc-muted)";
  return (
    <span
      className="inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-xs"
      style={{ borderColor: "var(--mc-border)", color }}
    >
      <span aria-hidden className="h-1.5 w-1.5 rounded-full" style={{ background: color }} />
      {status}
    </span>
  );
}

export function Muted({ children }: { children: React.ReactNode }) {
  return <span style={{ color: "var(--mc-muted)" }}>{children}</span>;
}

export function ConnectionBanner({
  state,
  lastSync,
}: {
  state: "live" | "connecting" | "lost";
  lastSync: string | null;
}) {
  // §13: cached views carry a visible sync stamp; a stale view must not
  // masquerade as live.
  const label =
    state === "live"
      ? `live · synced ${lastSync ?? "…"}`
      : state === "connecting"
        ? "connecting…"
        : `connection lost · last synced ${lastSync ?? "never"}`;
  const color =
    state === "live" ? "var(--mc-green)" : state === "connecting" ? "var(--mc-amber)" : "var(--mc-red)";
  return (
    <div className="flex items-center gap-2 text-xs" style={{ color }}>
      <span aria-hidden className="h-1.5 w-1.5 rounded-full" style={{ background: color }} />
      {label}
    </div>
  );
}
