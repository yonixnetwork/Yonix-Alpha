# Momentum strategy: research, root cause, upgrade and controls (2026-10-20)

Evidence labels: **VERIFIED** (read from code or recorded data), **INFERRED** (reasoned from verified facts), **OPEN** (not yet established). Nothing below says the strategy is profitable: the evidence says it has not been.

---

## 1. What momentum is (VERIFIED, code)

| Step | Where | What it does |
|---|---|---|
| Pre-filter | `engine-solana-discovery/app/funnel.py` `momentum_prefilter` | Bonding-curve tokens (not migrated) older than `momentum_min_age_seconds` (30 min), or younger but at least 70 % of the curve sold and not taken by the fresh engine. Active in the last 2 min, at least `min_trades_in_window` trades in 5 min, trades up at least 1.5x against the previous 5 min. Stream data only, no RPC. |
| Gate | `decision-engine/app/gate_eval.py` with `assemble_fresh(engine="solana_momentum")` | The full safety gate (authorities, holders, liquidity / impact, creator history, manipulation, account limits, risk plan). |
| Signal v1 | `strategies/solana.py` `momentum_signal` | Six checks, current 5 min window vs the previous one: price up and faster than before; volume x1.5; trades x1.5; unique buyers x1.3; buyers growing at least as fast as sellers; buy/sell volume 1.3. All six must pass. **Unchanged since Sep 25.** |
| Exits | `paper_engine` / exit plan | The shared plan: volatility stop (2 x window volatility, floor 10 %), TP1-TP3, trailing after TP1, exit intelligence. Nothing momentum-specific. |
| LIVE | `safety/gate.py` | `LIVE_SUSPENDED_ENGINES = {"solana_momentum"}` refused every LIVE momentum entry in code (Oct 13). **Removed in this change** (section 5). |

**What v1 does not check:**
- how far the price has already run (late entry);
- whether the buying comes from many wallets or a few;
- whether buyers are thinning while the price still rises;
- distribution, or creator sells inside the window.

## 2. Root cause of the historical losses

### 2.1 What is already established (VERIFIED, `docs/YONIXALPHA_DEEP_STRATEGY_RECOVERY_REPORT.md` sections 3.3, 4; server data Oct 15)

| Set | n | Net SOL | PF |
|---|---|---|---|
| PAPER momentum, all | 49 | -0.4924 | 0.55 |
| PAPER before #53 (Oct 7) | 9 | +0.1714 | one trade (Slice +0.1924); the other 8 lost -0.021 |
| PAPER Oct 10-11 | 39 | -0.6416 | 22 stops cost -0.97 |
| LIVE momentum | 33 | -0.0212 | 0.05 (sizes about 0.005 SOL; fees 7.7 % of size) |

**What changed between the good and the bad period** was not the strategy. The code was the same file throughout. The changes were:
1. **Global mode PAPER (Oct 10 08:25).** Signals that had traded LIVE at about 0.004 SOL now traded the 10 SOL paper book.
2. **Risk 2 % with a 2 SOL maximum (Oct 9 23:29 to Oct 10 13:34).**
3. **#69 event re-checks.** 28 trades on Oct 10, against about 1 a day before.
4. **38 of 49 trades sized at the pool-fraction cap:** -0.63 SOL, PF 0.24.

Stops of 19-26 % (2 x window volatility) on 0.2-0.5 SOL positions cost 0.04-0.10 SOL each.

**Conclusion (INFERRED from the above):**
- The "better" period is one outlier trade.
- The v1 signal never showed an edge.
- The losses scaled with size and frequency, not with a code change.

### 2.2 What is not yet established, and the tool that establishes it (OPEN)

The brief asks, for every losing and winning trade, what qualified it:
- the market before entry;
- whether momentum was accelerating or exhausted;
- whether buyers were independent;
- liquidity;
- late entry;
- creator / holder sells;
- the exit;
- whether the loss came from the strategy, the execution or the paper model.

The decision-time evidence for this is stored. Each traded decision keeps its full gate snapshot in `opportunity_outcomes` (retention keeps it):
- the 1 m / 5 m returns;
- volume, buyer, seller and new-wallet accelerations;
- flow state;
- buyer breadth;
- the curve state;
- creator sells;
- manipulation and manufactured-pump flags.

