"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { apiGet, apiPut, ApiError } from "@/lib/api";
import { formatDate } from "@/lib/format";
import type { SettingsOut, SettingsVersionOut } from "@/lib/types";

const SCOPES = ["GLOBAL", "solana_fresh", "solana_migration", "solana_momentum", "binance_futures", "bybit_futures", "hyperliquid_perps"];

type Value = string | number | boolean | string[] | null;

const SECTIONS: { title: string; match: (k: string) => boolean }[] = [
  { title: "Account & sizing", match: (k) => /risk_per_trade|position_size|open_positions|daily_loss|exposure|cooldown/.test(k) },
  { title: "Liquidity & execution", match: (k) => /liquidity|pool_fraction|impact|round_trip|slippage|data_age/.test(k) },
  { title: "Token & holders", match: (k) => /authority|transfer_fee|top1|top10|creator|holder/.test(k) },
  { title: "Trading activity", match: (k) => /buyers|top3|trades_in_window/.test(k) },
  { title: "Stops, targets & trailing", match: (k) => /stop|tp_|trailing/.test(k) },
  { title: "Decision policy", match: () => true },
];

const HELP: Record<string, string> = {
  risk_per_trade_pct: "Loss at the stop, after all costs, as a fraction of equity (0.01 = 1%).",
  max_pool_fraction: "Largest position as a fraction of pool liquidity.",
  max_round_trip_loss_bps: "Refuse if buying and immediately selling would lose more than this.",
  min_stop_pct: "Floor on the automatic stop distance. Must exceed round-trip costs.",
  max_risk_level_for_auto: "Findings above this level need operator approval.",
  wait_for_liquidity_max_age_seconds: "Young pools below min liquidity WAIT instead of NO_TRADE for this long.",
};

function toInput(v: Value): string {
  if (Array.isArray(v)) return v.join(", ");
  if (v === null) return "";
  return String(v);
}

function fromInput(original: Value, text: string): Value {
  if (Array.isArray(original)) return text.split(",").map((x) => x.trim()).filter(Boolean);
  if (typeof original === "boolean") return text === "true";
  if (typeof original === "number") return text === "" ? original : Number(text);
  if (original === null) return text === "" ? null : text;
  return text;
}

export default function RiskSettingsPage() {
  const router = useRouter();
  const [scope, setScope] = useState("solana_fresh");
  const [data, setData] = useState<SettingsOut | null>(null);
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [note, setNote] = useState("");
  const [history, setHistory] = useState<SettingsVersionOut[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const [s, h] = await Promise.all([
        apiGet<SettingsOut>(`/api/control/settings/${scope}`),
        apiGet<SettingsVersionOut[]>(`/api/control/settings/${scope}/history`),
      ]);
      setData(s);
      setHistory(h);
      setDraft(Object.fromEntries(Object.entries(s.effective).map(([k, v]) => [k, toInput(v)])));
      setError(null);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) return router.replace("/login");
      setError("Failed to load settings.");
    }
  }, [scope, router]);

  useEffect(() => {
    setSaved(null);
    load();
  }, [load]);

  const changed = useMemo(() => {
    if (!data) return new Set<string>();
    return new Set(Object.keys(draft).filter((k) => draft[k] !== toInput(data.effective[k])));
  }, [draft, data]);

  const grouped = useMemo(() => {
    if (!data) return [];
    const keys = Object.keys(data.effective);
    const used = new Set<string>();
    return SECTIONS.map((sec) => {
      const ks = keys.filter((k) => !used.has(k) && sec.match(k));
      ks.forEach((k) => used.add(k));
      return { title: sec.title, keys: ks };
    }).filter((g) => g.keys.length);
  }, [data]);

  async function save() {
    if (!data) return;
    setBusy(true);
    setError(null);
    setSaved(null);
    try {
      // Always the complete set: a key left out would fall back to the code
      // default on the server rather than keep its current value.
      const payload = Object.fromEntries(Object.entries(data.effective).map(([k, v]) => [k, fromInput(v, draft[k] ?? toInput(v))]));
      const res = await apiPut<{ version: SettingsVersionOut; clamp_notes: string[] }>(`/api/control/settings/${scope}`, {
        settings: payload,
        note: note || null,
      });
      setSaved(
        `Saved as version ${res.version.version}.` +
          (res.clamp_notes.length ? ` Bounded by hard limits: ${res.clamp_notes.join("; ")}` : ""),
      );
      setNote("");
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Save failed.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      <div className="page-header">
        <div className="page-title">Risk Settings</div>
      </div>
      <p className="muted">
        Runtime thresholds used by the safety gate, stored in the database and versioned. Hard limits cannot be
        exceeded from here. Secrets and API keys are never stored here; they stay in the server&apos;s .env.
      </p>

      <div className="filters">
        <select value={scope} onChange={(e) => setScope(e.target.value)}>
          {SCOPES.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
        {data && (
          <span className="muted">
            Effective source: {data.source.scope}
            {data.source.version ? ` v${data.source.version}` : " (code defaults)"}
          </span>
        )}
      </div>

      {data?.source.errors && (
        <div className="notice notice-danger">
          The stored settings failed validation and are being ignored in favour of defaults: {data.source.errors.join("; ")}
        </div>
      )}
      {error && <div className="error">{error}</div>}
      {saved && <div className="success">{saved}</div>}

      {data &&
        grouped.map((g) => (
          <div key={g.title}>
            <div className="section-title">{g.title}</div>
            <div className="form-grid">
              {g.keys.map((k) => {
                const original = data.effective[k];
                const limit = data.hard_limits[k];
                return (
                  <div className="form-row" key={k}>
                    <label htmlFor={k}>{k}</label>
                    {typeof original === "boolean" ? (
                      <select id={k} value={draft[k]} onChange={(e) => setDraft({ ...draft, [k]: e.target.value })}>
                        <option value="true">true</option>
                        <option value="false">false</option>
                      </select>
                    ) : (
                      <input
                        id={k}
                        className={changed.has(k) ? "changed" : ""}
                        value={draft[k] ?? ""}
                        onChange={(e) => setDraft({ ...draft, [k]: e.target.value })}
                      />
                    )}
                    <span className="form-hint">
                      default {toInput(data.defaults[k]) || "none"}
                      {limit ? ` · hard ${limit.kind} ${limit.bound}` : ""}
                      {HELP[k] ? ` · ${HELP[k]}` : ""}
                    </span>
                  </div>
                );
              })}
            </div>
          </div>
        ))}

      {data && (
        <div className="inline-form" style={{ marginTop: 20 }}>
          <input placeholder="Change note (optional)" value={note} onChange={(e) => setNote(e.target.value)} maxLength={256} />
          <button className="btn" disabled={busy || changed.size === 0} onClick={save}>
            {busy ? "Saving…" : `Save ${changed.size} change${changed.size === 1 ? "" : "s"}`}
          </button>
          <button className="btn btn-ghost" disabled={changed.size === 0} onClick={load}>
            Discard
          </button>
        </div>
      )}

      <div className="section-title">History</div>
      {history.length === 0 ? (
        <div className="muted">No saved versions for this scope; code defaults apply.</div>
      ) : (
        <table className="data-table">
          <thead>
            <tr>
              <th>Version</th>
              <th>Saved</th>
              <th>Note</th>
            </tr>
          </thead>
          <tbody>
            {history.map((h) => (
              <tr key={h.id}>
                <td>v{h.version}</td>
                <td>{formatDate(h.created_at)}</td>
                <td>{h.note ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
