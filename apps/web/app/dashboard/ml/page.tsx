"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import MLReadiness from "@/components/MLReadiness";
import Pagination from "@/components/Pagination";
import { apiGet, ApiError } from "@/lib/api";
import { usePagedList } from "@/lib/usePagedList";
import { formatDate } from "@/lib/format";
import type { MLStatsOut, ModelVersionOut } from "@/lib/types";

function modelStatusPillClass(status: string): string {
  if (status === "active") return "pill pill-ok";
  if (status === "retired") return "pill pill-off";
  return "pill pill-off";
}

export default function MLPage() {
  const router = useRouter();
  const [stats, setStats] = useState<MLStatsOut | null>(null);
  const { data, error, offset, setOffset, limit } = usePagedList<ModelVersionOut>("/api/ml/models", {});

  const loadStats = useCallback(async () => {
    try {
      setStats(await apiGet<MLStatsOut>("/api/ml/stats"));
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        router.replace("/login");
      }
    }
  }, [router]);

  useEffect(() => {
    loadStats();
  }, [loadStats]);

  return (
    <div>
      <div className="page-header">
        <h1 className="page-title">ML Engine</h1>
        <Link className="btn btn-ghost btn-sm" href="/dashboard/ml/review">
          ML Review: champion / challenger, drift, data quality
        </Link>
      </div>

      <MLReadiness />

      {stats && (
        <div className="detail-grid">
          <div className="card">
            <div className="status-label">Feature Snapshots</div>
            <div className="status-value">{stats.total_features}</div>
          </div>
          <div className="card">
            <div className="status-label">Labeled</div>
            <div className="status-value">{stats.labeled_features}</div>
          </div>
          <div className="card">
            <div className="status-label">Unlabeled</div>
            <div className="status-value">{stats.unlabeled_features}</div>
          </div>
        </div>
      )}

      {stats && stats.labeled_features === 0 && (
        <div className="empty-state" style={{ marginBottom: 20 }}>
          Zero labeled feature snapshots means training never runs (see docs/ML.md) — labels are only ever set by
          paper trading closing a position with a known outcome (docs/PAPER_TRADING.md).
        </div>
      )}

      <div className="page-title" style={{ fontSize: 16, marginBottom: 12 }}>
        Model registry
      </div>

      {error && <div className="error">{error}</div>}

      {data && data.items.length === 0 && (
        <div className="empty-state">No model has ever been trained — see the stats above for why.</div>
      )}

      {data && data.items.length > 0 && (
        <>
          <table className="data-table">
            <thead>
              <tr>
                <th>Name</th>
                <th>Version</th>
                <th>Status</th>
                <th>Samples</th>
                <th>Holdout AUC</th>
                <th>Trained</th>
                <th>Activated</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((m) => (
                <tr key={m.id}>
                  <td className="mono">{m.name}</td>
                  <td>{m.version}</td>
                  <td>
                    <span className={modelStatusPillClass(m.status)}>{m.status}</span>
                  </td>
                  <td>{m.training_sample_count}</td>
                  <td>
                    {typeof m.metrics.holdout_auc === "number" ? m.metrics.holdout_auc.toFixed(4) : "—"}
                  </td>
                  <td>{formatDate(m.trained_at)}</td>
                  <td>{formatDate(m.activated_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <Pagination total={data.total} limit={limit} offset={offset} onOffsetChange={setOffset} />
        </>
      )}
    </div>
  );
}
