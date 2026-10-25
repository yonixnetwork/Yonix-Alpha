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

**Added after the 2026-10-24 server output (`tools.pnl_reconcile`, before deploying):**

| # | Fact | Source |
|---|---|---|
| 16 | The `solana` paper book no longer matches its own closed trades: cash 8.817569, start 10, realized −1.118834, nothing open. That is a difference of **−0.063596 SOL**; on 2026-10-22 it was 0. | `tools.pnl_reconcile` |
| 17 | The decision engine (entries) and the position manager (exits) both write that row from different processes with a read-modify-write and no lock. A transaction that read the balance earlier overwrites a change committed in between. Reproduced in `tests/test_paper_cash_concurrency.py`: the old pattern loses an exit's +0.3. **Fixed**: every cash change is now one atomic `cash = cash + delta` (`safety.store.adjust_cash`). The −0.0636 already lost stays; correcting it is the operator's decision and must be audited. | code, test |
| 18 | Per-trade booked PnL is not affected; only the book's cash is. The cash feeds RISK_PCT sizing and the paper-cash check. | code |
| 19 | Paper, all time, every Solana strategy negative: fresh 79 trades −0.423 SOL; migration 166 trades −0.180 SOL; momentum 50 trades −0.515 SOL. | same |
| 20 | Live, all time: 105 positions, booked −0.0479 SOL (fresh −0.0131, migration −0.0135, momentum −0.0212). About 0.0076 SOL of rent is still refundable in 5 empty token accounts. | same |
| 21 | Three closed LIVE positions have no booked PnL (PVE, VIBEPAD, 7wmm1gDa). These are data gaps, not zero results. | same |
| 22 | Last 24 h: 37 paper entries (fresh 10, migration 27) and 0 momentum entries, even though momentum is AUTO: 246 momentum decisions were refused. | `tools.live_check` |

**Added after the first post-deploy output (revision e0149d5, 2026-10-24 21:27 UTC; PAPER):**

| # | Fact | Source |
|---|---|---|
| 23 | Momentum, all time: 50 trades, net −0.515 SOL, fees 0.332 SOL (gross −0.183). 23 stop-loss exits lost −0.992 SOL; 4 take-profit-3 exits made +0.381 and 11 trailing stops +0.133. Average loss −14.7 % (median −12.9 %); single stop exits reached −23 %, −30 % and −35 %. | `performance_report --section all` |
| 24 | Momentum by token age: 30 min–2 h, 29 trades, −0.025 (PF 0.95, positive before fees); 2–24 h, 18 trades, −0.526 (PF 0.11). Entries with a deterioration indicator: 13 trades, −0.297 (PF 0.21). Both groups are below 20 trades. | same |
| 25 | Momentum traded once in the 7 complete days to 2026-10-24 (−0.023). Most of its 50 trades are from 2026-10-10/11. A daily momentum pattern cannot be read from this week. | `--section week` |
| 26 | Fresh, 7 days: 24 trades, net −0.063. Daily: +0.057 (1 trade), −0.104 (5), −0.188 (6), −0.013 (6), +0.184 (6). 6 stop losses lost −0.572 (average loss −45.7 %, largest −0.160); 12 take-profit-3 exits made +0.432. | same |
| 27 | Migration, 7 days: 53 trades, net −0.022, gross +0.025: the fees (0.048) turn a small gross gain into a loss. | same |
| 28 | Every migration ledger decision shows "measured 0". By design: the executable return is simulated on the bonding curve only, and a migrated token trades on a PumpSwap pool (`opportunities._finalize`). Migration refusals cannot be judged from the ledger. | code |
| 29 | Shadow return model, prospective scores only: favoured 102 rows, mean −3.7 %; not favoured 5 338 rows, −9.9 %. The ranking points the right way, but even the favoured group loses on average. | `--section predictions` |
| 30 | The paper book difference is still −0.063596 SOL just after deploying the cash fix. Expected: the fix stops new losses and does not restore old ones. | `tools.pnl_reconcile` |

