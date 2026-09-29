"use client";

import { Section, ErrorNotice, Loading } from "@/components/ui";
import { useApi } from "@/lib/useApi";

type J = Record<string, any>;
const STATUS_CLASS: Record<string, string> = { OK: "pill pill-ok", WATCH_ONLY: "pill pill-warn", NOT_CONFIGURED: "pill pill-off",
  MISMATCH: "pill pill-danger", INVALID: "pill pill-danger" };

/** The EVM half of the trading wallet (BSC + Robinhood Chain): public address
 * and on-chain native balances, next to the EVM paper accounts. */
export default function EvmWalletPanel() {
  const { data, error, loading } = useApi<J>("/api/evm/wallet", undefined, { refreshMs: 60000 });
  const live = data?.live;
  return (
    <Section title="EVM wallet (BSC and Robinhood Chain)">
      <ErrorNotice error={error} />
      {loading && !data && <Loading />}
      {live && (
        <>
          <p className="small">
            <span className={STATUS_CLASS[live.status] ?? "pill pill-off"}>{live.status.replace("_", " ")}</span>{" "}
            {live.address ? <span className="mono">{live.address}</span> : null} <span className="muted">{live.detail}</span>
          </p>
          <div className="stat-grid">
            {Object.entries((live.balances ?? {}) as Record<string, J>).map(([c, b]) => (
              <div className="stat" key={c}>
                <div className="stat-label">{c === "bsc" ? "BSC" : "Robinhood Chain"} (LIVE, on chain)</div>
                <div className="stat-value">{b.balance !== null ? `${Number(b.balance).toFixed(6)} ${live.currency[c]}` : <span className="muted">unavailable</span>}</div>
                {b.detail ? <div className="form-hint">{b.detail}</div> : null}
              </div>
            ))}
            {(data.paper as J[]).map((a) => (
              <div className="stat" key={a.name}>
                <div className="stat-label">{a.name} (PAPER)</div>
                <div className="stat-value">{Number(a.cash).toFixed(6)} {a.currency}</div>
                <div className="form-hint">started with {Number(a.starting).toFixed(4)}</div>
              </div>
            ))}
          </div>
          <p className="muted small">{live.execution}</p>
        </>
      )}
    </Section>
  );
}
