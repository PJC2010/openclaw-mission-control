"use client";

// §13 network reality: every cached view carries a visible "last synced"
// stamp, and a stale view must never be able to masquerade as live. Past
// the threshold the decision buttons are disabled and the reason is shown.

import { useCallback, useEffect, useState } from "react";

export const STALE_AFTER_MS = 60_000; // §13 default

export function useFreshness(refresh: () => Promise<void>, intervalMs = 10_000) {
  const [lastSync, setLastSync] = useState<number | null>(null);
  const [online, setOnline] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now());

  const run = useCallback(async () => {
    try {
      await refresh();
      setLastSync(Date.now());
      setError(null);
    } catch (exc) {
      setError(String(exc));
    }
  }, [refresh]);

  useEffect(() => {
    run();
    const poll = setInterval(run, intervalMs);
    const tick = setInterval(() => setNow(Date.now()), 1000);
    const goOnline = () => setOnline(true);
    const goOffline = () => setOnline(false);
    setOnline(typeof navigator === "undefined" ? true : navigator.onLine);
    window.addEventListener("online", goOnline);
    window.addEventListener("offline", goOffline);
    return () => {
      clearInterval(poll);
      clearInterval(tick);
      window.removeEventListener("online", goOnline);
      window.removeEventListener("offline", goOffline);
    };
  }, [run, intervalMs]);

  const ageMs = lastSync === null ? Infinity : now - lastSync;
  const stale = !online || ageMs > STALE_AFTER_MS;
  const lastSyncLabel =
    lastSync === null
      ? "never"
      : new Date(lastSync).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

  return { stale, online, lastSync, lastSyncLabel, ageMs, error, refresh: run };
}
