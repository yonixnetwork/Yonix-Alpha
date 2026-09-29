# Scanner intelligence layer (2026-09-29)

Goal: raw wallet count, raw volume and "a smart wallet bought" are not treated as
sufficient evidence of organic demand. Every signal below is a **feature**. The gate
acts on it only through a configurable action (WARN by default, so nothing blocks
until the operator decides). Its predictive value is measured by the ablation
experiment, never assumed.

## Research status

These are technical references and features to evaluate, **not** proof of
profitability. Primary sources were not reachable from this environment
(egress blocks launchwatch.dev, dev.to and arxiv.org); what follows is from
search-engine summaries.

| Reference | What we took | Status |
|---|---|---|
| DecClust / funding-graph write-ups | Trace early buyers' first SOL to a shared funder. Exclude exchange/infra funders by throughput (about 1,000 transactions). Coordinated operators fund 5–15 fresh wallets through intermediaries. | The same exclusion already existed (`BUSY_FUNDER_SIGNATURES = 1000`). Multi-hop tracing is **not** implemented (1 hop only). |
| MadeOnSol | Deployer intelligence, coordination, transparent risk factors | Concept only. Its "3+ cluster wallets → 94 % dump" claim is not reproduced and not used as a threshold. |
| LaunchWatch | Causal manufactured-pump pattern from candles | Features implemented. Its exact thresholds are **unverified**; ours are configurable starting values. |
| Pump.fun sniper-cohort paper (arXiv 2607.02795) | Persistent cohorts exist; cohort presence is not by itself a causal trading edge | Hence activity-matched validation instead of raw correlation. |

Rules of language used in code and UI:
- Smart-wallet activity is an observable feature whose predictive value must be validated.
- Funding-cluster relationships are evidence of wallet dependence or coordination. They affect the risk and organic-demand analysis; they do not prove manipulation.
- The manufactured-pump detector identifies a documented price/flow pattern associated with elevated manipulation risk. It is a defensive signal to evaluate, not a guarantee of a dump.
- Deployer history is one feature in the decision and ML systems.

## Components

