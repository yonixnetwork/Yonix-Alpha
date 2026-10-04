"use client";

import { useState } from "react";
import { ErrorNotice, Loading, Section } from "@/components/ui";
import { apiPut } from "@/lib/api";
import { formatDate } from "@/lib/format";
import { useApi } from "@/lib/useApi";

type J = Record<string, any>;

const STATE_CLASS: Record<string, string> = {
  CONNECTED: "pill pill-ok", CONNECTING: "pill pill-warn", RECONNECTING: "pill pill-warn", LIMITED: "pill pill-danger",
  REFUSED: "pill pill-danger", WRONG_CHAIN: "pill pill-danger", NOT_CONFIGURED: "pill pill-off", DISABLED: "pill pill-off",
};
const SOURCE: Record<string, string> = { sequencer_feed: "Sequencer feed", pending_tx: "Pending transactions" };
const CHAIN: Record<string, string> = { robinhood: "Robinhood Chain", bsc: "BSC" };
const dash = (v: any, suffix = "") => (v === null || v === undefined ? <span className="muted">—</span> : `${Number(v).toLocaleString()}${suffix}`);

function StreamSettings({ data, reload }: { data: J; reload: () => void }) {
  const [draft, setDraft] = useState<J | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const v = draft ?? data.settings;
  const set = (k: string, val: any) => setDraft({ ...v, [k]: val });
  const save = async () => {
    try { const r: J = await apiPut("/api/evm/stream-settings", v); setMsg(r.note ?? "Saved."); setDraft(null); reload(); }
    catch (e) { setMsg(String((e as Error).message)); }
  };
  return (
    <div>
      <div className="form-grid">
        <label className="small"><input type="checkbox" checked={!!v.robinhood_feed_enabled}
          onChange={(e) => set("robinhood_feed_enabled", e.target.checked)} /> Robinhood Chain sequencer feed</label>
        <label className="small"><input type="checkbox" checked={!!v.verify_feed_signatures}
          onChange={(e) => set("verify_feed_signatures", e.target.checked)} /> Check the sequencer signature on every feed message</label>
        <label className="small"><input type="checkbox" checked={!!v.bsc_pending_enabled}
          onChange={(e) => set("bsc_pending_enabled", e.target.checked)} /> BSC pending transactions (needs a WSS endpoint)</label>
        {[["robinhood_feed_url", "Feed URL (wss://)"], ["robinhood_delayed_url", "Delayed feed URL (wss://)"]].map(([k, label]) => (
          <label key={k} className="small">{label}
            <input style={{ display: "block", width: "100%" }} value={v[k]} onChange={(e) => set(k, e.target.value)} /></label>))}
        {[["fallback_after_failures", "Use the delayed feed after N failed connections"],
          ["primary_retry_minutes", "Retry the primary feed every (minutes)"],
          ["recover_budget_per_s", "Sender recoveries per second (unwatched transactions)"]].map(([k, label]) => (
          <label key={k} className="small">{label}
            <input style={{ display: "block" }} value={v[k]} inputMode="numeric" onChange={(e) => set(k, e.target.value)} /></label>))}
      </div>
      <div className="btn-row"><button className="btn btn-sm" disabled={!draft} onClick={save}>Save stream settings</button>
        <button className="btn btn-ghost btn-sm" onClick={() => setDraft({ ...data.defaults })}>Reset to defaults</button></div>
      {msg && <p className="small">{msg}</p>}
    </div>
  );
}

const rate = (r: number | null | undefined) => (r === null || r === undefined ? <span className="muted">NOT AVAILABLE</span> : `${(r * 100).toFixed(1)} %`);

/** Master §68: the streams and the launchpad logs checked against each other, last 24 hours. */
function CrossCheck({ chains }: { chains: Record<string, J> }) {
  return (
    <>
      <div className="section-title">Streams vs logs (last 24 h)</div>
      <div className="table-scroll">
        <table className="data-table">
          <thead><tr><th>Chain</th><th>Launches in the logs: seen first by a stream</th><th>Trades in the logs: seen first</th>
            <th>Lead median / p95</th><th>Stream launchpad transactions found in the logs (after 5 min)</th><th>Not in the logs</th></tr></thead>
          <tbody>{Object.entries(chains).map(([chain, c]) => {
            const x: J | undefined = c.crosscheck;
            if (!x) return null;
            const l = x.launches_seen_first_by_stream, t = x.trades_seen_first_by_stream, st = x.stream_txs_in_logs;
            return (
              <tr key={chain}>
                <td>{CHAIN[chain] ?? chain}</td>
                <td>{l.seen_first} of {l.logged} ({rate(l.rate)})</td>
                <td>{t.seen_first} of {t.logged} ({rate(t.rate)})</td>
                <td>{l.lead_s_median === null ? <span className="muted">no samples</span> : `${l.lead_s_median} s`}
                  {" / "}{l.lead_s_p95 === null ? <span className="muted" title="needs 20 samples">—</span> : `${l.lead_s_p95} s`}</td>
                <td>{st.in_logs} of {st.checked} ({rate(st.rate)})</td>
                <td>{st.not_in_logs}</td>
              </tr>);
          })}</tbody>
        </table>
      </div>
      <p className="muted small">{Object.values(chains)[0]?.crosscheck?.note}</p>
    </>
  );
}

