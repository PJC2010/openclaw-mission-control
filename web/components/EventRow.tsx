"use client";

import { type EventItem } from "@/lib/api";

const kindColor: Record<string, string> = {
  tool_call: "var(--mc-blue)",
  tool_result: "var(--mc-muted)",
  message: "var(--mc-text)",
  lifecycle: "var(--mc-amber)",
  error: "var(--mc-red)",
  log: "var(--mc-faint)",
};

function summarize(event: EventItem): string {
  const p = event.payload as Record<string, any>;
  switch (event.kind) {
    case "tool_call":
      return String(
        p.tool_name ?? p.tool_calls?.[0]?.name ?? "tool call"
      );
    case "tool_result":
      return `${p.tool_name ?? "tool"} → ${p.status ?? "result"}`;
    case "lifecycle":
      return String(p.transition ?? "lifecycle");
    case "message": {
      const text = p.content?.text ?? p.direction ?? "";
      return String(text).slice(0, 120) || "message";
    }
    default:
      return event.kind;
  }
}

export function EventRow({ event }: { event: EventItem }) {
  const time = new Date(event.ts).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
  return (
    <details className="group border-b py-1.5 last:border-b-0" style={{ borderColor: "var(--mc-border)" }}>
      <summary className="flex cursor-pointer list-none items-baseline gap-2 text-sm">
        <span className="mono shrink-0 text-xs" style={{ color: "var(--mc-faint)" }}>
          {time}
        </span>
        <span
          className="mono shrink-0 text-xs uppercase"
          style={{ color: kindColor[event.kind] ?? "var(--mc-muted)" }}
        >
          {event.kind}
        </span>
        {/* Agent-supplied text renders as a text node only (S3). */}
        <span className="truncate">{summarize(event)}</span>
      </summary>
      <pre
        className="mono mt-1 max-h-72 overflow-auto rounded-lg border p-2 text-xs leading-relaxed"
        style={{ borderColor: "var(--mc-border)", background: "var(--mc-bg)" }}
      >
        {JSON.stringify(event.payload, null, 2)}
      </pre>
    </details>
  );
}
