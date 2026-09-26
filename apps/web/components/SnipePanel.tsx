"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { ErrorNotice, Section } from "@/components/ui";
import { apiGet, apiPut, ApiError } from "@/lib/api";
import type { ModesOut, SettingsOut } from "@/lib/types";

type Live = { settings: Record<string, string>; limits: Record<string, [string, string]> };
type Strategy = { name: string; config: Record<string, unknown>; mode?: string };

const SNIPE_MODES = ["fresh_launch", "migration", "both", "off"] as const;
type SnipeMode = (typeof SNIPE_MODES)[number];

// Name filters and the dev-token ceiling are safety settings, saved in both
// Pump.fun scopes so fresh and migrated tokens are filtered alike.
const SAFETY_KEYS = ["min_name_length", "skip_duplicate_names", "ascii_names_only", "max_creator_tokens_created",
  "cooldown_after_loss_seconds"] as const;
const SCOPES = ["solana_fresh", "solana_migration"];

function snipeModeOf(m: ModesOut): SnipeMode {
  const fresh = (m.strategies.solana_fresh ?? "OFF") !== "OFF";
  const mig = (m.strategies.solana_migration ?? "OFF") !== "OFF";
  return fresh && mig ? "both" : fresh ? "fresh_launch" : mig ? "migration" : "off";
}

/** The operator's sniper configuration keys, mapped onto the controls that
 * already run the engine: strategy modes, strategy config, live execution
 * settings and the versioned safety settings. Nothing here bypasses the
 * safety gate; URLs and keys stay on the server. */
