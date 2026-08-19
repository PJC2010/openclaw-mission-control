// Same-origin API access. Agent-supplied strings from these payloads are
// only ever rendered as React text nodes (S3: inert text, no HTML).

export type Agent = {
  id: string;
  runtime: string;
  display_name: string;
  instance_key: string;
  status: string;
  last_heartbeat_at: string | null;
  adapter: { adapter: string; ok: boolean; state: string; detail: string } | null;
};

export type Run = {
  id: string;
  agent_id: string;
  external_id: string;
  status: string;
  trigger: string;
  started_at: string;
  ended_at: string | null;
  error_summary: string | null;
  token_input: number | null;
  token_output: number | null;
  cost_usd: number | null;
  parent_run_id: string | null;
};

export type RunDetail = Run & {
  children: Run[];
  event_count: number;
  agent?: { display_name: string; runtime: string };
};

export type EventItem = {
  id: number;
  seq: number;
  ts: string;
  kind: string;
  payload: Record<string, unknown>;
};

export type AgentInfoMap = Record<string, { display_name: string; runtime: string }>;

export async function getJSON<T>(path: string): Promise<T> {
  const response = await fetch(path, { cache: "no-store" });
  if (!response.ok) {
    throw new Error(`${path} → ${response.status}`);
  }
  return (await response.json()) as T;
}

export function timeAgo(iso: string | null): string {
  if (!iso) return "—";
  const delta = (Date.now() - new Date(iso).getTime()) / 1000;
  if (delta < 0) return "just now";
  if (delta < 60) return `${Math.floor(delta)}s ago`;
  if (delta < 3600) return `${Math.floor(delta / 60)}m ago`;
  if (delta < 86400) return `${Math.floor(delta / 3600)}h ago`;
  return `${Math.floor(delta / 86400)}d ago`;
}

export function clock(date: Date): string {
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

export const statusColor: Record<string, string> = {
  up: "var(--mc-green)",
  running: "var(--mc-blue)",
  succeeded: "var(--mc-green)",
  down: "var(--mc-red)",
  failed: "var(--mc-red)",
  cancelled: "var(--mc-amber)",
  unknown: "var(--mc-faint)",
};
