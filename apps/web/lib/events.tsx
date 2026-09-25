"use client";

import { createContext, useContext, useEffect, useRef, useState } from "react";
import { getAccessToken } from "@/lib/api";

export type LiveStatus = "connecting" | "live" | "offline";
export interface LiveEvent {
  type: string;
  data: Record<string, unknown>;
  source?: string;
  at?: string;
}
type Listener = (e: LiveEvent) => void;

interface Ctx {
  status: LiveStatus;
  lastEventAt: string | null;
  subscribe: (fn: Listener) => () => void;
}

const EventsContext = createContext<Ctx>({ status: "offline", lastEventAt: null, subscribe: () => () => undefined });

function wsUrl(): string {
  const base = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
  return base.replace(/^http/, "ws").replace(/\/$/, "") + "/api/ws";
}

/** One authenticated WebSocket per tab to /api/ws. The token is sent as the
 * first message (never in the URL, which proxies log). Reconnects with
 * capped backoff; pages fall back to polling while it is down. */
export function EventsProvider({ children }: { children: React.ReactNode }) {
  const [status, setStatus] = useState<LiveStatus>("connecting");
  const [lastEventAt, setLastEventAt] = useState<string | null>(null);
  const listeners = useRef(new Set<Listener>());

  useEffect(() => {
    let ws: WebSocket | null = null;
    let stopped = false;
    let attempt = 0;
    let retry: ReturnType<typeof setTimeout> | null = null;

    const connect = () => {
      const token = getAccessToken();
      if (!token) {
        setStatus("offline");
        return;
      }
      setStatus("connecting");
      try {
        ws = new WebSocket(wsUrl());
      } catch {
        schedule();
        return;
      }
      ws.onopen = () => ws?.send(JSON.stringify({ type: "auth", token }));
      ws.onmessage = (m) => {
        let e: LiveEvent;
        try {
          e = JSON.parse(m.data as string);
        } catch {
          return;
        }
        if (e.type === "ws.ready") {
          attempt = 0;
          setStatus("live");
          return;
        }
        if (e.type === "ws.ping") return;
        setLastEventAt(e.at ?? new Date().toISOString());
        listeners.current.forEach((fn) => fn(e));
      };
      ws.onclose = () => {
        setStatus("offline");
        schedule();
      };
      ws.onerror = () => ws?.close();
    };
    const schedule = () => {
      if (stopped) return;
      attempt += 1;
      retry = setTimeout(connect, Math.min(30000, 1000 * 2 ** Math.min(attempt, 5)));
    };
    connect();
    return () => {
      stopped = true;
      if (retry) clearTimeout(retry);
      ws?.close();
    };
  }, []);

  const subscribe = (fn: Listener) => {
    listeners.current.add(fn);
    return () => {
      listeners.current.delete(fn);
    };
  };

  return <EventsContext.Provider value={{ status, lastEventAt, subscribe }}>{children}</EventsContext.Provider>;
}

export function useLiveStatus() {
  const { status, lastEventAt } = useContext(EventsContext);
  return { status, lastEventAt };
}

/** Calls `fn` for every realtime event whose type is in `types` (or all
 * events when `types` contains "*"). */
export function useEvents(types: string[], fn: Listener) {
  const { subscribe } = useContext(EventsContext);
  const fnRef = useRef(fn);
  fnRef.current = fn;
  const key = types.join(",");
  useEffect(() => {
    if (!key) return;
    const wanted = new Set(key.split(","));
    return subscribe((e) => {
      if (wanted.has("*") || wanted.has(e.type)) fnRef.current(e);
    });
  }, [key, subscribe]);
}