Afterwards the ledger also records:
- the price path to T+60 m, with executable round trips;
- the loss class;
- the post-exit classification.

`yonixalpha_core.tools.momentum_research` reads all of it, read-only:

| Section | Answers |
|---|---|
| `trades` | Per trade, one line:<br>- the features as recorded **at the decision**;<br>- the v2 checks re-run on them;<br>- MFE / MAE, hold, exit reason;<br>- the ledger's loss class (`DATA_FAILURE`, `EXECUTION_QUALITY`, `RISK_MODEL_FAILURE`, `LIQUIDITY_COLLAPSE`, `MARKET_REVERSAL`, …) and post-exit class.<br>Trades whose intelligence was not recorded are shown as such, never reconstructed. |
| `compare` | Winners vs losers on those decision-time features: medians, distributions, and how often each v2 check would have failed. |
| `replay` | Every recorded momentum decision with a complete outcome (traded or not, plus WATCH rows):<br>- v1 vs each v2 check and combinations;<br>- scored on the ledger's **executable** return (0.05 SOL reference, pump fee both legs, curve impact, 2 s latency, network costs) at T+60 s / T+5 m / T+15 m;<br>- chronological: the first 60 % selects, the last 40 % is out of sample;<br>- two threshold sweeps choose on the first part only. |
| `exits` | By exit reason: net, median MFE, give-back (MFE - realized), post-exit classification. |

**Run it after the deploy (section 8).** Its output is the root-cause evidence per trade. The results are in section 2.3.

### 2.3 What the server data shows (VERIFIED, `momentum_research` run 2026-10-20 22:26 UTC, read-only)

**Coverage.**
- 82 closed momentum positions: LIVE 33, PAPER 49. Entries from 2026-09-27 to 2026-10-11 (momentum has not traded since).
- Decision-time intelligence is recorded for 75 of 82. The other 7 (the first trades, Sep 27-28, and one on Oct 10) cannot be explained from decision-time data; they are not reconstructed.
- Qualified-wallet participation is UNKNOWN on every row (not measured for momentum tokens).
- Replay: 2105 recorded gate decisions, 129 qualified by v1, executable T+5m known for 1641. Out of sample starts 2026-10-03 17:34 UTC.

**Net results.**

| Mode | Trades | Net SOL (sum by exit reason) |
|---|---|---|
| LIVE | 33 | -0.0213 |
| PAPER | 49 | -0.4923 |

**Root cause by group.**

| Group | What happened | Class |
|---|---|---|
| LIVE, all 33 | Median MFE about -40 % on stops, -20 % on TP2: the price "never went up" because the cost basis includes everything the wallet paid (`live_trading.py:404-406`). At about 0.004 SOL per buy, fees and the new token-account rent are 30-110 % of the size. The trade starts deep under water before the market moves. | EXECUTION_QUALITY (size too small for its fixed costs), not a signal finding |
| PAPER stops, 22 | -0.9695 SOL, median MFE -1.3 %: the price fell from the first second. 0.02-0.10 SOL each. | SIGNAL_FAILURE / BAD_ENTRY: the strategy entered at a local top |
| PAPER winners | TP3 4 trades +0.3809, TP2 4 trades +0.0600, trailing 11 trades +0.1331. Median give-back 15-21 % of the move. | The exits worked; the entries did not feed them enough winners |
| PAPER intel exits, 6 | -0.0716 SOL | MARKET_REVERSAL mostly |

**Correction (2026-10-21, `tools.pnl_reconcile`, see `LIVE_CONTROL_AND_MOMENTUM_DIAGNOSTICS.md` §4b):**
- The rent deposits were reclaimed and are already in the booked −0.0212 SOL. They were not a loss.
- About 20 % of the LIVE loss is consumed fees; the rest is the market.
- Against the market fill price, the median MFE was about +2.5 % and about a third of the trades never rose at all. The −40 % figure above is against the cost basis.

Creator sells were not a cause: `creator_sold` was True on 1 of 61 losers. No trade shows a failed price feed in its loss class (`DATA_FAILURE` does not occur).

