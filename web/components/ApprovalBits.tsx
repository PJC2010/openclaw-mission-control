"use client";

import Link from "next/link";
import { useState } from "react";
import {
  countdown,
  formatArgs,
  riskColor,
  type Approval,
  type RiskLevel,
} from "@/lib/approvals";

export function RiskBadge({ level, claimed }: { level: RiskLevel; claimed?: RiskLevel | null }) {
  const color = riskColor[level];
  return (
    <span className="inline-flex items-center gap-1.5">
      <span
        className="rounded-md border px-2 py-0.5 text-xs font-semibold uppercase tracking-wide"
        style={{ borderColor: color, color }}
      >
        {level}
      </span>
      {claimed && claimed !== level ? (
        // The agent's own claim is a hint at best and is shown as such
        // (§7.4): a compromised agent will understate its risk.
        <span className="text-xs" style={{ color: "var(--mc-faint)" }}>
          agent claimed {claimed}
        </span>
      ) : null}
    </span>
  );
}

export function ApprovalRow({ approval }: { approval: Approval }) {
  return (
    <Link
      href={`/approval/?id=${approval.id}`}
      className="flex flex-col gap-1 border-b px-4 py-3 last:border-b-0"
      style={{ borderColor: "var(--mc-border)" }}
    >
      <div className="flex items-center gap-2">
        <RiskBadge level={approval.risk_level} />
        {/* Agent-supplied tool name — a text node, never markup (S3). */}
        <span className="mono truncate text-sm">{approval.tool_name}</span>
        <span className="ml-auto shrink-0 text-xs" style={{ color: "var(--mc-faint)" }}>
          {approval.state === "pending" ? countdown(approval.expires_at) : approval.state}
        </span>
      </div>
      <div className="truncate text-xs" style={{ color: "var(--mc-muted)" }}>
        {approval.agent_name}
        {approval.risk_categories?.reasons?.length
          ? " · " + approval.risk_categories.reasons[0]
          : ""}
      </div>
    </Link>
  );
}

/**
 * The complete arguments (§12 S7). Storage never truncates; the collapsed
 * state here does, and says so in the control itself — an unobvious
 * truncation is exactly what an injected payload would hide behind.
 */
export function ArgumentBlock({ args }: { args: unknown }) {
  const text = formatArgs(args);
  const lines = text.split("\n");
  const long = lines.length > 12 || text.length > 900;
  const [expanded, setExpanded] = useState(!long);
  const shown = expanded ? text : lines.slice(0, 12).join("\n");
  const [copied, setCopied] = useState(false);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      setCopied(false);
    }
  };

  return (
    <div className="mt-2">
      <div className="mb-1 flex items-center gap-3 text-xs" style={{ color: "var(--mc-muted)" }}>
        <span>arguments</span>
        <span className="mono" style={{ color: "var(--mc-faint)" }}>
          {lines.length} lines · {text.length} chars
        </span>
        <button onClick={copy} className="ml-auto" style={{ color: "var(--mc-blue)" }}>
          {copied ? "copied" : "copy"}
        </button>
      </div>
      <pre
        className="mono max-h-[50vh] overflow-auto rounded-lg border p-2 text-xs leading-relaxed"
        style={{ borderColor: "var(--mc-border)", background: "var(--mc-bg)" }}
      >
        {shown}
      </pre>
      {long ? (
        <button
          onClick={() => setExpanded((value) => !value)}
          className="mt-1 w-full rounded-lg border py-2 text-xs"
          style={{ borderColor: "var(--mc-border)", color: "var(--mc-amber)" }}
        >
          {expanded
            ? "collapse"
            : `SHOWING FIRST 12 OF ${lines.length} LINES — tap to show all`}
        </button>
      ) : null}
    </div>
  );
}

export function KillSwitchBanner({ engaged }: { engaged: boolean }) {
  if (!engaged) return null;
  // §7.7 — an obvious visual state so the operator cannot forget it is on.
  return (
    <div
      className="rounded-xl border-2 px-4 py-3 text-sm font-semibold"
      style={{ borderColor: "var(--mc-red)", background: "#2a0f12", color: "var(--mc-red)" }}
    >
      ALL AGENTS PAUSED — every approval request is being denied.
    </div>
  );
}