**Report defects found in that output, fixed:**
- 11 LIVE entries that never filled (`failed`, no quantity, no cost) were listed as "OPEN (not booked)". They are now counted apart as entries that never filled.
- Ledger reasons were split by their numbers: "liquidity 0.0031 below minimum 20" and "liquidity 0.0051 ..." were separate rows. Numbers now read N, so one reason is one row.
- The all-time "trades/day 0.01" and "avg open at once 0.0" were measured from 2000-01-01. All-time rates now start at the first entry (`rates_from`).
- An engine with no measured outcome now gets a DATA GAP line saying why.

**Why a day is profitable and the next is not (fresh and momentum, PAPER):**
1. **Few trades per day.** About 6 a day for fresh, so one or two exits decide a day's sign.
2. **Wins and losses are lopsided.** A fresh win is capped near +27 % (take profit 3). A stop exit averaged −46 %: the price passes the stop between two observations, and the exit fills far below it. One stop exit (−0.16) can cancel about five wins (+0.033 each).
3. **Bad days are the days with stop exits;** 2026-10-23 had six wins and no stop exit.

So the daily sign mostly reflects whether a stop exit happened that day. It does not show a change in edge. Over the 7 days the result is −0.063 SOL.

**Not changed (each needs forward-test evidence first):** stop placement and gap handling; excluding 2–24 h momentum tokens; excluding entries with deterioration indicators; position size. Each group above has fewer than 20 trades or comes from a single period.

### Friday 2026-10-23 vs the weekend (started 2026-10-25)

