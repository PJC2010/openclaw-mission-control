"use client";

// Run detail with live log tail: REST backlog first, then SSE resuming from
// the last fetched event id (§13). Query-param routing keeps the page
// compatible with static export.

import Link from "next/link";
import { Suspense, useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import { ConnectionBanner, Muted, StatusPill } from "@/components/bits";
import { EventRow } from "@/components/EventRow";
import { useEventStream } from "@/lib/useEventStream";
import { getJSON, timeAgo, type EventItem, type RunDetail } from "@/lib/api";

export default function RunPage() {
  return (
    <Suspense>
      <RunView />
    </Suspense>
  );
}

function RunView() {
  const runId = useSearchParams().get("id");
  const [run, setRun] = useState<RunDetail | null>(null);
  const [backlog, setBacklog] = useState<EventItem[] | null>(null);
  const [afterId, setAfterId] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const { events: live, state, lastSync } = useEventStream({
    runId: runId ?? undefined,
    afterId,
    enabled: Boolean(runId),
  });

  useEffect(() => {
    if (!runId) return;
    let cancelled = false;
    (async () => {
      try {
        const [detail, eventsBody] = await Promise.all([
          getJSON<RunDetail>(`/v1/runs/${runId}`),
          getJSON<{ events: EventItem[] }>(`/v1/runs/${runId}/events?limit=500`),
        ]);
        if (cancelled) return;
        setRun(detail);
        setBacklog(eventsBody.events);
        setAfterId(eventsBody.events.at(-1)?.id ?? 0);
      } catch (exc) {
        if (!cancelled) setError(String(exc));
      }
    })();
    const timer = setInterval(async () => {
      try {
        if (!cancelled) setRun(await getJSON<RunDetail>(`/v1/runs/${runId}`));
      } catch {
        /* transient */
      }
    }, 15_000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [runId]);

  if (!runId) {
    return (
      <Muted>
        <span className="text-sm">No run selected.</span>
      </Muted>
    );
  }
  if (error) {
    return (
      <div className="card p-3 text-sm" style={{ color: "var(--mc-red)" }}>
        {error}
      </div>
    );
  }
  if (!run) {
    return (
      <Muted>
        <span className="text-sm">Loading…</span>
      </Muted>
    );
  }

  const events = [...(backlog ?? []), ...live];

  return (
    <main className="flex flex-col gap-4">
      <section className="card p-4">
        <div className="flex items-center justify-between gap-2">
          <span className="mono truncate text-sm">{run.external_id}</span>
          <StatusPill status={run.status} />
        </div>
        <dl
          className="mt-3 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-xs"
          style={{ color: "var(--mc-muted)" }}
        >
          <dt>agent</dt>
          <dd>{run.agent?.display_name ?? run.agent_id}</dd>
          <dt>trigger</dt>
          <dd>{run.trigger}</dd>
          <dt>started</dt>
          <dd>
            {new Date(run.started_at).toLocaleString()} ({timeAgo(run.started_at)})
          </dd>
          <dt>ended</dt>
          <dd>{run.ended_at ? new Date(run.ended_at).toLocaleString() : "—"}</dd>
          <dt>tokens</dt>
          <dd>
            {run.token_input ?? "—"} in / {run.token_output ?? "—"} out
          </dd>
          <dt>cost</dt>
          <dd>{run.cost_usd != null ? `$${run.cost_usd.toFixed(4)}` : "—"}</dd>
          {run.parent_run_id ? (
            <>
              <dt>parent</dt>
              <dd>
                <Link href={`/run/?id=${run.parent_run_id}`} style={{ color: "var(--mc-blue)" }}>
                  parent run →
                </Link>
              </dd>
            </>
          ) : null}
        </dl>
        {run.error_summary ? (
          <div className="mono mt-3 rounded-lg border p-2 text-xs" style={{ borderColor: "var(--mc-red)", color: "var(--mc-red)" }}>
            {run.error_summary}
          </div>
        ) : null}
        {run.children.length > 0 ? (
          <div className="mt-3 text-xs">
            <Muted>subagents:</Muted>{" "}
            {run.children.map((child) => (
              <Link
                key={child.id}
                href={`/run/?id=${child.id}`}
                className="mono mr-2"
                style={{ color: "var(--mc-blue)" }}
              >
                {child.external_id}
              </Link>
            ))}
          </div>
        ) : null}
      </section>

      <section className="card p-4">
        <div className="mb-2 flex items-center justify-between">
          <h2 className="text-sm font-semibold uppercase tracking-wider" style={{ color: "var(--mc-muted)" }}>
            Events ({events.length}
            {run.event_count > events.length ? ` of ${run.event_count}` : ""})
          </h2>
          <ConnectionBanner state={state} lastSync={lastSync} />
        </div>
        <div className="flex flex-col">
          {events.map((event) => (
            <EventRow key={event.id} event={event} />
          ))}
          {events.length === 0 ? (
            <Muted>
              <span className="text-sm">No events recorded.</span>
            </Muted>
          ) : null}
        </div>
      </section>
    </main>
  );
}