| Section of the request | Implementation | Notes / limits |
|---|---|---|
| 1–2 Wallet relationship / coordination graph | `solana/wallet_graph.py`; funder→wallet edges persisted in `yx:wg:children:*` (30 days) by `solana/funding.py`, now with funding time and amount | Evidence kinds: creator funding, common non-exchange funder, co-dump history. Same-second timing only *upgrades* an existing link. Classes: CREATOR_RELATED, COORDINATED, FUNDING_RELATED, SMART_MONEY_CLUSTER, POTENTIAL_COORDINATION, plus a PROTOCOL_CONTROLLED role. Each cluster reports size, parents, fan-out, funding concentration, funding window, coordinated buy/sell seconds, SOL flow, net token exposure and evidence. **Coverage:** funding comes from the gate's RPC checks (first 6 buyers of gated candidates, cached a week), so most traders of most tokens are UNATTRIBUTED. Common-DEX-activity and wallet-age signals are not implemented (they would need extra RPC). |
| 3 Effective unique buyers | `buyers` block | A funding-related or coordinated cluster counts as one buyer. Creator, creator-linked and protocol wallets are removed. Potential-coordination and unattributed buyers are **not** subtracted. The explanation string shows the arithmetic. |
| 4 Organic-demand ratio | `demand` block | Volume is split into creator, creator-linked, funding cluster, coordinated, potential, protocol, independent and unattributed. The ratio is a single number only when ≥ `organic_min_attribution_share` of volume is attributed; otherwise lower and upper bounds. Unattributed volume is never counted as organic. |
| 5–6 Deployer intelligence, time-aware | `deployer_launches` (migration 0020) and `deployer_intel.py`. The ledger notes each launch when first tracked and resolves it at T+60m. | Features at T use only launches created before T **and** resolved at or before T; `deployer_history_cutoff` and `deployer_feature_timestamp` are stored. Rates are shrunk toward the base rate of launches resolved before T. Fewer than 3 resolved launches is INSUFFICIENT_HISTORY. Coverage: launches this system saw since deploy (history starts empty). |
| 7–9 Smart money + coordination | `smart_money` block | Context: NONE, ONE_SMART_WALLET, MULTIPLE_INDEPENDENT, MULTIPLE_RELATED, CREATOR_RELATED or UNKNOWN. Also quality (mean posterior lower bound), independence and signal strength (clusters collapse to one). Per-wallet PnL, cost basis and entry market cap are **not** available (the system tracks launch outcomes, not wallet PnL). |
| 10–12 Net SOL flow, organic acceleration, creator adjustment | `flows` (5 s–5 m), `acceleration`, `creator` blocks | Raw and adjusted volume are both kept, with an adjustment reason. |
| 13–15 Manufactured-pump detector | `solana/manufactured_pump.py` (`mp-1`) | Causal: candles only from trades up to the decision. Metrics: positive-return share, return CV, buy/sell mix stability, log-price R², window return. Thresholds are dashboard settings and stored with every result. It needs a developed pattern (UNKNOWN before), so it runs at every re-evaluation. Gate action `manufactured_pump_action` (default WARN). It does not act on open positions yet. |
| 16–17 Combined evidence / protocol addresses | Separate gate findings, each needing measured evidence. Protocol accounts (curve PDA, pool, pool authority, programs) are never creator wallets. | No single weak indicator declares a scam, rug or honeypot. |
| 18–22 ML features, validation, ablation, matched controls | `ml/opportunity_features.py` (`oppfeat-2026.09.2`, 39 new features with missing indicators, groups, and source/timestamp per group); `services/ml/app/ablation.py` | Wallet identities and cluster ids are **not** model inputs. A–F cumulative sets plus 5 leave-one-out ablations, evaluated forward in time. Verdicts come from a bootstrap interval and calibration, never from the largest return. Matched strata: engine, stage, regime, time of day, buyers, market cap, age, liquidity, recycled-wallet activity. |
| 23 Rejected-token learning | `analysis.relationship_followup` at T+60m | Records whether independent buyers arrived, organic demand rose, decision-time clusters exited, and the creator sold. Outcome analysis only; never a feature. |
| 24–25 Pipeline and latency | Relationship analysis = Redis `MGET` + pipeline over cached facts, no RPC; deployer = one indexed query | Latency per decision is in `timings_ms.relationships`. Missing data is reported as UNKNOWN / UNATTRIBUTED, never filled in. |
| 26 Dashboard | Token page "Demand, wallet relationships & deployer"; ML Review "Feature ablation"; settings group "Wallet relationships, organic demand & deployer history" | |
| 27 Observation coverage | `intel.observation` | COMPLETE, PARTIAL, STALE or UNAVAILABLE. **GAPPED is not detectable yet:** stream outages are not recorded. |
| 30 Required experiment | `python -m app.ablation` in the ml container; every 6 h automatically; `GET /api/ml/ablation` | Needs ≥ 200 completed, labelled ledger rows **recorded after this deploy** for the new layers to have data. |

## Gate findings (all WARN by default)

LOW_EFFECTIVE_BUYERS (`min_effective_buyers` 5), LOW_ORGANIC_DEMAND
(`min_organic_demand_ratio` 0.2, only when measured), HIGH_COORDINATION
(`coordination_high_wallets` 4), CREATOR_CONCENTRATION
(`max_creator_related_volume_ratio` 0.3), HIGH_SMART_MONEY_CONCENTRATION,
POOR_DEPLOYER_HISTORY (`deployer_max_risk_score` 0.8 with ≥ 3 resolved),
MANUFACTURED_PUMP_PATTERN. Setting a threshold to 0 turns that check off; each has
its own `*_action`.

## Storage cost

The stored relationships block is compact: no per-wallet table on ledger rows, at
most 5 clusters × 5 members, and zero flows dropped. It is about 5 KB for a
300-trade token, less for typical rejected tokens. The per-wallet table (10
largest traders) is kept only on gate evaluations.
