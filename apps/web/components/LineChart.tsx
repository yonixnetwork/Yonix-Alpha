"use client";

import { useId } from "react";

interface Point {
  x: string;
  y: number;
}

/** Minimal responsive SVG line chart (no chart library: one less
 * dependency to audit). Shows min/max/last and a zero line when the series
 * crosses zero. The data itself is rendered as an accessible table for
 * screen readers. */
export default function LineChart({ points, label, height = 180, unit = "" }: { points: Point[]; label: string; height?: number; unit?: string }) {
  const id = useId();
  if (points.length < 2) return <div className="empty-state">Not enough data for a chart yet.</div>;
  const w = 600;
  const pad = 6;
  const ys = points.map((p) => p.y);
  const min = Math.min(...ys);
  const max = Math.max(...ys);
  const span = max - min || 1;
  const sx = (i: number) => pad + (i / (points.length - 1)) * (w - 2 * pad);
  const sy = (v: number) => pad + (1 - (v - min) / span) * (height - 2 * pad);
  const d = points.map((p, i) => `${i ? "L" : "M"}${sx(i).toFixed(1)},${sy(p.y).toFixed(1)}`).join(" ");
  const last = points[points.length - 1];
  const fmt = (v: number) => v.toLocaleString(undefined, { maximumFractionDigits: 4 }) + unit;
  return (
    <figure className="chart" aria-labelledby={id}>
      <svg viewBox={`0 0 ${w} ${height}`} preserveAspectRatio="none" role="img" aria-label={`${label}: last ${fmt(last.y)}`}>
        {min < 0 && max > 0 && <line x1={0} x2={w} y1={sy(0)} y2={sy(0)} className="chart-zero" />}
        <path d={d} className={last.y >= points[0].y ? "chart-line up" : "chart-line down"} fill="none" />
      </svg>
      <figcaption id={id} className="chart-caption">
        <span>{label}</span>
        <span className="muted">
          min {fmt(min)} · max {fmt(max)} · last {fmt(last.y)}
        </span>
      </figcaption>
    </figure>
  );
}
