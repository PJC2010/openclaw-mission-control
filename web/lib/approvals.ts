// Approval types and the display-safety helpers.
//
// Everything an agent supplies (tool name, arguments, rationale) is treated
// as hostile text (§12 S3): rendered only as React text nodes, never HTML,
// never markdown, never auto-linked, and with control characters made
// visible rather than allowed to act.

export type RiskLevel = "low" | "medium" | "high" | "critical";

export type Approval = {
  id: string;
  agent_id: string;
  agent_name: string;
  tool_name: string;
  risk_level: RiskLevel;
  risk_categories: { categories?: string[]; reasons?: string[]; never_auto_approvable?: boolean };
  claimed_risk: RiskLevel | null;
  state: "pending" | "approved" | "denied" | "expired" | "cancelled";
  rationale: string;
  source: string;
  created_at: string;
  expires_at: string;
  decided_at: string | null;
  decided_by: string | null;
  decided_via: string | null;
  decision_note: string | null;
  external_run_id: string | null;
  consumed_at?: string | null;
  resolution_state?: string;
  resolution_attempts?: number;
  resolution_error?: string | null;
};

export type ApprovalDetail = Approval & {
  tool_args: unknown;
  args_digest: string;
};

export type KillSwitch = {
  engaged: boolean;
  updated_at?: string;
  updated_by?: string;
};

export const riskColor: Record<RiskLevel, string> = {
  low: "var(--mc-muted)",
  medium: "var(--mc-blue)",
  high: "var(--mc-amber)",
  critical: "var(--mc-red)",
};

// Characters that can act rather than display:
//   C0/C1 controls (minus \n and \t, which <pre> renders harmlessly),
//   and the invisible/bidi formatting set — soft hyphen, zero-width
//   joiners, LRM/RLM, the bidi embedding+override block, the isolates,
//   and BOM.
//
// The bidi overrides matter as much as ESC here. U+202E reverses the
// visual order of everything after it, so an argument reading
// "rm -rf /" can be made to display as something harmless while the
// approved bytes are unchanged. JSON.stringify escapes the C0 range on
// its own but passes these through untouched, which is precisely the
// "payload aimed at the operator's eyes" S3 warns about.
//
// Built from escapes so no literal control byte ever sits in this source.
const CONTROL_CHARS = new RegExp(
  "[\\u0000-\\u0008\\u000B\\u000C\\u000E-\\u001F\\u007F-\\u009F" +
    "\\u00AD\\u200B-\\u200F\\u202A-\\u202E\\u2060\\u2066-\\u2069\\uFEFF]",
  "g"
);

/**
 * Replace control characters with visible escapes.
 *
 * An ANSI escape sequence in a tool argument must be *shown*, not obeyed:
 * left raw it can hide text in a terminal the operator later pastes into,
 * and it can distort this layout (§17 test 10).
 */
export function visibleControlChars(text: string): string {
  return text.replace(CONTROL_CHARS, (char) => {
    const code = char.codePointAt(0)!;
    return code <= 0xff
      ? "\\x" + code.toString(16).padStart(2, "0")
      : "\\u" + code.toString(16).padStart(4, "0");
  });
}

/** Pretty-print arguments for display. Never truncated here (§12 S7). */
export function formatArgs(args: unknown): string {
  let text: string;
  try {
    text = typeof args === "string" ? args : JSON.stringify(args, null, 2);
  } catch {
    text = String(args);
  }
  return visibleControlChars(text ?? "");
}

export function secondsUntil(iso: string): number {
  return Math.max(0, Math.round((new Date(iso).getTime() - Date.now()) / 1000));
}

export function countdown(iso: string): string {
  const seconds = secondsUntil(iso);
  if (seconds <= 0) return "expired";
  const minutes = Math.floor(seconds / 60);
  return minutes > 0 ? minutes + "m " + (seconds % 60) + "s" : seconds + "s";
}
