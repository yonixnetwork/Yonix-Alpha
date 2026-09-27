"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { apiGet, apiPut, ApiError } from "@/lib/api";
import { formatDate } from "@/lib/format";
import type { SettingsOut, SettingsVersionOut } from "@/lib/types";
import RuntimeApply from "@/components/RuntimeApply";

const SCOPES = ["GLOBAL", "solana_fresh", "solana_migration", "solana_momentum", "binance_futures", "bybit_futures", "hyperliquid_perps"];

type Value = string | number | boolean | string[] | null;

// First match wins; `also` shows a key that belongs to another section here
// too (same value, edited in either place).
const SECTIONS: { title: string; match: (k: string) => boolean; also?: string[]; note?: string }[] = [
  { title: "Account & sizing", match: (k) => /risk_per_trade|position_size|open_positions|daily_loss|exposure|cooldown/.test(k) },
  {
    title: "Creator risk",
    match: (k) => /creator/.test(k),
    note: "Creator history counts the pump.fun tokens the launch creator's wallet has created, on chain. Fewer than the minimum is not proof of anything — choose the action. If the count cannot be established it is UNKNOWN, never guessed.",
  },
  {
    title: "Migrated liquidity (PumpSwap)",
    match: (k) => /^migrated_|min_migrated/.test(k),
    also: ["max_entry_impact_bps", "max_exit_impact_bps"],
    note: "Migrated tokens only. Usable liquidity is the pool's SOL side (what a seller can withdraw) in USD. Below the minimum: NO_TRADE. Bonding-curve tokens are never judged by this rule. The price-impact limits apply to every venue.",
  },
  { title: "Token name filters", match: (k) => /name_length|duplicate_names|ascii_names/.test(k),
    note: "Word blacklists (substring / exact / regex) are on the Rules page." },
  { title: "Fresh-token observation", match: (k) => /^fresh_|max_active_candidates|^momentum_/.test(k) },
  { title: "Exit intelligence", match: (k) => /^exit_/.test(k) },
  { title: "Liquidity & execution", match: (k) => /liquidity|pool_fraction|impact|round_trip|slippage|data_age/.test(k) },
  { title: "Token & holders", match: (k) => /authority|transfer_fee|top1|top10|holder|tax|agent|program/.test(k) },
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
  creator_history_check: "Creator History Check ON/OFF.",
  min_creator_tokens_created: "Minimum pump.fun tokens created by the creator wallet (this one included).",
  creator_below_threshold_action: "What happens below the minimum: WARN, REDUCE_SIZE, REQUIRE_MANUAL_APPROVAL or REJECT.",
  creator_history_unknown_action: "What happens when the count cannot be established.",
  max_creator_tokens_created: "Serial-launcher ceiling: at or above this, approval is needed. 0 = off.",
  max_creator_launches_24h: "Launches by this creator seen by the stream in 24 h; above: approval.",
  max_creator_share: "Creator wallet's own holding, share of supply; above: approval.",
  max_creator_linked_buyers: "Early buyers funded by the creator; above: approval.",
  migrated_liquidity_check: "Liquidity Check ON/OFF (migrated tokens).",
  min_migrated_liquidity_usd: "Minimum usable liquidity in USD; below: NO_TRADE.",
  migrated_max_entry_slippage_bps: "Entry price impact + pool fee at the planned size (bps).",
  migrated_max_exit_slippage_bps: "Exit price impact + pool fee at the planned size (bps).",
  max_entry_impact_bps: "Maximum entry price impact (bps), every venue.",
  max_exit_impact_bps: "Maximum exit price impact (bps), every venue.",
  min_name_length: "Reject names shorter than this. 0 = off.",
  skip_duplicate_names: "Reject a launch reusing a name launched in the last 24 h.",
  ascii_names_only: "Reject names/symbols with non-ASCII characters.",
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
      const also = (sec.also ?? []).filter((k) => keys.includes(k) && !ks.includes(k));
      return { title: sec.title, note: sec.note, keys: [...ks, ...also] };
    }).filter((g) => g.keys.length);
  }, [data]);

  const [applyToOverrides, setApplyToOverrides] = useState(true);
  const overriddenBy = data?.scope === "GLOBAL" ? data.scope_links?.overridden_by ?? [] : [];

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
      let also = "";
      if (scope === "GLOBAL" && applyToOverrides && overriddenBy.length && changed.size) {
        // Engines with their own settings ignore GLOBAL: apply the same
        // changed keys to each of them (their other values stay as they are).
        const done: string[] = [];
        for (const o of overriddenBy) {
          const cur = await apiGet<SettingsOut>(`/api/control/settings/${o.scope}`);
          const merged = Object.fromEntries(Object.entries(cur.effective).map(([k, v]) =>
            [k, changed.has(k) ? fromInput(data.effective[k], draft[k]) : v]));
          const r = await apiPut<{ version: SettingsVersionOut }>(`/api/control/settings/${o.scope}`,
            { settings: merged, note: note || "applied from GLOBAL" });
          done.push(`${o.scope} v${r.version.version}`);
        }
        also = ` Also applied to ${done.join(", ")}.`;
      }
      setSaved(
        `Saved as version ${res.version.version}.` + also +
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

      {data?.scope === "GLOBAL" && overriddenBy.length > 0 && (
        <div className="notice notice-danger">
          These engines have their own saved settings and do <b>not</b> use GLOBAL:{" "}
          {overriddenBy.map((o) => `${o.scope} v${o.version}`).join(", ")}. A GLOBAL change alone does not reach them.
          <label style={{ display: "block", marginTop: 6 }}>
            <input type="checkbox" checked={applyToOverrides} onChange={(e) => setApplyToOverrides(e.target.checked)} /> Apply
            the keys I change here to those engines too
          </label>
        </div>
      )}
      {data && data.scope !== "GLOBAL" && data.scope_links?.follows_global === true && (
        <div className="notice">
          {data.scope} currently follows GLOBAL. Saving here gives it its own settings; later GLOBAL changes will then not reach it.
        </div>
      )}
      {data?.source.errors && (
        <div className="notice notice-danger">
          The stored settings failed validation and are being ignored in favour of defaults: {data.source.errors.join("; ")}
        </div>
      )}
      {error && <div className="error">{error}</div>}
      {saved && <div className="notice">{saved} <RuntimeApply inline /></div>}

      {data &&
        grouped.map((g) => (
          <div key={g.title}>
            <div className="section-title">{g.title}</div>
            {g.note && <p className="muted">{g.note}</p>}
            <div className="form-grid">
              {g.keys.map((k) => {
                const original = data.effective[k];
                const limit = data.hard_limits[k];
                const choices = data.enums?.[k];
                const id = `${g.title}-${k}`.replace(/[^a-zA-Z0-9_-]/g, "_");
                return (
                  <div className="form-row" key={k}>
                    <label htmlFor={id}>{k}</label>
                    {typeof original === "boolean" ? (
                      <select id={id} className={changed.has(k) ? "changed" : ""} value={draft[k]}
                        onChange={(e) => setDraft({ ...draft, [k]: e.target.value })}>
                        <option value="true">ON (true)</option>
                        <option value="false">OFF (false)</option>
                      </select>
                    ) : choices ? (
                      <select id={id} className={changed.has(k) ? "changed" : ""} value={draft[k]}
                        onChange={(e) => setDraft({ ...draft, [k]: e.target.value })}>
                        {choices.map((c) => <option key={c} value={c}>{c}</option>)}
                      </select>
                    ) : (
                      <input
                        id={id}
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
