"use client";

// Home (§13 above-the-fold intent): agents up/down, recent runs, live tail.
// Pending approvals and untagged-work land here in Phases 2–3.

import Link from "next/link";
import { useEffect, useState } from "react";
import { ConnectionBanner, Muted, StatusPill } from "@/components/bits";
import { EventRow } from "@/components/EventRow";
import { useEventStream } from "@/lib/useEventStream";
import {
  getJSON,
  timeAgo,
  type Agent,
  type AgentInfoMap,
  type Run,
} from "@/lib/api";

export default function Home() {
  const [agents, setAgents] = useState<Agent[] | null>(null);
  const [runs, setRuns] = useState<Run[]>([]);
  const [agentInfo, setAgentInfo] = useState<AgentInfoMap>({});
  const [error, setError] = useState<string | null>(null);
  const { events, state, lastSync } = useEventStream({});

  useEffect(() => {
    let cancelled = false;
    async function refresh() {
      try {
        const [agentsBody, runsBody] = await Promise.all([
          getJSON<{ agents: Agent[] }>("/v1/agents"),
          getJSON<{ runs: Run[]; agents: AgentInfoMap }>("/v1/runs?limit=8"),
        ]);
        if (cancelled) return;
        setAgents(agentsBody.agents);
        setRuns(runsBody.runs);
        setAgentInfo(runsBody.agents);
        setError(null);
      } catch (exc) {
        if (!cancelled) setError(String(exc));
      }
    }
    refresh();
    const timer = setInterval(refresh, 10_000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, []);

  return (
    <main className="flex flex-col gap-4">
      <ConnectionBanner state={state} lastSync={lastSync} />
      {error ? (
        <div className="card p-3 text-sm" style={{ color: "var(--mc-red)" }}>
          {error}
        </div>
      ) : null}

      <section className="grid grid-cols-1 gap-3 md:grid-cols-2">
        {(agents ?? []).map((agent) => (
          <div key={agent.id} className="card p-4">
            <div className="flex items-center justify-between">
              <div className="font-medium">{agent.display_name}</div>
              <StatusPill status={agent.status} />
            </div>
            <div className="mt-1 text-xs" style={{ color: "var(--mc-muted)" }}>
              {agent.runtime} · heartbeat {timeAgo(agent.last_heartbeat_at)}
            </div>
            {agent.adapter && !agent.adapter.ok ? (
              <div className="mt-2 text-xs" style={{ color: "var(--mc-amber)" }}>
                adapter {agent.adapter.state}: {agent.adapter.detail}
              </div>
            ) : null}
          </div>
        ))}
        {agents !== null && agents.length === 0 ? (
          <div className="card p-4 text-sm">
            <Muted>
              No agents registered yet — set MC_OPENCLAW_URL / MC_HERMES_HOME and restart
              the API.
            </Muted>
          </div>
        ) : null}
      </section>

      <section className="card p-4">
        <div className="mb-2 flex items-center justify-between">
          <h2 className="text-sm font-semibold uppercase tracking-wider" style={{ color: "var(--mc-muted)" }}>
            Recent runs
          </h2>
          <Link href="/runs/" className="text-xs" style={{ color: "var(--mc-blue)" }}>
            all runs →
          </Link>
        </div>
        <div className="flex flex-col">
          {runs.map((run) => (
            <Link
              key={run.id}
              href={`/run/?id=${run.id}`}
              className="flex items-center gap-2 border-b py-2 text-sm last:border-b-0"
              style={{ borderColor: "var(--mc-border)" }}
            >
              <StatusPill status={run.status} />
              <span className="mono truncate text-xs">{run.external_id}</span>
              <span className="ml-auto shrink-0 text-xs" style={{ color: "var(--mc-faint)" }}>
                {agentInfo[run.agent_id]?.display_name ?? "?"} · {timeAgo(run.started_at)}
              </span>
            </Link>
          ))}
          {runs.length === 0 ? (
            <Muted>
              <span className="text-sm">No runs observed yet.</span>
            </Muted>
          ) : null}
        </div>
      </section>

      <section className="card p-4">
        <h2 className="mb-2 text-sm font-semibold uppercase tracking-wider" style={{ color: "var(--mc-muted)" }}>
          Live tail
        </h2>
        <div className="flex flex-col-reverse">
          {events.slice(-14).map((event) => (
            <EventRow key={event.id} event={event} />
          ))}
          {events.length === 0 ? (
            <Muted>
              <span className="text-sm">Waiting for events…</span>
            </Muted>
          ) : null}
        </div>
      </section>
    </main>
  );
}