**Winners vs losers (21 vs 61, decision-time medians).** The two groups look almost the same:
- breadth 0.70 vs 0.71, top-3 share 0.72 vs 0.73, 1-minute return +5.8 % vs +5.9 %;
- winners had a **higher** 5-minute return (+36 % vs +27 %), so an exhaustion cap points the wrong way;
- winners had higher volume (1.29 vs 0.83) and buyer (1.06 vs 0.86) acceleration, lower seller acceleration (0.60 vs 0.73), were closer to the high (-13 % vs -20 %), further along the curve (75 % vs 67 %), and slightly larger and younger.
These differences are small at n = 21 and are hypotheses only.

**Replay, executable return of a 0.05 SOL reference trade.**

| Variant | T+5m selection | T+5m out of sample |
|---|---|---|
| every decision (context) | n=1012, mean -4.86 %, PF 0.28 | n=629, mean -4.21 %, PF 0.33 |
| v1 baseline | n=36, mean -7.83 %, PF 0.13 | n=62, mean -5.60 %, PF 0.46 |
| v1 + exhaustion (0.5) | n=32, -6.65 %, PF 0.16 | n=54, -7.23 %, PF 0.20 |
| v1 + decay | n=21, -9.48 %, PF 0.05 | n=28, +1.80 %, PF 1.24; **-74.9 without the best row** |
| v1 + independent_buyers | n=8, -9.20 %, PF 0.11 | n=14, +0.27 %, PF 1.02; **-121.4 without the best row** |
| v1 + anti_dump | n=29, -7.29 %, PF 0.13 | n=52, -5.15 %, PF 0.47 |
| v1 + liquidity | n=34, -7.77 %, PF 0.13 | n=60, -5.63 %, PF 0.47 |
| v1 + exhaustion + anti_dump | n=27, -6.26 %, PF 0.16 | n=45, -5.81 %, PF 0.25 |

- v1 picks **worse** than the average recorded decision in both periods, at T+60s, T+5m and T+15m.
- No check helps in both periods. The two positive out-of-sample rows (decay, independent buyers) are one outlier each and lose in the selection period.
- The exhaustion sweep chose 0.2 on the selection period; out of sample it gave n=25, -6.92 %, PF 0.07, worse than v1. This is the overfitting the chronological split is there to catch.
- The breadth sweep had no value with n >= 10.

**Conclusions (INFERRED from the above).**
1. The momentum losses come from the strategy's entries (no edge after costs) and, for LIVE, from a size too small for the fixed costs. They do not come from a broken price feed or the paper model.
2. No v2 check is justified by the data. All stay off; the defaults are not changed.
3. A B5 pass is not expected with v1 or any tested variant. Running PAPER would add data but should not be read as a path to profit.
4. Momentum stays OFF with WATCH monitoring, which records outcomes without trading. A future hypothesis (for example, volume and buyer acceleration with lower seller acceleration, near the high) must be stated before it is tested on WATCH data collected after today.

## 3. Research (GitHub repositories read from source, 2026-10-20)

PumpPortal, Dexscreener, Jupiter and Solana documentation could not be fetched: this environment's network policy blocks those hosts (403 at the proxy). GitHub was reachable, so the four repositories were read directly. Nothing was executed.

