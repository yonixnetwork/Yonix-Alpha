"use client";

import { Empty, ErrorNotice, Loading, Section } from "@/components/ui";
import { formatDate } from "@/lib/format";
import { useApi } from "@/lib/useApi";

type Entry = { request_id: string; method: string; path: string; params: string[]; status: number; ms: number; at: string };
type Out = { threshold_ms: number; items: Entry[]; by_path: { path: string; count: number; max_ms: number; errors: number }[] };

/** API requests that took 2 s or more or failed (apps/api request_timing).
 * A dashboard "Request failed" message names its request ID; this is where
 * it can be looked up. */
export default function SlowRequests() {
  const q = useApi<Out>("/api/system/slow-requests", { limit: 50 }, { refreshMs: 60000 });
  return (
    <Section title="API slow and failed requests">
      <ErrorNotice error={q.error} />
      {q.loading && !q.data && <Loading />}
      {q.data && q.data.items.length === 0 && <Empty>No API request took {q.data.threshold_ms / 1000} s or more, and none failed, since this was recorded.</Empty>}
      {q.data && q.data.items.length > 0 && (
        <>
          <p className="muted small">
            Kept: the latest {q.data.items.length} requests that took {q.data.threshold_ms / 1000} s or more or ended in a 5xx.
            A database statement is stopped after 25 s (QUERY_TIMEOUT) so a slow page fails fast instead of holding the others up.
          </p>
          <div className="table-scroll">
            <table className="data-table">
              <thead><tr><th>Endpoint</th><th>Requests</th><th>Slowest</th><th>Failed</th></tr></thead>
              <tbody>{q.data.by_path.map((b) => (
                <tr key={b.path}><td className="mono small">{b.path}</td><td>{b.count}</td><td>{(b.max_ms / 1000).toFixed(1)} s</td>
                  <td className={b.errors ? "neg" : ""}>{b.errors}</td></tr>))}</tbody>
            </table>
          </div>
          <details>
            <summary className="small">Every recorded request</summary>
            <div className="table-scroll">
              <table className="data-table">
                <thead><tr><th>When</th><th>Request</th><th>Status</th><th>Duration</th><th>Request ID</th></tr></thead>
                <tbody>{q.data.items.map((e) => (
                  <tr key={e.request_id + e.at}>
                    <td>{formatDate(e.at)}</td>
                    <td className="mono small">{e.method} {e.path}{e.params.length ? ` (${e.params.join(", ")})` : ""}</td>
                    <td className={e.status >= 500 ? "neg" : ""}>{e.status}</td>
                    <td>{(e.ms / 1000).toFixed(1)} s</td>
                    <td className="mono small">{e.request_id}</td>
                  </tr>))}</tbody>
              </table>
            </div>
          </details>
        </>
      )}
    </Section>
  );
}