**Did a code change cause it?** The server ran the same revision (#95 or earlier; deploys were not recorded before #96) from Friday until about 21:27 UTC on Saturday. Then #96–#100 were deployed (e0149d5). Every non-test change in that deploy was reviewed (`git diff ec79ecc e0149d5`):

| Change | Can it change which tokens are traded, the size or the exits? |
|---|---|
| Paper cash written atomically (`adjust_cash`, #100) | Sizing reads the cash; the fix only stops lost updates. The amounts are the same. |
| LIVE checks in `live_readiness` / the order worker (#97) | LIVE orders only; the gate, PAPER entries and exits are not touched. |
| SOL price kept on ledger rows (`_regime`, #96) | Recording only, after the decision. |
| Large-buy candidates at stream ingest; observation loop (#99) | Recording only. A failure is logged and never stops the scanner. |
| Shadow scores for new decisions every 30 s (#98) | Writes `ml_shadow` only; shadow models are never loaded for decisions. |
| Reports, API read endpoints, dashboard pages (#96–#101) | Read-only. |

So a Saturday loss before 21:27 UTC cannot come from this deploy. After it, none of these changes alters a decision. What could have changed without a code change, and is now listed by `day_compare` CHANGES:
- settings or strategy-mode edits;
- an automatic entry-strategy demotion (`entry_intel.strategy_demoted`, PAPER → SHADOW, only on a 95 % interval below 0);
- model versions;
- deploys.

**New tool: `tools.day_compare`.** It shows each day's results, the trades one by one with what the decision saw, the funnel, the market, data quality and every recorded change around the days. Days follow `--tz`.

**Data gaps for these days:**
- No SOL/USD history before 2026-10-25. Ledger rows only carry it from the Saturday deploy, and only when the cached price was older than the decision. Now recorded: `market_samples`, every 5 min (SOL/USD, launches and migrations per hour, active tokens, stream heartbeat).
- Large buys before a decision exist only from about 21:27 UTC on 2026-10-24.
- Service warnings are pruned after the retention days (default 3), so Friday's may be gone.
- Scanner stage counters are kept 48 h (`momentum_pipeline`).

**Results from the server (2026-10-25 11:35 UTC; server and database in UTC; PAPER):**

*Verified*
1. **Same code and settings on Friday and Saturday.** The server ran ec79ecc from 2026-10-22 07:56 until #99/#100 were pulled at 20:57–21:07 on Saturday. No risk-settings version after 2026-10-21. No automatic demotion, model promotion or global-mode change. Every gate model version in the window is `superseded` and was never activated. Positions carry no model version, so decisions were rule-based.
2. **Every Saturday trade closed by 17:40:36 UTC.** That is before the RPC outage (Helius off 19:11–20:28; `rpc_outage` ×2, `rpc_health_check_failed` ×157) and before the deploy. Neither caused Saturday's result.
3. **Momentum** had no closed trade on either day. Its PAPER → AUTO switch (Saturday 10:06) cannot explain the difference.
4. **Friday** (+0.1837 SOL, 20 trades):
   - fresh: 6/6 wins, +0.1843 (5 × take-profit 3);
   - migration: 14 trades, −0.0006.
   - The best trade is 28.5 % of the wins; without it, fresh is still +0.132. So the profit came from several independent trades, not one winner.
   - Three near-migration fresh trades (SHEKEL, TRUMP2, zCondom) gave +0.137.
5. **Saturday** (−0.0795, 19 trades):
   - fresh: 2/7 wins, −0.0494. Stops at −30.4 % (ONION, planned 10 %), −30.4 % (TAHOE), −25.5 % (Nuannuan), and a −18.2 % exit on APOGEE, whose price fell 86 % within 148 s.
   - migration: 3/12 wins, −0.0301. Eight stop-losses (−0.0341), including $MRLY at −66.4 % in 25 s against a 10 % planned stop. The other seven stops lost about as planned (average −14.5 %).
6. **Only the price after entry differs.**
   - Same between the days: slippage estimate (median 1.31 %), liquidity (about 57 SOL), size (median 0.0223), fees (3.4–4.3 %).
   - *Correction (2026-10-25):* the "entry delay 0 s" printed by the first `day_compare` measured only the order time. The simulated fill lands 2.5–10 s later (`paper_execution.settle_entry_delay`). `day_compare` now prints that latency and the price move during it, for both days.
   - Different: maximum rise after entry, median +26.5 % Friday vs +10.9 % Saturday and +10.6 % Sunday; maximum fall −8.7 % vs −21.8 % and −15.8 %.
7. **Friday is the only positive day of the last six:** 10-20 −0.0945, 10-21 −0.1895, 10-22 −0.0591, 10-23 +0.1837, 10-24 −0.0795, 10-25 (partial) −0.0871. There was no profitable period that then got worse.
8. **Size spreads the results.**
   - Migration has `max_loss_per_trade_sol` 0.0067, so its trades are about 0.022 SOL.
   - Fresh has none, so risk-based sizing gives 0.04–0.17 SOL.
   - Trades of at least 0.1 SOL in 7 days: 20, net −0.349, of which near-migration fresh trades: 14, −0.262, median loss −56.6 %.

*Hypotheses (not established)*
- **Random variation** around a negative average for fresh (−0.0092 per trade over the 14-day baseline). This fits best.
- **Market activity:** median launches per hour seen by decisions were 1,432–1,464 on Friday and 1,099–1,242 on Saturday. SOL price is not known for these days.
- **Late entries after a run-up:** median 5-minute move before entry +11.9 % Friday vs +54.9 % Saturday, but only 3–4 trades a day have that value.
- **Weaker buying at entry:** median buyers/sellers 3.2 vs 2.0.
- **Gap risk:** fast dumps (−65 % in 25 s, −86 % in 148 s) pass the stop between observations.

*Anomalies, inspected with `execution_funnel --mint`*
- **KUNGFU:** four exit-intelligence reductions (23:38–00:04, sellers outnumbering buyers, volume collapsing) sold most of the position at a loss. Only the small remainder reached take-profit 1. `exit_reason` keeps only the last step, so the loss was reported under `take_profit_1`.
  - Fixed: every exit step is now recorded on the position (`plan.exit_steps`, paper and LIVE).
  - `day_compare` prints the whole exit path.
- **苦根:** the decision fill was at 1.691e-7. The simulated fill 10 s later was 34 % higher, at 2.269e-7, measured from pool trades.
  - Take-profit and stop levels stay where the decision put them (paper and LIVE alike). The higher fill was already above TP1 and TP2, so both fired at once, and TP3 then closed the trade at −2.1 %.
  - This is an exit-design question, not a simulation error: levels set from the decision price, not the fill. Not changed.
- **TRUMP2:** the price fell 6.4 % during the 4 s fill, so the fill was cheaper. The fill model works both ways, as intended.

*Sunday 2026-10-25 (to 11:35 UTC):* 14 trades, −0.0871.
- Fresh: 2 stop exits, −0.052; eToro −20.1 % within 11 s.
- Momentum: SHARD −0.023.
- Migration: 4/11 wins, −0.012.
- Recorded market samples: SOL/USD 100.84 → 101.19 (+0.35 %); median launches per hour 944–1,036, lower again than Friday.

*Not changed:* thresholds, exits, sizing, strategy modes. Each candidate needs forward-test evidence or the operator's decision. A per-trade loss cap for fresh is a risk limit for the operator to set, not a fix made to improve past results.


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

## Phase 8: dashboard LIVE control (`yonixalpha_core/live_control.py`)

### Who decides what

| Layer | Where | Role |
|---|---|---|
| Server ceiling | `.env`: `TRADING_ENABLED`, `LIVE_TRADING_ENABLED`, `PAPER_TRADING=false` | Set **once** by the server owner. While it is closed, nothing can trade live. It is the deployment-level hard limit and emergency stop, not the everyday switch. Unchanged. |
| Operating mode | Dashboard: Settings or Live Execution → **Operating mode and LIVE control** | The everyday selector, stored in the database: PAPER / MANUAL with the buttons, LIVE only through **Arm LIVE**. |
| Kill switch | Risk page | Stops every new entry at once; exits continue. |

**The conflict found:** the dashboard could already store global mode LIVE, but with your current `.env` (`LIVE_TRADING_ENABLED=false`, `PAPER_TRADING=true`) the ceiling is closed, so dashboard activation is impossible. That is by design and is not bypassed.

**The safe migration:** open the ceiling once (steps below). From then on PAPER ↔ LIVE is a dashboard action. Opening the ceiling alone trades nothing: the global mode stays PAPER until you arm LIVE.

### States

| State | Meaning |
|---|---|
| OBSERVATION ONLY | No strategy may enter; the scanner keeps observing. |
| PAPER trading | Entries are simulated. The panel lists what would block LIVE. |
| LIVE armed, blocked | The global mode is LIVE, but a mandatory check fails: no live order is created or sent. Each reason is shown. |
| LIVE enabled and eligible | Every check passes; a decision that passes the gate may execute in the live wallet. |

### Mandatory checks, enforced on the server

**Every order** (re-run by the order worker every 15 s; the smoke test is included):

| Check | What it verifies |
|---|---|
| Ceiling | The `.env` ceiling is open. |
| Kill switch | It is released. |
| Wallet | The wallet is configured. |
| Signer | The order worker is ready to sign. |
| RPC | Wallet read fresh, and no more than 50 % of RPC calls rate-limited. |
| Network | Solana mainnet, by the genesis hash the worker reads from the RPC. |
| Program and token addresses | The mainnet addresses. |
| Balance | Covers the reserve, the largest live size, the fixed costs and the token-account rent. |
| Duplicate-order protection | Unique idempotency key in the database, and no sent order unresolved for more than 5 min. |
| Exposure | Open live positions below the maximum; an exposure cap set. |
| Scanner | Data fresh. |
| Risk engine | Heartbeat fresh. |
| Critical faults | No LIVE position needing review or with failed exits. |

**To arm:**

| Check | What it verifies |
|---|---|
| Entry switches | On. |
| Strategy | At least one strategy set to MANUAL or AUTO. |
| Configuration | Complete, validated as it would be with LIVE on. |
| Sizing | Position size set, and a maximum loss per trade set (SOL). |
| Stop policy | Every entry has a stop. |
| Limits | Slippage and price-impact limits within the hard limits. |

**Switching LIVE on** (changed 2026-10-25, at your request): the **LIVE trading OFF / ON toggle** (Settings and Live Execution). ON opens a dialog that says real funds may be used. After you confirm, the server runs every check. If any check fails, LIVE stays OFF and the page lists each failing check with its reason and what to do. There is no password and no typed phrase any more; a logged-in dashboard session plus the dialog is enough, so keep the dashboard login safe. OFF is immediate.

While the `.env` ceiling is closed, four checks also fail because of it: signer, RPC, network and balance. The order worker does not start the live signer, read the wallet or check the network until the ceiling is open. Those checks say so and are re-run once it is open; none was removed.

Each attempt is audited: who, when, from, to, reason, and the failing checks if refused.

The PAPER / MANUAL buttons can never switch LIVE on.

### Enforcement

Hiding a button would not be security, so the server enforces every rule:

- **Order creation:** `live_trading.live_readiness` refuses to create a LIVE order unless the worker's latest check record (`yx:live:control`, 60 s TTL) is fresh and passing.
- **Before signing:** under the order's row lock, the worker refuses to send a BUY while the global mode is not LIVE, the kill switch is engaged, or the checks fail.
- **Every second:** the worker cancels queued BUYs in those cases.
- **Fix:** before this change, a BUY queued just before switching back to PAPER would still have been sent.
- **Exits are never held:** SELL orders keep running when LIVE is disarmed, and nothing is closed automatically.
- **Exceptions:** rent reclaim opens nothing and is exempt. The smoke test keeps running with global PAPER, as before, but now needs the same checks.

No ML training, model promotion, paper profit or page refresh can arm LIVE: only `POST /api/control/live-control/arm` sets it.

### Remaining steps before you could choose to go live

Real-money execution stayed disabled in this work.

1. Decide that you want to. Momentum is not profitable in paper (50 trades, −0.515 SOL): see the forward test.
2. On the server, set in `.env`: `TRADING_ENABLED=true`, `LIVE_TRADING_ENABLED=true`, `PAPER_TRADING=false`. Then run `bash scripts/deploy.sh`. The global mode stays PAPER.
3. In Risk settings, set `max_loss_per_trade_sol` above 0 for each strategy that may trade live.
4. Set that strategy to MANUAL (approve each entry) or AUTO.
5. Fund the wallet above reserve + size + costs. It was 0.103 SOL on 2026-10-22, with a 0.05 SOL reserve.
6. Open Settings → LIVE trading and click **ON**, then confirm. The server switches only if every check passes; otherwise it lists what to fix.
7. To stop: click **OFF** (immediate), or use the kill switch.

## Phases 6–7: ML engine and monitoring

These build on what exists: the ML Training Lab, the Signal Center, ML governance and ML Review. Nothing was replaced.

### What the ML system keeps apart

| Kind | Where it lives |
|---|---|
| Training / validation data | The shadow models train on the oldest 75 % of the ledger and validate on the newest 25 %; rows whose labels were not known yet are purged. Frozen validation sets are kept per model family. |
| Out-of-sample evaluation | Holdout metrics and frozen-validation reports (`ml_validation_reports`). |
| Immutable historical predictions | Signal Center signals (`ml_signals`: a trigger refuses UPDATE and DELETE), recorded within 120 s of a decision, never backfilled. |
| Completed outcomes | `ml_signal_outcomes`, and the ledger's outcome columns, measured after the horizon. |
| Production champion | `model_versions.status = active`, set only by an operator's promotion (audited `ml.promoted`). |
| Challengers | `status = challenger`. Registered, never promoted automatically. |
| Proposed changes awaiting approval | DRAFT experiments; challengers that passed validation (AWAITING MY APPROVAL). |

### New in this change

- **Prospective shadow scores.** The ml service's 30 s signal loop now scores ledger decisions of the last 3 minutes (`shadow_ml.score_new`, with the loaded models cached). A shadow prediction now exists before its T+5m outcome.
  - Older rows scored by the training cycle are still written, but `ml.prediction_audit` counts them as RETROSPECTIVE and never evaluates them.
  - The evaluation compares the decisions the return model favoured with the rest and with all of them, after costs. That is the baseline comparison for a model.
- **ML engine monitoring** (ML Engine page, `GET /api/ml/monitor`, `yonixalpha_core.ml_monitor`), from saved records only:
  - per model: champion, challenger, shadow, training time and samples, stage, contribution, status and why;
  - experiment signals: total / evaluated / pending;
  - shadow scores, prospective vs retrospective;
  - closed PAPER trades per strategy, kept apart from predictions: win / loss / breakeven, average win and loss, profit factor, net, net per trade, drawdown, open positions, forward-test status;
  - rejected decisions and what happened after them;
  - experiments: proposed, accepted, rejected and retired, each with its verdict;
  - the ML steps' last success;
  - data-source health;
  - audit-trail counts for proposals, acceptances, rejections, retirements, promotions, retirements of champions and contribution changes.
- **Event history** (`GET /api/ml/history`): per decision, signal → decision → paper entry → exit → evaluation. **Open** shows the full timeline in the ML Training Lab.

### Plain-language states

| State | When |
|---|---|
| INSUFFICIENT DATA | Not validated yet, or the forward test lacks evidence. |
| OBSERVING | Shadow only, or an accepted experiment still collecting signals. |
| PAPER TESTING | A strategy in a forward test without a baseline. |
| VALIDATION FAILED | A challenger failed its frozen validation. |
| IMPROVEMENT NOT CONFIRMED | Not supported, inconclusive or deteriorating. |
| CHALLENGER OUTPERFORMING BASELINE | Supported forward, or improving. |
| AWAITING MY APPROVAL | A DRAFT experiment, a challenger that passed validation, or an experiment that beat its comparison. |
| CHAMPION IN USE | The operator-promoted model. |

No state is shown without a saved record behind it. Nothing trains, promotes or trades from this page; a model contributes only through ML governance's audited, stepwise, operator-only rules (LIVE_CONTRIBUTOR stays locked).

## Phase 5: large buys and wallet behaviour (`yonixalpha_core/large_trades.py`)

### The hypothesis being tested

A large buy draws other buyers, and earlier holders then sell into it. This is tested; nothing sells or buys because of it.

### Definition (`lbuy-2026.10.1`; a change gets a new version)

A **large buy** is a curve buy that meets all of these:
- at least 0.3 SOL;
- at least 2 % of the curve's virtual SOL reserves;
- at least 3 × the median buy of the 60 s before it;
- at least 3 trades in those 60 s (a quieter token cannot be measured).

There is at most one event per token per 5 min.

### How it is recorded

1. The stream marks candidates at ingest, with two comparisons per trade.
2. The discovery service checks them every 20 s and records each **EVENT** with one **CONTROL**: another active token at the same moment, with enough trades and no large buy of its own.
3. The recording runs whatever the trading mode, and a failure never stops the scanner.

**Before the moment (60 s):**
- trades, buys and sells;
- distinct buyers and sellers;
- net SOL flow;
- price move;
- virtual SOL reserves;
- token age;
- whether the large buyer is the token's creator.

**After the moment, at 30 s / 60 s / 5 min / 15 min**, from stored trades only:
- price return;
- net SOL flow;
- new buyers;
- sells by earlier holders (count and SOL);
- for an EVENT, the share the large buyer itself sold.

A row whose stored history no longer covers the window is INCOMPLETE. Rows are kept 30 days.

### The study

`$C exec -T api python -m yonixalpha_core.tools.large_buy_study --days 14`

- Pairs are split by time: the first 60 % for exploration, the last 40 % as a hold-out.
- For each horizon and measure it shows the EVENT mean against the CONTROL mean, a 95 % bootstrap interval of the difference, and the sample sizes.
- A finding needs at least 30 hold-out pairs, the same sign in both periods, and a hold-out interval excluding 0. Otherwise it says INSUFFICIENT DATA or NOT CONFIRMED.
- **"Buying right after a large buy"** is the hold-out return minus a 3 % cost **estimate**: two 1.25 % curve fees plus fixed costs on a 0.05 SOL trade. It is not a measured fill.
- **Related wallets:** only "is the creator" is known. Other relations, such as funding links, are reported as unknown, never assumed.

Data starts at deployment, so expect INSUFFICIENT DATA for the first days.

### Wallet intelligence (existing; reused, not rebuilt)

Wallet profiles (`wallet_pnl`) use FIFO realized PnL per token. Open holdings are not valued, and sells without a recorded buy are never counted as gains.

A wallet with fewer than 5 closed trades is shown as INSUFFICIENT_DATA, so one large win does not make a wallet "smart". The proven-wallet feature uses the same minimum. Launch-buyer reputation uses a causal Beta estimate that shrinks small samples.

Known limits, stated rather than hidden:
- copy delay;
- wallet clustering uncertainty;
- histories that begin with the stream;
- survivorship of tokens the stream saw.

Optional external sources (X narrative, enrichment) are off by default and fail gracefully. The on-chain stream and the ledger do not depend on them.
