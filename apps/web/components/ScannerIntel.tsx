"use client";

import { Factory, Network, Radar, Users, Eye } from "lucide-react";
import MarketCap from "@/components/MarketCap";
import { Section, Stat } from "@/components/ui";
import { formatDate } from "@/lib/format";

type J = Record<string, any>;
const pct = (v: unknown) => (v === null || v === undefined ? "unknown" : `${(Number(v) * 100).toFixed(0)}%`);
const num = (v: unknown, d = 2) => (v === null || v === undefined ? "unknown" : String(Number(Number(v).toFixed(d))));
const sol = (v: unknown) => (v === null || v === undefined ? "—" : `${num(v, 3)} SOL`);
const ax = (v: unknown) => (v === null || v === undefined ? "n/a" : `×${num(v)}`);
const short = (w?: string) => (w ? `${w.slice(0, 4)}…${w.slice(-4)}` : "—");
const CLASS_PILL: Record<string, string> = {
  CREATOR_RELATED: "pill pill-danger", COORDINATED: "pill pill-danger", FUNDING_RELATED: "pill pill-warn",
  POTENTIAL_COORDINATION: "pill pill-off", SMART_MONEY_CLUSTER: "pill pill-ok",
};
const RISK_PILL: Record<string, string> = { HIGH: "pill pill-danger", ELEVATED: "pill pill-warn", LOW: "pill pill-ok" };

/** Wallet relationships, effective buyers, organic demand, deployer
 * history and the manufactured-pump detector for one token. Evidence and
 * features, never predictions: unknown stays unknown. */
