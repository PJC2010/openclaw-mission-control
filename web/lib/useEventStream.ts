"use client";

// SSE tail with resume. EventSource re-sends Last-Event-ID automatically on
// its own reconnects; we also seed `last_event_id` from the REST backlog so
// the stream begins exactly where the fetched history ended (§13).

import { useEffect, useRef, useState } from "react";
import { clock, type EventItem } from "@/lib/api";

const MAX_BUFFER = 300; // virtualization-lite: never render unbounded history

export function useEventStream(params: {
  runId?: string;
  agentId?: string;
  afterId?: number | null;
  enabled?: boolean;
}) {
  const { runId, agentId, afterId, enabled = true } = params;
  const [events, setEvents] = useState<EventItem[]>([]);
  const [state, setState] = useState<"live" | "connecting" | "lost">("connecting");
  const [lastSync, setLastSync] = useState<string | null>(null);
  const seen = useRef<Set<number>>(new Set());

  useEffect(() => {
    if (!enabled || afterId === null) return;
    const query = new URLSearchParams();
    if (runId) query.set("run_id", runId);
    if (agentId) query.set("agent_id", agentId);
    if (afterId !== undefined) query.set("last_event_id", String(afterId));
    const source = new EventSource(`/v1/stream?${query.toString()}`);
    setState("connecting");

    const onRecord = (raw: MessageEvent) => {
      setState("live");
      setLastSync(clock(new Date()));
      try {
        const item = JSON.parse(raw.data) as EventItem;
        if (seen.current.has(item.id)) return;
        seen.current.add(item.id);
        setEvents((current) => [...current, item].slice(-MAX_BUFFER));
      } catch {
        // ignore malformed frame
      }
    };
    // Named events arrive per kind; a catch-all keeps us future-proof.
    for (const kind of ["message", "tool_call", "tool_result", "log", "error", "lifecycle"]) {
      source.addEventListener(kind, onRecord);
    }
    source.onmessage = onRecord;
    source.onopen = () => {
      setState("live");
      setLastSync(clock(new Date()));
    };
    source.onerror = () => setState("lost");
    return () => source.close();
  }, [runId, agentId, afterId, enabled]);

  return { events, state, lastSync };
}
