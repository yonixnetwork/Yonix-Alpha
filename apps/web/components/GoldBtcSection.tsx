"use client";

import LineChart from "@/components/LineChart";
import { ErrorNotice, Loading, Section, Stat } from "@/components/ui";
import { useApi } from "@/lib/useApi";

interface GoldBtc {
  points: { time: string; btc: number; gold: number; ratio: number }[];
  ratio: number;
  mean: number | null;
  std: number | null;
  zscore: number | null;
  corr: number | null;
  btc_change_pct: number | null;
  gold_change_pct: number | null;
  ratio_change_pct: number | null;
  regime: string;
  source: string;
  note: string;
}

const f = (v: number | null, d = 2, unit = "") => (v === null || v === undefined ? "—" : `${v.toFixed(d)}${unit}`);

export default function GoldBtcSection() {
  const { data, error, loading } = useApi<GoldBtc>("/api/analytics/gold-btc", undefined, { refreshMs: 120000 });
  return (
    <Section title="BTC / gold ratio">
      <ErrorNotice error={error} />
      {loading && !data && <Loading what="Fetching closed candles from Binance…" />}
      {data && (
        <>
          <div className="notice">{data.note}</div>
          <div className="stat-grid">
            <Stat label="Ratio (oz of gold per BTC)">{f(data.ratio, 3)}</Stat>
            <Stat label="Z-score vs window">{f(data.zscore, 2)}</Stat>
            <Stat label="Regime">{data.regime}</Stat>
            <Stat label="Return correlation">{f(data.corr, 2)}</Stat>
            <Stat label="BTC change">{f(data.btc_change_pct, 2, "%")}</Stat>
            <Stat label="Gold change">{f(data.gold_change_pct, 2, "%")}</Stat>
            <Stat label="Ratio change">{f(data.ratio_change_pct, 2, "%")}</Stat>
          </div>
          <LineChart label="BTC / gold" points={data.points.map((p) => ({ x: p.time, y: p.ratio }))} />
          <div className="form-hint">{data.source}</div>
        </>
      )}
    </Section>
  );
}
