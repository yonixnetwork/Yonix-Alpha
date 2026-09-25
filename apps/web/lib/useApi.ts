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

  const load = useCallback(async () => {
    if (!path || opts.skip) return;
    try {
      const result = await apiGet<T>(path, JSON.parse(paramsKey));
      setData(result);
      setError(null);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        router.replace("/login");
        return;
      }
      setError(err instanceof ApiError ? err.message : "Failed to load data.");
    } finally {
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