export default function SnipePanel({ modes, onModes }: { modes: ModesOut; onModes: (m: ModesOut) => void }) {
  const [snipe, setSnipe] = useState<SnipeMode>(snipeModeOf(modes));
  const [autoBuy, setAutoBuy] = useState(modes.strategies.solana_fresh === "AUTO" || modes.strategies.solana_migration === "AUTO");
  const [buyAmount, setBuyAmount] = useState("");
  const [live, setLive] = useState<Live | null>(null);
  const [slippage, setSlippage] = useState("");
  const [fee, setFee] = useState("");
  const [safety, setSafety] = useState<Record<string, string>>({});
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    (async () => {
      try {
        const [l, fresh, s] = await Promise.all([
          apiGet<Live>("/api/live/settings"),
          apiGet<Strategy>("/api/strategies/solana_fresh"),
          apiGet<SettingsOut>("/api/control/settings/solana_fresh"),
        ]);
        setLive(l);
        setSlippage(l.settings.entry_slippage_pct ?? "");
        setFee(l.settings.priority_fee_sol ?? "");
        const size = fresh.config.manual_position_size_sol;
        setBuyAmount(size === null || size === undefined ? "" : String(size));
        setSafety(Object.fromEntries(SAFETY_KEYS.map((k) => [k, String(s.effective[k] ?? "")])));
      } catch (e) {
        setErr(e instanceof ApiError ? e.message : String(e));
      }
    })();
  }, []);

  async function apply(what: string, fn: () => Promise<void>) {
    setBusy(true);
    setErr(null);
    setMsg(null);
    try {
      await fn();
      setMsg(`${what} saved.`);
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  const applyModes = () =>
    apply("Snipe mode", async () => {
      const on = autoBuy ? "AUTO" : "MANUAL";
      const want: Record<string, string> = {
        solana_fresh: snipe === "fresh_launch" || snipe === "both" ? on : "OFF",
        solana_migration: snipe === "migration" || snipe === "both" ? on : "OFF",
      };
      let m = modes;
      for (const [strategy, mode] of Object.entries(want)) {
        if (modes.strategies[strategy] !== mode) m = await apiPut<ModesOut>(`/api/control/modes/strategy/${strategy}`, { mode });
      }
      onModes(m);
    });

  const applyBuy = () =>
    apply("Buy amount", async () => {
      const value = buyAmount.trim() === "" ? null : buyAmount.trim();
      for (const name of SCOPES) await apiPut(`/api/strategies/${name}/config`, { config: { manual_position_size_sol: value } });
    });

  const applyExecution = () =>
    apply("Slippage and priority fee", async () => {
      const l = await apiPut<Live>("/api/live/settings", { entry_slippage_pct: slippage, priority_fee_sol: fee });
      setLive(l);
    });

  const applySafety = () =>
    apply("Filters", async () => {
      for (const scope of SCOPES) {
        const cur = await apiGet<SettingsOut>(`/api/control/settings/${scope}`);
        const payload: Record<string, unknown> = { ...cur.effective };
        for (const k of SAFETY_KEYS) {
          const orig = cur.effective[k];
          const v = safety[k];
          payload[k] = typeof orig === "boolean" ? v === "true" : typeof orig === "number" ? Number(v) : v;
        }
        await apiPut(`/api/control/settings/${scope}`, { settings: payload, note: "Settings → Pump.fun snipe settings" });
      }
    });

  const field = (k: string, label: string, help: string) => (
    <div className="form-row" key={k}>
      <label htmlFor={`snipe-${k}`}>{label}</label>
      {k === "skip_duplicate_names" || k === "ascii_names_only" ? (
        <select id={`snipe-${k}`} value={safety[k] ?? ""} onChange={(e) => setSafety({ ...safety, [k]: e.target.value })}>
          <option value="true">ON</option>
          <option value="false">OFF</option>
        </select>
      ) : (
        <input id={`snipe-${k}`} value={safety[k] ?? ""} onChange={(e) => setSafety({ ...safety, [k]: e.target.value })} />
      )}
      <span className="form-hint">{help}</span>
    </div>
  );

  return (
    <Section title="Pump.fun snipe settings">
      <ErrorNotice error={err} />
      {msg && <div className="success">{msg}</div>}
      <div className="notice">
        These map the sniper configuration keys onto the engine that already runs. Every entry still passes the full safety
        gate (holders, creator, liquidity, sellability, tax, slippage, impact, execution). The global mode decides PAPER or LIVE.
      </div>

      <div className="form-grid">
        <div className="form-row">
          <label htmlFor="snipe-mode">SNIPE_MODE</label>
          <select id="snipe-mode" value={snipe} onChange={(e) => setSnipe(e.target.value as SnipeMode)}>
            {SNIPE_MODES.map((m) => <option key={m} value={m}>{m}</option>)}
          </select>
          <span className="form-hint">fresh_launch = Fresh Tokens strategy, migration = Migrated Tokens (PumpSwap), both, or off.</span>
        </div>
        <div className="form-row">
          <label htmlFor="snipe-auto">AUTO_BUY</label>
          <select id="snipe-auto" value={autoBuy ? "true" : "false"} onChange={(e) => setAutoBuy(e.target.value === "true")}>
            <option value="true">ON — AUTO</option>
            <option value="false">OFF — MANUAL (every entry waits for approval)</option>
          </select>
          <span className="form-hint">Current: fresh {modes.strategies.solana_fresh ?? "—"} · migration {modes.strategies.solana_migration ?? "—"}</span>
        </div>
      </div>
      <div className="btn-row"><button className="btn btn-sm" disabled={busy} onClick={applyModes}>Apply snipe mode</button></div>

      <div className="form-grid">
        <div className="form-row">
          <label htmlFor="snipe-buy">BUY_AMOUNT_SOL</label>
          <input id="snipe-buy" value={buyAmount} placeholder="automatic" onChange={(e) => setBuyAmount(e.target.value)} />
          <span className="form-hint">
            Requested size per entry, both Pump.fun strategies. Reduced automatically when risk per trade, max position, balance
            or the SOL reserve require it. Empty = sized automatically from risk.
          </span>
        </div>
      </div>
      <div className="btn-row"><button className="btn btn-sm" disabled={busy} onClick={applyBuy}>Save buy amount</button></div>

      <div className="form-grid">
        <div className="form-row">
          <label htmlFor="snipe-slip">SLIPPAGE (%)</label>
          <input id="snipe-slip" value={slippage} onChange={(e) => setSlippage(e.target.value)} />
          <span className="form-hint">Live entry slippage. Allowed {live?.limits.entry_slippage_pct?.join("–") ?? "…"} %.</span>
        </div>
        <div className="form-row">
          <label htmlFor="snipe-fee">PRIORITY_FEE (SOL)</label>
          <input id="snipe-fee" value={fee} onChange={(e) => setFee(e.target.value)} />
          <span className="form-hint">Live priority fee. Allowed {live?.limits.priority_fee_sol?.join("–") ?? "…"} SOL.</span>
        </div>
      </div>
      <div className="btn-row"><button className="btn btn-sm" disabled={busy} onClick={applyExecution}>Save slippage &amp; fee</button></div>

      <div className="form-grid">
        {field("min_name_length", "MIN_NAME_LENGTH", "Reject names shorter than this. 0 = off.")}
        {field("skip_duplicate_names", "SKIP_DUPLICATE_NAMES", "Reject a launch reusing a name launched in the last 24 h.")}
        {field("ascii_names_only", "ASCII_NAMES_ONLY", "Reject names/symbols with non-ASCII characters.")}
        {field("max_creator_tokens_created", "MAX_DEV_TOKENS", "Creator wallets with this many pump.fun tokens or more need approval (serial-launcher indicator). 0 = off.")}
        {field("cooldown_after_loss_seconds", "COOLDOWN_SECONDS", "Pause after a losing trade. There is no pause after every buy; per-token re-evaluation is paced by the engine.")}
      </div>
      <div className="btn-row"><button className="btn btn-sm" disabled={busy} onClick={applySafety}>Save filters (fresh + migration)</button></div>

      <table className="data-table">
        <thead><tr><th>Key</th><th>Where / status</th></tr></thead>
        <tbody>
          <tr><td>LAUNCHPAD</td><td>pumpfun: supported. meteora / both: <b>NOT COMPLETE</b> — no Meteora DBC stream, curve model or execution route exists; the option is not offered.</td></tr>
          <tr><td>BUY_POOLS</td><td>Automatic by lifecycle: bonding curve → <code>pump</code>, migrated → <code>pump-amm</code> (PumpSwap). Other pools are not traded.</td></tr>
          <tr><td>BLACKLISTED_WORDS / BLACKLISTED_EXACT</td><td><Link className="link" href="/dashboard/rules">Rules</Link> → “Add scam-name preset” (substring and exact rules, each editable).</td></tr>
          <tr><td>MIN_CREATOR_TOKENS_CREATED and action</td><td><Link className="link" href="/dashboard/risk-settings">Risk settings</Link> → Creator risk.</td></tr>
          <tr><td>MIN_MIGRATED_LIQUIDITY_USD</td><td><Link className="link" href="/dashboard/risk-settings">Risk settings</Link> → Migrated liquidity (scope solana_migration).</td></tr>
          <tr><td>WS_URL / TRADE_URL</td><td>Server <code>.env</code> (SOLANA_WS_URL / Helius); the PumpPortal trade-local endpoint is fixed in code. Not editable here, by design.</td></tr>
          <tr><td>JUPITER quote / price URLs</td><td>Fixed in code: lite-api.jup.ag without a key, api.jup.ag with JUPITER_API_KEY in <code>.env</code>.</td></tr>
          <tr><td>METEORA_DBC_PROGRAM</td><td>Not used (Meteora not implemented).</td></tr>
          <tr><td>SOL_MINT</td><td>Constant (wrapped SOL, So111…112); not a setting.</td></tr>
        </tbody>
      </table>
    </Section>
  );
}
