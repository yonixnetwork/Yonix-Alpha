"use client";

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { apiDelete, apiGet, apiPatch, apiPost, ApiError } from "@/lib/api";
import { formatDate } from "@/lib/format";
import type { BlacklistOut, CustomRuleOut } from "@/lib/types";

const SCOPES = ["GLOBAL", "solana_fresh", "solana_migration"];
const RULE_SCOPES = ["GLOBAL", "solana_fresh", "solana_migration", "binance_futures"];
const OPS = ["<", "<=", ">", ">=", "==", "!="];
const ACTIONS = ["WAIT", "REQUIRE_MANUAL_APPROVAL", "NO_TRADE", "REJECT", "ALLOW"];

export default function RulesPage() {
  const router = useRouter();
  const [blacklist, setBlacklist] = useState<BlacklistOut[]>([]);
  const [rules, setRules] = useState<CustomRuleOut[]>([]);
  const [fields, setFields] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [bl, setBl] = useState({ scope: "GLOBAL", field: "symbol", match_type: "exact", value: "", reason: "" });
  const [rule, setRule] = useState({ name: "", scope: "GLOBAL", field: "", op: ">", threshold: "", action: "WAIT" });

  const load = useCallback(async () => {
    try {
      const [b, r, f] = await Promise.all([
        apiGet<BlacklistOut[]>("/api/control/blacklist"),
        apiGet<CustomRuleOut[]>("/api/control/rules"),
        apiGet<string[]>("/api/control/rules/fields"),
      ]);
      setBlacklist(b);
      setRules(r);
      setFields(f);
      setRule((cur) => (cur.field ? cur : { ...cur, field: f[0] ?? "" }));
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) return router.replace("/login");
      setError("Failed to load rules.");
    }
  }, [router]);

  useEffect(() => {
    load();
  }, [load]);

  async function run(fn: () => Promise<unknown>) {
    setError(null);
    try {
      await fn();
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Request failed.");
    }
  }

  return (
    <div>
      <div className="page-header">
        <div className="page-title">Rules &amp; Blacklist</div>
      </div>
      {error && <div className="error">{error}</div>}

      <div className="section-title">Blacklist</div>
      <p className="muted">
        A match rejects the token outright. Patterns are shell-style globs (<span className="mono">*</span> any text,{" "}
        <span className="mono">?</span> one character), case-insensitive; a pattern of only wildcards is refused.
      </p>
      <div className="inline-form">
        <select value={bl.scope} onChange={(e) => setBl({ ...bl, scope: e.target.value })}>
          {SCOPES.map((s) => (
            <option key={s}>{s}</option>
          ))}
        </select>
        <select value={bl.field} onChange={(e) => setBl({ ...bl, field: e.target.value })}>
          <option value="symbol">symbol</option>
          <option value="name">name</option>
          <option value="mint">mint</option>
        </select>
        <select value={bl.match_type} onChange={(e) => setBl({ ...bl, match_type: e.target.value })}>
          <option value="exact">exact</option>
          <option value="pattern">pattern</option>
        </select>
        <input placeholder="value, e.g. *SCAM*" value={bl.value} onChange={(e) => setBl({ ...bl, value: e.target.value })} />
        <input placeholder="reason (optional)" value={bl.reason} onChange={(e) => setBl({ ...bl, reason: e.target.value })} />
        <button
          className="btn btn-sm"
          disabled={!bl.value.trim()}
          onClick={() => run(async () => {
            await apiPost("/api/control/blacklist", { ...bl, reason: bl.reason || null });
            setBl({ ...bl, value: "", reason: "" });
          })}
        >
          Add
        </button>
      </div>
      {blacklist.length === 0 ? (
        <div className="muted">No blacklist entries.</div>
      ) : (
        <table className="data-table">
          <thead>
            <tr>
              <th>Scope</th>
              <th>Field</th>
              <th>Match</th>
              <th>Value</th>
              <th>Reason</th>
              <th>Added</th>
              <th>Enabled</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {blacklist.map((b) => (
              <tr key={b.id}>
                <td>{b.scope}</td>
                <td>{b.field}</td>
                <td>{b.match_type}</td>
                <td className="mono">{b.value}</td>
                <td>{b.reason ?? "—"}</td>
                <td>{formatDate(b.created_at)}</td>
                <td>
                  <input
                    type="checkbox"
                    checked={b.enabled}
                    onChange={(e) => run(() => apiPatch(`/api/control/blacklist/${b.id}`, { enabled: e.target.checked }))}
                  />
                </td>
                <td>
                  <button className="btn btn-ghost btn-sm" onClick={() => run(() => apiDelete(`/api/control/blacklist/${b.id}`))}>
                    Delete
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <div className="section-title">Custom rules</div>
      <p className="muted">
        Conditions on measured features. A rule can only make the gate stricter: ALLOW never lifts a safety finding.
        A rule whose feature is unavailable for a token does not fire.
      </p>
      <div className="inline-form">
        <input placeholder="rule name" value={rule.name} onChange={(e) => setRule({ ...rule, name: e.target.value })} />
        <select value={rule.scope} onChange={(e) => setRule({ ...rule, scope: e.target.value })}>
          {RULE_SCOPES.map((s) => (
            <option key={s}>{s}</option>
          ))}
        </select>
        <select value={rule.field} onChange={(e) => setRule({ ...rule, field: e.target.value })}>
          {fields.map((f) => (
            <option key={f}>{f}</option>
          ))}
        </select>
        <select value={rule.op} onChange={(e) => setRule({ ...rule, op: e.target.value })}>
          {OPS.map((o) => (
            <option key={o}>{o}</option>
          ))}
        </select>
        <input placeholder="threshold" value={rule.threshold} onChange={(e) => setRule({ ...rule, threshold: e.target.value })} />
        <select value={rule.action} onChange={(e) => setRule({ ...rule, action: e.target.value })}>
          {ACTIONS.map((a) => (
            <option key={a}>{a}</option>
          ))}
        </select>
        <button
          className="btn btn-sm"
          disabled={!rule.name.trim() || !rule.threshold.trim()}
          onClick={() => run(async () => {
            await apiPost("/api/control/rules", rule);
            setRule({ ...rule, name: "", threshold: "" });
          })}
        >
          Add
        </button>
      </div>
      {rules.length === 0 ? (
        <div className="muted">No custom rules.</div>
      ) : (
        <table className="data-table">
          <thead>
            <tr>
              <th>Name</th>
              <th>Scope</th>
              <th>Condition</th>
              <th>Action</th>
              <th>Enabled</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {rules.map((r) => (
              <tr key={r.id}>
                <td>{r.name}</td>
                <td>{r.scope}</td>
                <td className="mono">
                  {r.field} {r.op} {r.threshold}
                </td>
                <td>{r.action}</td>
                <td>
                  <input
                    type="checkbox"
                    checked={r.enabled}
                    onChange={(e) => run(() => apiPatch(`/api/control/rules/${r.id}`, { enabled: e.target.checked }))}
                  />
                </td>
                <td>
                  <button className="btn btn-ghost btn-sm" onClick={() => run(() => apiDelete(`/api/control/rules/${r.id}`))}>
                    Delete
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
