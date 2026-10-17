"use client";

// Crypto Trading equity / drawdown chart (separate from the Memecoin LineChart). One y-axis,
// 2px lines, the validated categorical palette in fixed order (series-1..8,
// globals.css, checked against the dark surface), a legend for 2+ series
// and direct end labels for up to 4, a crosshair + tooltip on hover or
// keyboard, and a table view. Values are drawn as given: nothing is
// interpolated or smoothed.

import { useMemo, useRef, useState } from "react";

export type ChartSeries = { name: string; t: string[]; y: number[] };

const W = 720;
const H = 240;
const PAD = { l: 56, r: 96, t: 12, b: 28 };

function fmt(v: number, unit: string): string {
  const s = Math.abs(v) >= 1000 ? v.toLocaleString(undefined, { maximumFractionDigits: 0 })
    : v.toLocaleString(undefined, { maximumFractionDigits: 2 });
  return unit === "%" ? `${s}%` : s;
}

export default function EquityChart({ series, unit = "", title, height = H, invert = false }: {
  series: ChartSeries[]; unit?: string; title: string; height?: number; invert?: boolean;
}) {
  const [hover, setHover] = useState<number | null>(null);
  const [table, setTable] = useState(false);
  const ref = useRef<SVGSVGElement>(null);
  const shown = series.slice(0, 8);
  const longest = useMemo(() => shown.reduce((a, s) => (s.y.length > a.y.length ? s : a), shown[0] ?? { name: "", t: [], y: [] }),
    [shown]);
  const all = shown.flatMap((s) => s.y).filter((v) => Number.isFinite(v));
  if (!shown.length || !all.length) {
    return <div className="empty-state">No data to chart.</div>;
  }
  let lo = Math.min(...all);
  let hi = Math.max(...all);
  if (hi === lo) { hi += 1; lo -= 1; }
  const n = Math.max(...shown.map((s) => s.y.length));
  const x = (i: number) => PAD.l + (n <= 1 ? 0 : (i / (n - 1)) * (W - PAD.l - PAD.r));
  const y = (v: number) => {
    const f = (v - lo) / (hi - lo);
    return PAD.t + (invert ? f : 1 - f) * (height - PAD.t - PAD.b);
  };
  const ticks = [lo, lo + (hi - lo) / 2, hi];
  const path = (s: ChartSeries) => s.y.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join("");

  function onMove(e: React.MouseEvent<SVGSVGElement>) {
    const r = ref.current?.getBoundingClientRect();
    if (!r) return;
    const px = ((e.clientX - r.left) / r.width) * W;
    const i = Math.round(((px - PAD.l) / (W - PAD.l - PAD.r)) * (n - 1));
    setHover(Math.max(0, Math.min(n - 1, i)));
  }
  function onKey(e: React.KeyboardEvent<SVGSVGElement>) {
    if (e.key === "ArrowRight") setHover((h) => Math.min(n - 1, (h ?? -1) + 1));
    if (e.key === "ArrowLeft") setHover((h) => Math.max(0, (h ?? 1) - 1));
    if (e.key === "Escape") setHover(null);
  }

  return (
    <figure className="viz-root" style={{ margin: 0 }}>
      <figcaption className="viz-head">
        <span className="viz-title">{title}</span>
        <button className="btn btn-sm btn-ghost" onClick={() => setTable(!table)} aria-pressed={table}>
          {table ? "Chart" : "Table"}
        </button>
      </figcaption>
      {shown.length >= 2 && (
        <div className="viz-legend" aria-hidden={table}>
          {shown.map((s, k) => (
            <span key={s.name}><i style={{ background: `var(--series-${k + 1})` }} />{s.name}</span>
          ))}
        </div>
      )}
      {table ? (
        <div className="table-scroll" style={{ maxHeight: 280 }}>
          <table className="data-table">
            <thead><tr><th>Time</th>{shown.map((s) => <th key={s.name}>{s.name}</th>)}</tr></thead>
            <tbody>
              {longest.t.map((t, i) => (
                <tr key={t + i}><td className="mono small">{t.slice(0, 16).replace("T", " ")}</td>
                  {shown.map((s) => <td key={s.name} className="mono">{s.y[i] != null ? fmt(s.y[i], unit) : "—"}</td>)}</tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div style={{ position: "relative" }}>
          <svg ref={ref} viewBox={`0 0 ${W} ${height}`} width="100%" role="img" tabIndex={0}
            aria-label={`${title}. ${shown.length} series. Use the arrow keys to read values, or open the table view.`}
            onMouseMove={onMove} onMouseLeave={() => setHover(null)} onKeyDown={onKey} className="viz-svg">
            {ticks.map((v) => (
              <g key={v}>
                <line x1={PAD.l} x2={W - PAD.r} y1={y(v)} y2={y(v)} className="viz-grid" />
                <text x={PAD.l - 6} y={y(v) + 4} textAnchor="end" className="viz-axis">{fmt(v, unit)}</text>
              </g>
            ))}
            {[0, Math.floor((n - 1) / 2), n - 1].map((i) => (
              <text key={i} x={x(i)} y={height - 8} textAnchor={i === 0 ? "start" : i === n - 1 ? "end" : "middle"}
                className="viz-axis">{(longest.t[i] ?? "").slice(0, 10)}</text>
            ))}
            {shown.map((s, k) => (
              <path key={s.name} d={path(s)} fill="none" stroke={`var(--series-${k + 1})`} strokeWidth={2}
                strokeLinejoin="round" strokeLinecap="round" />
            ))}
            {shown.length <= 4 && shown.map((s, k) => s.y.length > 0 && (
              <text key={s.name} x={x(s.y.length - 1) + 6} y={y(s.y[s.y.length - 1]) + 4} className="viz-label">
                {s.name.length > 14 ? s.name.slice(0, 13) + "…" : s.name}
              </text>
            ))}
            {hover != null && (
              <g>
                <line x1={x(hover)} x2={x(hover)} y1={PAD.t} y2={height - PAD.b} className="viz-cross" />
                {shown.map((s, k) => s.y[hover] != null && (
                  <circle key={s.name} cx={x(hover)} cy={y(s.y[hover])} r={4} fill={`var(--series-${k + 1})`}
                    stroke="var(--surface)" strokeWidth={2} />
                ))}
              </g>
            )}
          </svg>
          {hover != null && (
            <div className="viz-tip" role="status" style={{ left: `${(x(hover) / W) * 100}%` }}>
              <div className="muted small">{(longest.t[hover] ?? "").slice(0, 16).replace("T", " ")}</div>
              {shown.map((s, k) => s.y[hover] != null && (
                <div key={s.name} className="small"><i style={{ background: `var(--series-${k + 1})` }} />
                  {s.name}: <span className="mono">{fmt(s.y[hover], unit)}</span></div>
              ))}
            </div>
          )}
        </div>
      )}
    </figure>
  );
}
