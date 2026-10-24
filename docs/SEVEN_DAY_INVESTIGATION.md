# Seven-day performance investigation (2026-10-24)

This change is an in-place upgrade; nothing was rebuilt or deleted. Live execution stays disabled throughout.

## Phase 1: findings

### VERIFIED FACTS

**Code and history**

| # | Fact | Source |
|---|---|---|
| 1 | The repository is at `ec79ecc` (PR #95, ML Training Lab). The branch was clean before this work. | git |
| 2 | Momentum PAPER, all time: 50 closed trades, booked −0.5153 SOL. Modelled fees are 0.3319 SOL, about 64 % of the loss. | `tools.pnl_reconcile`, 2026-10-22 server output |
| 3 | Momentum LIVE: 44 positions, booked −0.0212 SOL; consumed costs 0.0044 SOL. Rent deposits came back, except 0.0015 SOL still locked in one empty account. No LIVE trade since 2026-10-10. | same |
| 4 | Paper books reconcile to 0, except the archived Hyperliquid account (−15, not traded). | same |
| 5 | Out of sample, every recorded entry approach loses after costs. `MOMENTUM_CONTINUATION` on the hold-out: −9.93 % mean, PF 0.37. The gate's own entries: −12.17 %, n = 9 only. The skip-if-extended rule changed nothing out of sample. | `tools.entry_compare`, 81,056 signals, 2026-10-21 |
| 6 | The gate entered a median 30 minutes after the earliest signal for the same token, after a median +72 % move. | same |
| 7 | Median market MFE of LIVE momentum trades was about 2.5 %, and about a third never rose at all. | `tools.pnl_reconcile`, 2026-10-21 |

**Paper accounting**

| # | Fact | Source |
|---|---|---|
| 8 | Paper charges LIVE's fixed costs per round trip: buy and sell network + priority fees, plus the rent-reclaim transaction or the locked rent. | `live_trading.fixed_trade_costs`, `paper_engine` |
| 9 | Paper also charges the AMM / program fee and the curve's price impact, both inside the fill price. Each cost is counted once; booked PnL = proceeds − everything paid. | `paper_engine.open_position`, `apply_step` |

**Pipeline and data**

| # | Fact | Source |
|---|---|---|
| 10 | Since PR #93, momentum passes that find no free slot are still recorded as WATCH rows. 39 tokens in one hour (2026-10-22). | `tools.momentum_pipeline` |
| 11 | Checks that found no curve address fell from 44 % to 35 % after the TTL refresh. These are older tokens whose launch the stream never saw. They are counted only in the hourly funnel counters (48 h), not in the ledger. | same |
| 12 | Shadow-model scores are written onto ledger rows up to 6 h after the decision, and after training. Many stored "predictions" may therefore be retrospective. They were never used for decisions. | `services/ml/app/shadow_ml.py` |
| 13 | The market-regime record had launch and migration counts but no SOL price history. | `opportunities._regime` |
| 14 | The deployed revision was not visible to the application, so a test period could not say which code it ran on. | `scripts/deploy.sh` |

**Live trading control**

| # | Fact | Source |
|---|---|---|
| 15 | Live needs three `.env` locks (`TRADING_ENABLED`, `LIVE_TRADING_ENABLED`, `PAPER_TRADING=false`) and the stored global mode LIVE. The global mode is already selected in the dashboard and stored in the database; the locks are a deployment ceiling. | `safety.store.live_trading_permitted`, `live_activation` |

### DATA GAPS

- **Daily momentum PAPER results for the last 7 days.** Which days were profitable, from how many trades, and whether a single trade carried a day: not in any output I have. Run `performance_report --engine solana_momentum`.
- **Paper position sizes.** Fees of 0.0066 SOL per paper trade, against about 0.00015 SOL consumed per LIVE trade, suggest paper sizes much larger than the 0.003–0.017 SOL LIVE buys. Most of the paper fee would then be the percentage program / AMM fee. Not verified; the positions section shows size.
- **SOL price at past decisions.** Not recorded before this change (fact 13). Rising, falling or sideways SOL regimes can only be judged for decisions made after deploying this change.
- **Distinct tokens and trade concentration per day.** Shown by the new reports; not measured before.
- **Tokens dropped before the ledger** (no curve address, too young). They are only in 48-hour counters, so their outcomes are unknown.
- **Prospective model predictions.** How many stored scores were made before their outcome: now measured by `--section predictions`. Expected to be few, because of fact 12.

### POSSIBLE EXPLANATIONS: profitable one day, losing the next

None of these is established. The reports below test each one.

| Explanation | Fits the facts | Test |
|---|---|---|
| Few trades per day. With a few trades a day, one runner decides the day's sign. A profitable day can coexist with a negative expectancy. | 50 trades over about two weeks | Daily rows with trade counts; "best trade share of net" in the forward test |
| The typical move is about the size of the costs. Median MFE is about 2.5 %, and the round-trip percentage fee alone is about the same, so most trades lose a little and a rare large move wins. | Facts 2, 7, 9 | Gross vs net per day; median win / loss; fees per trade |
| Late entries. Entries come after much of the move. | Facts 5, 6 | `entry_compare` (done); the forward test keeps the same rules |
| Market activity. Quiet and busy hours may differ. | Untested | `by_regime` groups (QUIET / NORMAL / BUSY) |
| Exit behaviour. Stops near the costs, or trailing exits that give back gains. | Untested | `by_exit_reason`; exit timing classes |
| Large-wallet behaviour: a big buy draws buyers, then early holders take profit. | The user's hypothesis; untested | Phase 5 study, with a hold-out |

### TESTS REQUIRED

1. **Now, on the server:** `performance_report` (all sections) and `pnl_reconcile`. The commands are below.
2. **After deploying:** start a forward test, then run `forward_test status` daily for at least 7 days.
3. **Phase 5:** large-buy study with a chronological hold-out (next change).
4. **Phase 6:** score new ledger rows right after the decision, so predictions are prospective (next change).

### RECOMMENDED CHANGES

Done in this change:
- per-strategy, windowed performance diagnostics;
- the forward test with a baseline and uncertainty;
- deployment recording;
- the prediction audit;
- SOL price recorded with each decision.

Next changes:
- dashboard live-trading control, with the server-side checks listed in Phase 8 of the brief;
- ML engine monitoring with prospective scoring;
- the large-buy study.

Not recommended without evidence: any change to entry timing, sizing or exits. The forward test must first show a stable baseline.

## Phase 2: diagnostics

`$C` stands for:

```
cd /opt/yonixalpha && docker compose --env-file .env -f infra/docker/docker-compose.yml -f infra/docker/docker-compose.prod.yml
```

| # | Need | Command |
|---|---|---|
| 1 | Latest paper positions, one by one | `$C exec -T api python -m yonixalpha_core.tools.performance_report --section positions --mode PAPER --last 30` |
| 2 | Last 24 hours | `... performance_report --section day` |
| 3 | Last 7 complete days, daily | `... performance_report --section week` |
| 4 | All-time momentum | `... performance_report --section all --engine solana_momentum` |
| 5 | Live history and wallet reconciliation, rent apart | `$C exec -T api python -m yonixalpha_core.tools.pnl_reconcile --last 40` |
| 6 | Scanner, data sources, pipeline | `$C exec -T api python -m yonixalpha_core.tools.momentum_pipeline --hours 24`; dashboard Health → Scanner |
| 7 | Model predictions and outcomes | `... performance_report --section predictions --hours 168` |
| 8 | Rejected tokens and what happened after | `... performance_report --section rejected --hours 24` |

**The positions section shows:**
- token, mint and strategy;
- signal time;
- entry time and entry price;
- the price the decision saw;
- an entry slippage estimate, already inside gross and net;
- exit time, exit price and exit reason;
- size;
- gross, fees and net;
- holding time.

Open positions show a marked value labelled "unrealized, not booked".

**The window sections show, per strategy and mode (PAPER and LIVE apart):**
- closed trades and open positions;
- win / loss / breakeven counts;
- gross, fees and net;
- average and median win and loss, in SOL and %;
- largest win and loss;
- profit factor, or why it is undefined;
- net per trade (expectancy);
- return on the paper account's starting capital;
- maximum drawdown;
- average positions open at once and trades per day;
- groups by exit reason, stage (fresh, near-migration, migrated, momentum), token age, market activity and entry quality;
- data coverage.

Dashboard: **Solana Performance** (`/dashboard/performance`) has the same numbers, with a strategy filter, daily rows, the latest positions and the forward test.

**Definitions:**
- **Net:** booked realized PnL after every recorded cost.
- **Fees:** program, AMM, network, priority and close costs.
- **Gross:** net + fees.
- Price impact sits inside the fill price, so it is in gross and net already; it is shown as an estimate and never subtracted again.
- **Refundable rent** is in `pnl_reconcile` (deposit / reclaimed / locked), never in costs.

## Phase 3: forward test

1. Deploy. `deploy.sh` now records the revision: "Recording the deployed revision".
2. Check health: `momentum_pipeline --hours 1` shows the stream CONNECTED.
3. Start the period:

   ```
   $C exec -T api python -m yonixalpha_core.tools.forward_test start --label "after deploy <revision>"
   ```

   Or use the dashboard: Solana Performance → Forward test.
4. Daily: `$C exec -T api python -m yonixalpha_core.tools.forward_test status`.
5. After 7 days: the same command, plus `performance_report --section week`.

### What a period freezes at its start

- the rules: at least 30 closed trades, 7 days, 20 distinct tokens; one trade must not exceed half of a positive net; 95 % intervals; a 14-day baseline;
- the deployed revision;
- a fingerprint of the configuration: global mode, each strategy's mode and config, risk-settings versions, paper / live / exit / ML-contribution settings, active and challenger models;
- the data-source health.

Each report recomputes the fingerprint and names any component that changed: results after that point mix two configurations. Earlier periods are kept; `forward_test history` lists them.

### Verdicts, per strategy and mode

| Verdict | Meaning |
|---|---|
| INSUFFICIENT EVIDENCE | A rule is not met; the report says which. |
| IMPROVING | The interval of the difference to the baseline is above 0. |
| DETERIORATING | The interval of the difference to the baseline is below 0. |
| INCONCLUSIVE | Enough trades, but the interval includes 0. |
| NO BASELINE | No baseline trades to compare with. |

The **edge** line says whether the interval of net per trade lies above 0. Even then it is "not proof of a lasting edge".

### Signals

Every ledger decision in the period is listed with its first reason and the measured outcome:
- accepted;
- refused by the gate or the risk engine;
- watched or skipped for lack of a slot.

Decisions dropped before the ledger (no curve address, too young) appear only in `momentum_pipeline`.