/** Real-time transaction streams (master §9, §13): state, sequence health, delay and copy-trade lead. */
export default function EvmStreams() {
  const { data, error, loading, reload } = useApi<J>("/api/evm/streams", undefined, { refreshMs: 15000 });
  const [open, setOpen] = useState(false);
  return (
    <Section title="Transaction streams — Robinhood sequencer feed and BSC pending transactions">
      <ErrorNotice error={error} />
      {loading && !data && <Loading />}
      {data && (
        <>
          <p className="muted small">{data.note}</p>
          <div className="table-scroll">
            <table className="data-table">
              <thead><tr><th>Chain</th><th>Stream</th><th>State</th><th>Endpoint</th><th>Messages / transactions</th>
                <th>Sequence gaps (missing)</th><th>Duplicates / reorgs</th><th>Delay median / p95</th><th>Matched (targets / launchpads)</th>
                <th>Reconnects</th><th>Last message</th></tr></thead>
              <tbody>{Object.entries(data.chains as Record<string, J>).flatMap(([chain, c]) => (c.expected as string[]).map((src) => {
                const s: J | undefined = c.streams[src];
                if (!s) {
                  return (<tr key={chain + src}><td>{CHAIN[chain] ?? chain}</td><td>{SOURCE[src] ?? src}</td>
                    <td><span className="pill pill-off">NOT REPORTING</span></td>
                    <td colSpan={8} className="muted small">data-evm has not published this stream in the last 3 minutes (not deployed yet, or the service is down)</td></tr>);
                }
                return (
                  <tr key={chain + src}>
                    <td>{CHAIN[chain] ?? chain}</td>
                    <td>{SOURCE[src] ?? src}{s.fallback_active ? <div className="small neg">delayed feed in use</div> : null}</td>
                    <td><span className={STATE_CLASS[s.state] ?? "pill pill-off"}>{String(s.state).replaceAll("_", " ")}</span>
                      {s.detail ? <div className="small muted">{s.detail}</div> : null}
                      {src === "sequencer_feed" && s.verification ? <div className="small muted">signatures: {String(s.verification).replaceAll("_", " ").toLowerCase()}
                        {s.unverified ? <span className="neg"> ({s.unverified} dropped)</span> : null}</div> : null}</td>
                    <td className="small">{s.url ?? "—"}</td>
                    <td>{dash(s.messages)} / {dash(s.transactions)}</td>
                    <td>{src === "sequencer_feed" ? <>{dash(s.gaps)} ({dash(s.missing_messages)})</> : <span className="muted">n/a</span>}</td>
                    <td>{src === "sequencer_feed" ? <>{dash(s.duplicates)}{s.reorgs ? <div className="small neg">reorgs: {s.reorgs}</div> : null}
                      {s.backlog_skipped ? <div className="small muted" title="history replayed on the first connection: sequenced, not matched or timed">backlog skipped: {Number(s.backlog_skipped).toLocaleString()}</div> : null}</> : <span className="muted">n/a</span>}</td>
                    <td>{src === "sequencer_feed" ? <>{dash(s.delay_s_median, " s")} / {dash(s.delay_s_p95, " s")}</> : <span className="muted">n/a</span>}</td>
                    <td>{dash(s.matched_copy_targets)} / {dash(s.matched_launchpads)}</td>
                    <td>{dash(s.reconnects)}{s.last_error ? <div className="small muted" title={s.last_error}>last error: {String(s.last_error).slice(0, 60)}</div> : null}</td>
                    <td className="small">{s.last_message_at ? formatDate(s.last_message_at) : "none yet"}</td>
                  </tr>);
              }))}</tbody>
            </table>
          </div>
          <div className="table-scroll">
            <table className="data-table">
              <thead><tr><th>Chain</th><th>Copy-target trades (24 h)</th><th>Seen on a stream first</th><th>By stream</th>
                <th>Lead over confirmed detection: median / p95</th></tr></thead>
              <tbody>{Object.entries(data.chains as Record<string, J>).map(([chain, c]) => (
                <tr key={chain}>
                  <td>{CHAIN[chain] ?? chain}</td>
                  <td>{c.copy_events_24h}</td>
                  <td>{c.copy_events_24h ? c.seen_on_stream_24h : <span className="muted">no copy-target trades yet</span>}</td>
                  <td className="small">{Object.entries(c.by_source_24h as Record<string, number>).map(([k, n]) => `${SOURCE[k] ?? k.replaceAll("_", " ")}: ${n}`).join(", ") || "—"}</td>
                  <td>{c.lead_ms_median === null ? <span className="muted">no samples</span> : <>{dash(c.lead_ms_median, " ms")} / {c.lead_ms_p95 === null ? <span className="muted" title="needs 20 samples">—</span> : dash(c.lead_ms_p95, " ms")}</>}</td>
                </tr>))}</tbody>
            </table>
          </div>
          <CrossCheck chains={data.chains} />
          <button className="btn btn-ghost btn-sm" onClick={() => setOpen(!open)}>{open ? "Hide stream settings" : "Stream settings"}</button>
          {open && <StreamSettings data={data} reload={reload} />}
        </>
      )}
    </Section>
  );
}
