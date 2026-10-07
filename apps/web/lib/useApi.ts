"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { apiGet, ApiError } from "@/lib/api";
import { useEvents } from "@/lib/events";

type Params = Record<string, string | number | boolean | undefined>;

interface Options {
  /** Poll interval (ms) as a fallback when no realtime event arrives. */
  refreshMs?: number;
  /** Realtime event types that should trigger a reload. */
  reloadOn?: string[];
  /** Skip loading (e.g. until a required parameter is known). */
  skip?: boolean;
}

/** GET `path` with loading/error state, a 401 redirect to login, optional
 * polling, and reloads on realtime events (debounced so a burst of events
 * causes one request). */
export function useApi<T>(path: string | null, params?: Params, opts: Options = {}) {
  const router = useRouter();
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const paramsKey = JSON.stringify(params ?? {});
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  // One request at a time per panel: a poll that fires while the previous
  // request is still running is skipped instead of stacking a second one on
  // a slow endpoint (audit 2026-10-07: stacked polls kept the API's
  // database connections busy until unrelated pages timed out too).
  // Keyed by path + params, so changing a filter still loads at once.
  const inFlight = useRef<string | null>(null);
  const latest = useRef<string | null>(null);  // an older filter's late answer never replaces the newer one

  const load = useCallback(async () => {
    const key = `${path}?${paramsKey}`;
    if (!path || opts.skip || inFlight.current === key) return;
    inFlight.current = key;
    latest.current = key;
    try {
      const result = await apiGet<T>(path, JSON.parse(paramsKey));
      if (latest.current !== key) return;
      setData(result);
      setError(null);
    } catch (err) {
      if (latest.current !== key) return;
      if (err instanceof ApiError && err.status === 401) {
        router.replace("/login");
        return;
      }
      setError(err instanceof ApiError ? err.message : "Failed to load data.");
    } finally {
      if (inFlight.current === key) inFlight.current = null;
      setLoading(false);
    }
  }, [path, paramsKey, opts.skip, router]);

  useEffect(() => {
    load();
    if (!opts.refreshMs) return;
    const t = setInterval(load, opts.refreshMs);
    return () => clearInterval(t);
  }, [load, opts.refreshMs]);

  useEvents(opts.reloadOn ?? [], () => {
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(load, 400);
  });

  return { data, error, loading, reload: load, setData };
}