| Repo | Licence | What it does (code) | Evidence for it | Fit / limits |
|---|---|---|---|---|
| [charllysb/pumpfun-momentum-bot](https://github.com/charllysb/pumpfun-momentum-bot) | MIT | Entry on the first cross of +20 % market cap over creation, at least 8 buyers, within 90 s of launch. Anti-dump: drop if -15 % before the trigger. Exit: hard stop -12 %, trailing 13 % armed at +12 %, 25 s inactivity exit. | Its own README: simulated runs "positive", **live negative**. No data in the repo. | A launch chaser (our fresh engine's territory). Its live failure is latency against a fixed 2 % slippage model, which our latency re-pricing already addresses. |
| [SalahMessaoud/mako](https://github.com/SalahMessaoud/mako) | MIT | Dexscreener momentum: **reject a 5 m change > 50 %**, 5 m change ≥ 3-15 %, buy ratio ≥ 0.55, volume and liquidity ≥ $10k. On-chain momentum: curve growth rate, buyer uniqueness (unique / buys ≥ 0.3). Exits: 8 s stop confirmation, catastrophic stop x3, stale exit after 10 min within ±7 %, 30 min time exit. | None published. A code comment says its Dexscreener boost source was disabled for a "~30 % win rate". | **Exhaustion cap** and **buyer uniqueness** adapted as v2 checks. Its stale / time exits are candidates for the exit analysis. |
| [slightlyuseless/memecoin-trading-bot](https://github.com/slightlyuseless/memecoin-trading-bot) | MIT | Momentum example: score ≥ 80, ≥ 5 unique buyers, buy/sell ratio ≥ 2, price ≥ +5 %. Largest buyer ≤ 60 % of buy volume. **Exit on creator sell.** | None. | **Concentration** (largest-buyer share) and **creator sell** adapted (independent-buyers and anti-dump checks). |
| [JJnuts/solana-pumpfun-gem-finder](https://github.com/JJnuts/solana-pumpfun-gem-finder) | **None** (no reuse permission) | Signals only: cross-sectional z-scores; "do-not-chase" reversal gate (high ≥ 1.5x first and price < 0.7x high); confirmation after a 10-35 % pullback. | None; a bug the reading found would stop its revival alerts. | Concepts only (pullback vs continuation is recorded as an entry type). No code used. |

**Common to all four:**
- No reproducible backtest.
- No out-of-sample result.
- Two report negative or unproven live results.
- Their thresholds are starting points, never fitted values.

**The four conditions the brief separates** are now separate in our records:
1. **Historical momentum.** The token's run-up: the curve state and distance from the high, recorded.
2. **Momentum at detection.** The v1 checks plus the decay check.
3. **Still enterable at an executable price.** The gate's impact / liquidity rules, and the ledger's executable return.
4. **Enough upside after costs.** The replay scores executable return after fees, impact and latency, not the mid-price move.

## 4. What changed in the strategy (this PR)

**v1 is kept, unchanged.** v2 adds six checks (`strategies/momentum_v2.py`):
- every check is evaluated on every momentum decision and recorded (`momentum_v2` in the decision evidence and the ledger snapshot);
- a check decides **only when switched on** on the Strategies page;
- **all are off by default**, so the decision is still exactly v1;
- an enabled check can only take a v1 qualification away, never grant one;
- a check without its input (UNKNOWN) blocks when enabled.

| Check | Type | Rule (default) | Source of the idea |
|---|---|---|---|
| exhaustion | hard: REJECTED | 5 m return ≤ 50 % (too late otherwise) | mako |
| decay | WAIT | price up in the last minute while buyer or new-wallet acceleration < 1 | brief §4A/B |
| independent_buyers | WAIT | buyer breadth ≥ 0.5 and top-3 buyers ≤ 60 % of the last minute's buy volume | memecoin-trading-bot, mako |
| anti_dump | hard: REJECTED | no DISTRIBUTION / DETERIORATION flow, no creator sell, sellers not accelerating faster than buyers, no HIGH manufactured-pump pattern | memecoin-trading-bot, gem finder |
| liquidity | WAIT | curve real reserve ≥ 5 SOL | brief §4C (the gate's impact rules already apply) |
| qualified_wallets | WAIT | ≥ N qualified wallets buying | **not measured for momentum tokens yet**: always UNKNOWN, so enabling it blocks every entry. Kept so the gap is visible. |

**Explainable states:**

| State | Meaning |
|---|---|
| WATCH | v1 not met yet |
| WAIT | a confirmation is missing |
| QUALIFIED | v1 met and every enabled check passed |
| REJECTED | a hard check failed |
| STALE | no current data |

The **score** (the share of known checks passing) is an explanation only and never decides. Safety findings are applied by the gate separately and cannot be outweighed.

**Version:**
- the signal carries `1` (v1 only) or `2:exh+dmp…` (the enabled checks);
- it is stored with every decision and position, so results can be split by version and rolled back by switching checks off.

**Exits:** unchanged in this PR. The `exits` section measures give-back and post-exit behaviour by exit reason first. Changing exits without that would be guessing.

**ML:**
- No new model in this PR.
- The momentum dataset, with decision-time features, executable outcomes, chronological splits and the threshold sweeps, is the replay above.
- The existing shadow / champion ML (never allowed to lift a block) stays as it is.
- A model is only worth training once the replay shows the features carry information out of sample.

## 5. Web-app control of momentum (replaces the hidden suspension)

| Mode | What happens |
|---|---|
| OFF | No new momentum positions, no entry orders. Open positions keep their stops, take-profits and exit management; **switching OFF sells nothing**. With `monitor_when_off` (default on), tokens that pass the pre-filter are evaluated from the stream alone (no RPC) and recorded as **WATCH** outcomes, so evidence keeps accumulating. |
| PAPER | Simulated positions on the paper account, with the same costs, latency re-pricing and risk rules as the other strategies. Never a real transaction. |
| AUTO | Automatic entries without approval. **Real orders only** when the global mode is LIVE, the server's environment locks are open (`TRADING_ENABLED`, `LIVE_TRADING_ENABLED`, `PAPER_TRADING=false`) and the live worker is ready. Otherwise it runs on PAPER and the dashboard says why. Switching to AUTO needs an explicit confirmation: you type AUTO, and the server refuses without `confirm_auto`. |

**Implementation:**
- **Stored** in `strategy_configs`, the table the gate, the funnel and the position loop read on every pass. Changes apply without a restart, and the mode survives restarts.
- **Validated server-side and audited** (`strategy_mode.changed` with the previous and new mode, the user and `auto_confirmed`). Both mode endpoints enforce the same rule.
- **`LIVE_SUSPENDED_ENGINES` was removed.** Momentum LIVE is now governed by the same mode and global locks as every other strategy. The global live locks are unchanged.
- **The Strategies list** shows "Execution now" for every strategy.
- **The Momentum page** shows: mode, execution now, real-money authorization, version, the six checks with their thresholds, decision counts, and results (all, by lifecycle, by entry type, with net without the best trade and the best day). It also shows recent decisions with their state and reasons, and the scanner pipeline.
- **Emergency stop** (separate from the mode, typed confirmation):
  - "Emergency stop": OFF; positions stay managed.
  - "Emergency stop and sell momentum positions": OFF plus an exit request for each open momentum position, sold through the normal exit path. Audited as `strategy.emergency_stop`.

## 6. Scanner reliability

Unchanged this round, and already in place:
- PumpPortal reconnect with backoff;
- `stream_guard` gap fill;
- RPC failover and hot reload;
- the outage alarm;
- Helius account-level pre-checks and back-off;
- one shared stream for all strategies (momentum adds no connection; its OFF monitoring reads the same Redis stream).

Shown on the Momentum page and System Health: the scanner pipeline panel (last activity per stage, STALE / DISCONNECTED / DATA GAP, RPC 429 share, PumpPortal disconnects). X stays optional and cannot block.

**Not added:** a failure-injection test with providers down (OPEN, as in the upgrade report).

## 7. Acceptance gate (unchanged, B5)

Forward PAPER, at least 50 closed trades or 14 days. The pass rule:
- PF > 1.3 and net > 0 after costs, **excluding the single best trade and the single best day**;
- max drawdown below 3x the strategy's daily limit.

**No LIVE consideration before a pass, and then only with explicit approval.** A pass is evidence for more testing, not proof of future profit.

## 8. Operator steps

1. Deploy (this PR changes the API, decision-engine and discovery images).
2. Run the research tool (read-only) and send the output:
   ```
   cd /opt/yonixalpha && docker compose --env-file .env -f infra/docker/docker-compose.yml -f infra/docker/docker-compose.prod.yml exec -T api python -m yonixalpha_core.tools.momentum_research
   ```
3. Done 2026-10-20 (section 2.3): no check helped out of sample, so none was switched on. Momentum stays OFF with WATCH monitoring.

## 9. Limitations and open questions

- **Root cause per trade is answered in section 2.3** for the 75 trades with decision-time data. The 7 without it stay unexplained. No check helped; the sample (129 v1-qualified decisions, 82 trades) is small and its decisions overlap.
- **Qualified-wallet participation is not measured for momentum tokens.** The wallet intelligence covers launch buyers.
- **Exits are unchanged pending the exit analysis.**
- **The replay is limited by the ledger:**
  - it uses fixed-horizon executable returns of a 0.05 SOL reference trade, not the strategy's own exits;
  - overlapping decisions on one token are not independent.
- **Official API documentation** (PumpPortal, Dexscreener, Jupiter, Solana) could not be read from this environment; the facts used come from the code that already integrates them.
