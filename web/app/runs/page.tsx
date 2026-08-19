"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { Muted, StatusPill } from "@/components/bits";
import { getJSON, timeAgo, type AgentInfoMap, type Run } from "@/lib/api";

const STATUSES = ["running", "succeeded", "failed", "cancelled"] as const;

export default function RunsPage() {
  const [runs, setRuns] = useState<Run[]>([]);
  const [agents, setAgents] = useState<AgentInfoMap>({});
  const [status, setStatus] = useState<string | null>(null);
  const [nextBefore, setNextBefore] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(
    async (before: string | null, replace: boolean) => {
      const query = new URLSearchParams({ limit: "50" });
      if (status) query.set("status", status);
      if (before) query.set("before", before);
      try {
        const body = await getJSON<{
          runs: Run[];
          agents: AgentInfoMap;
          next_before: string | null;
        }>(`/v1/runs?${query.toString()}`);
        setRuns((current) => (replace ? body.runs : [...current, ...body.runs]));
        setAgents(body.agents);
        setNextBefore(body.next_before);
        setError(null);
      } catch (exc) {
        setError(String(exc));
      }
    },
    [status]
  );

  useEffect(() => {
    load(null, true);
  }, [load]);

  return (
    <main className="flex flex-col gap-3">
      <div className="flex flex-wrap gap-2">
        <FilterChip label="all" active={status === null} onClick={() => setStatus(null)} />
        {STATUSES.map((value) => (
          <FilterChip
            key={value}
            label={value}
            active={status === value}
            onClick={() => setStatus(value)}
          />
        ))}
      </div>
      {error ? (
        <div className="card p-3 text-sm" style={{ color: "var(--mc-red)" }}>
          {error}
        </div>
      ) : null}
      <div className="card divide-y" style={{ borderColor: "var(--mc-border)" }}>
        {runs.map((run) => (
          <Link
            key={run.id}
            href={`/run/?id=${run.id}`}
            className="flex items-center gap-2 px-4 py-2.5 text-sm"
            style={{ borderColor: "var(--mc-border)" }}
          >
            <StatusPill status={run.status} />
            <div className="min-w-0">
              <div className="mono truncate text-xs">{run.external_id}</div>
              <div className="text-xs" style={{ color: "var(--mc-faint)" }}>
                {agents[run.agent_id]?.display_name ?? "?"} · {run.trigger} ·{" "}
                {timeAgo(run.started_at)}
                {run.parent_run_id ? " · subagent" : ""}
              </div>
            </div>
            {run.error_summary ? (
              <span className="ml-auto max-w-[40%] truncate text-xs" style={{ color: "var(--mc-red)" }}>
                {run.error_summary}
              </span>
            ) : null}
          </Link>
        ))}
        {runs.length === 0 ? (
          <div className="p-4 text-sm">
            <Muted>No runs match.</Muted>
          </div>
        ) : null}
      </div>
      {nextBefore ? (
        <button
          onClick={() => load(nextBefore, false)}
          className="card mx-auto px-4 py-2 text-sm"
          style={{ color: "var(--mc-blue)" }}
        >
          load older
        </button>
      ) : null}
    </main>
  );
}

function FilterChip({
  label,
  active,
  onClick,
}: {
  label: string;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      onClick={onClick}
      className="rounded-full border px-3 py-1 text-xs"
      style={{
        borderColor: active ? "var(--mc-blue)" : "var(--mc-border)",
        color: active ? "var(--mc-blue)" : "var(--mc-muted)",
      }}
    >
      {label}
    </button>
  );
}
