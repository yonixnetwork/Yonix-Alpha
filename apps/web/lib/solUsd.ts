"use client";

import { useEffect, useState } from "react";
import { apiGet } from "@/lib/api";

export interface SolUsd { price: number | null; source: string | null; at: string | null }

const REFRESH_MS = 60_000;
let cached: { value: SolUsd; fetchedAt: number } | null = null;
let inflight: Promise<SolUsd> | null = null;

async function fetchSolUsd(): Promise<SolUsd> {
  if (cached && Date.now() - cached.fetchedAt < REFRESH_MS / 2) return cached.value;
  inflight ??= apiGet<{ price: string | null; source: string | null; at: string | null }>("/api/tokens/sol-usd")
    .then((r) => ({ price: r.price === null ? null : Number(r.price), source: r.source, at: r.at }))
    .catch(() => ({ price: null, source: null, at: null }))
    .then((value) => {
      cached = { value, fetchedAt: Date.now() };
      inflight = null;
      return value;
    });
  return inflight;
}

/** The backend's fresh SOL/USD rate (one request per page, shared by every
 * caller, refreshed every minute). price is null when the backend has no
 * fresh rate: show SOL only, never a guessed USD value. */
export function useSolUsd(): SolUsd {
  const [value, setValue] = useState<SolUsd>(cached?.value ?? { price: null, source: null, at: null });
  useEffect(() => {
    let alive = true;
    const load = () => fetchSolUsd().then((v) => alive && setValue(v));
    load();
    const t = setInterval(load, REFRESH_MS);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, []);
  return value;
}