export default function ScannerIntel({ intel }: { intel: J }) {
  const rel: J = intel.relationships ?? {};
  const dep: J = intel.deployer ?? {};
  const mp: J = intel.manufactured_pump ?? {};
  const obs: J = intel.observation ?? {};
  const b: J = rel.buyers ?? {}; const d: J = rel.demand ?? {}; const cr: J = rel.creator ?? {};
  const sm: J = rel.smart_money ?? {}; const acc: J = rel.acceleration ?? {}; const f30: J = rel.flows?.["30s"] ?? {};
  const f60: J = rel.flows?.["60s"] ?? {};
  return (
    <Section title="Demand, wallet relationships & deployer">
      <p className="muted small">
        Relationships are evidence of wallet dependence or coordination, not proof of manipulation. Smart-wallet activity,
        deployer history and the manufactured-pump pattern are features whose predictive value is measured (ML Review →
        feature ablation), never BUY signals.
      </p>
      {rel.status !== "MEASURED" ? (
        <p className="muted">Relationship analysis {rel.status ? `${rel.status}: ${rel.reason ?? rel.error ?? ""}` : "not recorded for this decision"}.</p>
      ) : (
        <>
          <div className="stat-grid">
            <Stat label="Buyers: raw → effective" hint={b.explanation}>
              <Users size={14} aria-hidden /> {b.raw_unique_buyers} → {b.effective_unique_buyers}
              <div className="muted small">{b.creator_related_buyers} creator-related · {b.coordinated_buyers} coordinated ·{" "}
                {b.independent_buyers} independent · {b.unattributed_buyers} unattributed</div>
            </Stat>
            <Stat label="Organic-demand ratio" hint={d.why ?? "independent volume / total volume"}>
              {d.organic_demand_ratio !== null && d.organic_demand_ratio !== undefined ? pct(d.organic_demand_ratio)
                : `${pct(d.organic_demand_ratio_lower)} – ${pct(d.organic_demand_ratio_upper)}`}
              <div className="muted small">{d.status} · {pct(d.attributed_volume_share)} of volume attributed</div>
            </Stat>
            <Stat label="Volume">
              {sol(d.total_volume_sol)}
              <div className="muted small">creator-related {sol((d.creator_volume_sol ?? 0) + (d.creator_linked_volume_sol ?? 0))} · cluster{" "}
                {sol((d.funding_cluster_volume_sol ?? 0) + (d.coordinated_volume_sol ?? 0))} · independent {sol(d.independent_volume_sol)} ·
                unattributed {sol(d.unattributed_volume_sol)}</div>
            </Stat>
            <Stat label="Creator share" hint={cr.adjustment_reason}>
              {pct(cr.creator_related_volume_ratio)} of volume
              <div className="muted small">adjusted volume {sol(cr.adjusted_volume_sol)} of {sol(cr.raw_volume_sol)} · creator sells{" "}
                {pct(cr.creator_sell_ratio)} of sell volume</div>
            </Stat>
            <Stat label="Net SOL flow (30 s / 60 s)">
              {num(f30.net_sol_flow ?? 0, 3)} / {num(f60.net_sol_flow ?? 0, 3)}
              <div className="muted small">organic 30 s {num(f30.organic_net_sol_flow ?? 0, 3)} · cluster buys 30 s {num(f30.cluster_buy_flow ?? 0, 3)}</div>
            </Stat>
            <Stat label="Acceleration (last 30 s vs previous)" hint={acc.note}>
              volume {ax(acc.raw_volume_acceleration)} · organic {ax(acc.organic_volume_acceleration)}
              <div className="muted small">buyers {ax(acc.raw_buyer_acceleration)} · effective buyers {ax(acc.effective_buyer_acceleration)}
                {" "}(n/a: nothing in the previous 30 s)</div>
            </Stat>
            <Stat label="Smart money" hint={sm.note}>
              {sm.context ?? "UNKNOWN"}
              <div className="muted small">{sm.smart_money_count ?? 0} proven · {sm.smart_money_independent ?? 0} independent ·{" "}
                {sm.smart_money_clustered ?? 0} clustered · {sm.smart_money_creator_related ?? 0} creator-related · quality {num(sm.smart_money_quality)}</div>
            </Stat>
            <Stat label="Evidence coverage">
              <Network size={14} aria-hidden /> funding known for {rel.coverage?.funding_known ?? 0} of {rel.coverage?.traders ?? 0} traders
              <div className="muted small">{rel.coverage?.shared_funder_activity_unchecked ? `${rel.coverage.shared_funder_activity_unchecked} share an unchecked funder · ` : ""}
                graph {rel.graph_version} · as of {formatDate(rel.as_of)}</div>
            </Stat>
          </div>
          {(rel.clusters ?? []).length > 0 && (
            <div className="table-scroll">
              <table className="data-table">
                <caption className="table-caption">Funding clusters ({rel.cluster_count})</caption>
                <thead><tr><th>Cluster</th><th>Classification</th><th>Size / buyers</th><th>Parent (fan-out)</th>
                  <th>Same-second buys / sells</th><th>Buy / sell SOL</th><th>Evidence</th></tr></thead>
                <tbody>{rel.clusters.map((c: J) => (
                  <tr key={c.cluster_id}>
                    <td className="mono">{c.cluster_id}</td>
                    <td><span className={CLASS_PILL[c.classification] ?? "pill pill-off"}>{c.classification}</span></td>
                    <td>{c.size} / {c.buyers}</td>
                    <td className="mono small">{(c.parents ?? []).map((p: string) => `${short(p)} (${c.funding_fanout?.[p] ?? "?"})`).join(", ") || "—"}</td>
                    <td>{c.coordinated_buy_seconds} / {c.coordinated_sell_seconds}</td>
                    <td className="mono">{num(c.buy_sol, 3)} / {num(c.sell_sol, 3)}</td>
                    <td className="small">{(c.evidence ?? []).slice(0, 2).join("; ")}</td>
                  </tr>
                ))}</tbody>
              </table>
            </div>
          )}
          {(rel.wallets ?? []).length > 0 && (
            <div className="table-scroll">
              <table className="data-table">
                <caption className="table-caption">Largest traders by volume</caption>
                <thead><tr><th>Wallet</th><th>Role</th><th>Cluster</th><th>Buy / sell SOL</th><th>Evidence</th></tr></thead>
                <tbody>{rel.wallets.map((w: J) => (
                  <tr key={w.wallet}>
                    <td className="mono">{short(w.wallet)}{w.proven ? " · proven" : ""}</td>
                    <td>{w.role}</td><td className="mono small">{w.cluster_id ?? "—"}</td>
                    <td className="mono">{num(w.buy_sol, 3)} / {num(w.sell_sol, 3)}</td>
                    <td className="small">{(w.evidence ?? []).join("; ") || "—"}</td>
                  </tr>
                ))}</tbody>
              </table>
            </div>
          )}
        </>
      )}
      <div className="stat-grid">
        <Stat label="Deployer history" hint={dep.source}>
          <Factory size={14} aria-hidden /> {dep.status ?? "not recorded"}
          <div className="muted small">
            {dep.deployer_launch_count !== undefined ? `${dep.deployer_launch_count} earlier launches (${dep.launches_last_24h ?? 0} in 24 h) · ${dep.resolved_launches ?? 0} resolved` : dep.reason ?? ""}
          </div>
        </Stat>
        {dep.resolved_launches > 0 && (
          <Stat label="Deployer outcomes" hint={`shrunk toward the base rate (prior ${dep.prior_strength}); cutoff ${dep.deployer_history_cutoff}`}>
            risk score {num(dep.deployer_risk_score, 3)}
            <div className="muted small">bond rate {pct(dep.deployer_bond_rate)} · win {pct(dep.deployer_win_rate)} · loss {pct(dep.deployer_loss_rate)} ·
              recent {pct(dep.deployer_recent_success_rate)} · creator sold early {pct(dep.deployer_creator_sell_rate)}</div>
          </Stat>
        )}
        {dep.deployer_peak_mc_sol?.median !== undefined && dep.deployer_peak_mc_sol?.median !== null && (
          <Stat label="Deployer median peak market cap"><MarketCap sol={dep.deployer_peak_mc_sol.median} historical />
            <div className="muted small">p25 {num(dep.deployer_peak_mc_sol.p25)} · p75 {num(dep.deployer_peak_mc_sol.p75)} SOL · n {dep.deployer_peak_mc_sol.n}</div></Stat>
        )}
        <Stat label="Manufactured-pump pattern" hint={mp.note}>
          <Radar size={14} aria-hidden /> <span className={RISK_PILL[mp.risk] ?? "pill pill-off"}>{mp.risk ?? "not recorded"}</span>
          <div className="muted small">{mp.risk === "UNKNOWN" ? mp.reason : mp.score !== undefined
            ? `score ${num(mp.score)} · pattern ${mp.pattern_duration_seconds ?? 0}s · ${Object.entries(mp.conditions ?? {}).map(([k, v]) => `${k} ${v ? "met" : "not met"}`).join(", ")}`
            : ""} {mp.detector_version ? `· ${mp.detector_version}` : ""}</div>
        </Stat>
        <Stat label="Observation coverage" hint={obs.note}>
          <Eye size={14} aria-hidden /> {obs.coverage_status ?? "not recorded"}
          <div className="muted small">{obs.observation_count ?? 0} trades · first seen {obs.first_seen_at ? formatDate(obs.first_seen_at) : "—"}
            {obs.reason ? ` · ${obs.reason}` : ""}</div>
        </Stat>
      </div>
    </Section>
  );
}
