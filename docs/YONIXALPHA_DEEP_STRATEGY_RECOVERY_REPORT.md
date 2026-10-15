# YONIXALPHA - Deep Strategy Recovery Report (Stage A: evidence and diagnosis)

Date: 2026-10-15 (UTC). Stage A only. No trading code, setting, strategy mode or deployment was changed. The only repository change is a read-only diagnostic tool (`yonixalpha_core/tools/strategy_recovery.py`). It runs inside one `READ ONLY` database transaction with a 60 s statement timeout, and it was run on the server without a deploy (piped from git).

## 0. Sources and labels

| Code | Source |
|---|---|
| **S1** | `strategy_recovery --section top/deep/book/settings/trades/compare/migration/parity/activity`, run 2026-10-15 04:12 to 04:27 UTC (operator paste) |
| **S2** | `rpc_usage --hours 1`, 2026-10-14 18:56 UTC |
| **S3** | server `git reflog` (deploy times) and `uptime` / `last` (no reboot since 2026-09-30 22:41) |
| **G** | git history of `yonixnetwork/yonix-alpha` |
| **C** | current source code (file:line) |
| **E** | earlier verified reports (`YONIXALPHA_HISTORICAL_PROFIT_AND_REGRESSION_VERIFICATION`, 2026-10-13), used as leads only |

Labels:
- **VERIFIED**: shown directly by S, G or C.
- **INFERRED**: follows from verified facts; the check that would prove it is named.
- **HYPOTHESIS**: plausible, not tested.
- **UNKNOWN**: no evidence.

Every amount is SOL. USD is not recorded per trade, so no USD figure is given.

All realized PnL is net of every fee the simulator or the chain charged. `realized_pnl = proceeds - entry_cost`, and return % = realized / entry cost including buy fees (`paper_engine.close_position`, `paper_engine.py:557-566`).

---

## 1. Executive summary

1. **The ~1000% trade exists, but it is a single trade, not a strategy.**
   - It is position `915561d1`: migration engine, token "D O T F" (`3oa5Gy38...Pump`), entered 2026-10-06 03:13:53 UTC.
   - Realized **+0.13899 SOL on 0.0100 SOL**, which is **+1389.9%**. TP2 and TP3 filled at **24.1x** the entry price.
   - The price was flat for 96 minutes, then rose 23x in under three minutes on a pool showing 1,282 SOL of liquidity.
   - Public market data (GeckoTerminal) confirms the move was real and the paper sells were executable (section 3.1, V1).
2. **Without that one trade the "profitable period" was a losing period** (S1, VERIFIED arithmetic):
   - The whole of Oct 6 was +0.1243 SOL. Without the trade it is **-0.0147** over 23 trades.
   - Migration before #53 was +0.1054 over 78 trades. Without it, **-0.0336**.
   - Migration excluding Oct 6 is -0.2980 over 69 trades (PF 0.42).
3. **No strategy shows a repeatable positive net edge.** Paper realized totals:
   - fresh **-0.0633** (44 trades);
   - migration **-0.1737** (93);
   - momentum **-0.4924** (49).
   - Paper total: **-0.7294 SOL realized** from a 10 SOL start (cash 8.69, one migration position still open; the risk engine's equity was 9.33).
   - LIVE: 72 closed trades, 8 wins, **-0.0479**.
4. **The strategy code never changed.** `strategies/solana.py` was last modified 2025-09-25 (G).
   - What changed were sizes, costs, data supply and two outages.
   - There is no older "better" strategy version to restore.
5. **Why migration entries stopped on Oct 14:**
   - At **12:29:55 UTC** (13:29 on a UTC+1 clock), one migration stop-loss lost **0.06796 SOL** on a 0.3967 SOL position.
   - Migration's own daily limit is **0.02 SOL**, so it was blocked for the rest of the UTC day: 181 candidates and about 1,700 evaluations refused.
   - The same thing happened again at 02:15 UTC today (a **1 SOL** position lost 0.1329).
   - The block is correct. The configuration contradicts itself: one planned loss (~0.094 SOL) is 4.7 times the whole daily limit (section 5).
6. **A 21.7-hour discovery outage, unrelated to risk:**
   - From **Oct 13 ~14:00 to Oct 14 11:40 UTC**, `data-solana` and `engine-solana-discovery` logged about 120 errors per hour each.
   - No new fresh or migration candidate was created for 18 of those hours.
   - It ended when the services restarted during the PR #76 deploy. The cause is unknown (check V3).
7. **Paper overstates live in specific, identifiable places:**
   - all three take-profits filled in one tick at the first pool price after a migration;
   - migrated-token entries are not re-priced for latency;
   - stops on quiet pools fill far beyond the stop.

   No paper and live trade can be matched (zero same-token pairs).

---

## 2. Deployment and configuration timeline (VERIFIED: S1 audit log, S3 reflog)

| UTC | What reached production | Trading effect |
|---|---|---|
| Sep 25 | `strategies/solana.py` final version (ac0d870, 4996a96) | signal rules for fresh, migration and momentum; unchanged since |
| Sep 26 03:46-03:49 | `solana_fresh` v1: max size **0.025**, token exposure 0.025, min stop 0.10. `solana_migration` v1: max size **0.01**, risk **2%**, daily **0.02** | the original per-engine sizing |
| Sep 27 14:54 | engine scopes converted to overrides of GLOBAL; min stop (0.10) not kept as an override, so engines follow GLOBAL 0.05 | the stop floor became 5% for every engine |
| Sep 30 10:55 / 11:47 | fresh, momentum and migration set to AUTO | |
| Oct 5 10:55 | 871e46a (#43) deployed | the code in force for the Oct 6 trades |
| **Oct 6 03:13** | **trade 915561d1 (+1389.9%)** | |
| Oct 7 12:43 | **#53** (8ad1d33): paper pays LIVE fixed costs | paper ~0.000225 SOL more per round trip |
| Oct 9 10:51 / 15:05 | #62 (LIVE drift charged to paper), #63 | |
| **Oct 9 23:29:20** | GLOBAL v11: risk 0.01 to 0.02 (the operator entered 0.05, clamped), max size 1 to 2. **Same second:** `follow_global` removed solana_fresh `max_position_size_quote` (0.025) and solana_migration `max_position_size_quote` (0.01) and `risk_per_trade_pct` (0.02) | **migration lost its 0.01 SOL cap**; fresh still capped by token exposure 0.025 |
| Oct 10 08:25 | `global_mode` = PAPER (the last LIVE buy was Oct 10 07:25) | all signals now trade the 10 SOL paper book |
| Oct 10 09:34 / 10:08 / 16:35 / 18:01 | #69 (fast promotion 2 s, event re-checks 10 s), #70, #71, #72+#73 | more frequent evaluations and entries |
| Oct 10 13:34 | GLOBAL v15: risk 0.01, max 1 | migration's 0.01 cap was **not** restored |
| Oct 10 17:05 / Oct 11 06:42 | momentum AUTO to PAPER, then OFF | |
| Oct 11 06:21 | GLOBAL v16: `max_fixed_cost_pct` 0.02 | |
| Oct 11 07:35 | #74 (0f048ab): migration pool detection after the Oct 8 pump.fun change | migration candidates return |
| Oct 11 08:08 | data retention ON (3 days) | rejected decisions before about Oct 12 are deleted |
| Oct 13 10:08 | #75 (883eb6a) | |
| **Oct 13 ~14:00 to Oct 14 11:40** | **discovery outage** (section 6) | no candidates |
| Oct 14 11:29 | #76 (b1653d8): SOL sizing, account pre-check | introduced the `Finding.decision` crash: 427 errors at 12h |
| Oct 14 13:05 | #77 (7b80b27): crash fixed | |

**Notes on the PRs and settings the brief asked about:**
- **#53, #62, #63, #74:** all reached production (reflog lines `8ad1d33`, `ed076c8`, `f4569fa`, `0f048ab`).
- **The Oct 9 23:29 save:** VERIFIED in the audit log (`risk_settings.follow_global ... removed ["max_position_size_quote","risk_per_trade_pct"]`). The code path is `safety/store.py save_for_all_engines`, before #76.
- **Period markers:** the tool's markers use 13:15 for #53 and 16:44 for #69/#71. The reflog gives 12:43 and 09:34/16:35. Trades between those times fall into the neighbouring period, which is a small effect.

---

## 3. The strongest historical winners, reconstructed

### 3.1 Position 915561d1: "D O T F", +1389.9% (VERIFIED from S1 deep)

| Field | Value |
|---|---|
| Engine / strategy | solana_migration / `post_migration_flow v1` (settings: solana_migration v4 + GLOBAL v10) |
| Token category at entry | **MIGRATED, established** (market cap at the decision 93,354 SOL; pool showing 1,282.6 SOL) |
| Launch | created and migrated within 5 s, "a bundled bond nobody else could buy" (finding INSTANT_BOND, EXECUTE). Migrated 02:46:52 |
| Discovery to decision | candidate 03:11:46 (migration event), decision 03:13:53, which is 27 minutes after migration |
| What the gate saw (before entry) | h1 pool buys 18 > sells 7; pool age 1,622 s; 20 trades in the window, 4.56 SOL volume; volatility 0.00028 (almost no price movement); top-10 holders 4.9%; no hard block |
| Plan | max_loss 0.2031 (= equity 10.156 x **2%**); size bound by **max_position_size 0.01**; stop = max(2 x 0.00028, **5% floor**) = 5%; TPs at 1R/2R/3R = +5/+10/+15% (TP3 capped at the strategy's 75th-percentile MFE) |
| Entry fill | 106.73 tokens at 9.369e-5 SOL (cost 0.0100 incl. 0.000034 fee). Paper did not charge fixed costs yet (before #53) |
| Hold | +5,380 s: TP1 40% at 9.774e-5 (+4.3%), returned 0.00417. Trailing stop raised in tiny steps for 6 minutes. **+5,931 s: TP2 and TP3 (60%) at 2.261e-3 = 24.1x**, returned 0.0724 + 0.0724 |
| Exit | take_profit_3, 04:52:44, hold 98.9 min; fees 0.00048 |
| Realized | **+0.138988 SOL**; return 13.899 = 1389.9% of the 0.0100 SOL entry cost; MFE 2320.9%, MAE -0.34% |
| Accounting check | consistent: 0.00417 + 0.07241 + 0.07241 = 0.14899 back, minus 0.0100 cost |

**Classification:** a realized paper result booked from observed pool prices. It is **not** an unrealized peak, a partial-position display or a denomination error; the units are consistent throughout.

It is suspicious as a market event:
1. **The jump:** the price went from 9.86e-5 at +5,751 s to 2.268e-3 at +5,931 s, 23x in three minutes. On a constant-product pool showing 1,282 SOL, that needs about 4,900 SOL of net buying.
2. **The pool was very quiet:** about 4.6 SOL of volume per window, so the 1,282 SOL of "liquidity" was barely traded.
3. **The paper simulator fills sells against the pool model.** It reads the quote vault balance plus `Pool.virtual_quote_reserves`, as pump-public-docs specify (`solana/pumpswap.py:205-228`). It does not check that trades actually happened at that price.

Possible explanations:
- real buying;
- a direct SOL transfer into the quote vault (it changes the price without a swap);
- a reserve read that is wrong for this pool's layout.

Status after check V1 (GeckoTerminal 5-minute candles of pool `HHWweFKz...`, run on the server 2026-10-15): **VERIFIED as a real market move.**

| Candle (UTC) | Open | High | Close | Volume |
|---|---|---|---|---|
| Oct 6 04:50-04:55 | 9.825e-5 | 2.265e-3 | 2.265e-3 | (cut off) |
| 04:55-05:00 | 2.265e-3 | 2.284e-3 | 2.284e-3 | 30.7 SOL |
| 05:00-05:05 | 2.284e-3 | 2.291e-3 | 2.291e-3 | 11.3 SOL |

- The paper sells (04:52:44 at 2.261e-3, 0.145 SOL in total) were inside a range that kept trading with real volume, so they were executable.
- This was one rare event on an instant-bond token, not something the strategy can repeat.

**Was it repeatable?**
- The other "established" migrated trades (market cap > 3,000 SOL at entry): 17 paper trades, net +0.0033. Without 915561d1, that is **-0.1357**.
- This includes ZEREBRO (`ca9e23bc`, Oct 15): market cap 250,146 SOL, pool showing 2,099 SOL, h1 volume 0.24 SOL. It was entered at the 1 SOL size cap with a 5% stop and **lost 0.1329 (-13.3%)**, 42% beyond its planned 0.0936 maximum (loss analysis flagged RISK_MODEL_FAILURE).

### 3.2 Fresh-engine winners: they made their money by being sold at the migration moment

`7a790ee6` STONKS (+70.2%) and `473d1cb6` TAPE (+57.4%) (VERIFIED, S1 deep):
- **Entry:** both bought on the bonding curve at 97.5% and 98.1% curve progress (0.025 SOL, 260 to 324 unique buyers, buy/sell volume ratio 2.35 and 1.53, about 400 trades in the window).
- **What happened next:** the curve's highest later price was only +14.5% (STONKS) and +11.3% (TAPE), at the graduation price of about 4.1088e-7.
- **The exit:** at the first tick after migration (`position_migrated`, price source `rpc:pumpswap_pool`), **TP1, TP2 and TP3 all filled in the same second** at 6.26e-7 and 6.02e-7. That is 46% and 52% above the curve's final price, 3.5 s (STONKS) and about 0 s (TAPE) after the pool appeared.

**Category correction:**
- The earlier report counted these as "fresh strategy on already-migrated tokens, 8 trades +0.0534".
- That was wrong. The tokens were **on the curve at entry and migrated while the position was held**. `PaperPosition.lifecycle` is overwritten to MIGRATED at that moment (`services/paper-trading/app/gate_manage.py:212-224`).
- The tool now labels them "-> migrated while held" (commit `bc48680`).
- Paper: about 8 such trades, about +0.054 (6 + 2 rows in S1: +0.0414 and +0.0122; STONKS and TAPE are verified individually).
- **LIVE: the same pattern occurred 5 times and won 0 times** (LIVE fresh rows "MIGRATED pool <10m" 4 trades -0.0021, "30m-24h" 1 trade -0.0001).

**Update after check V2 (GeckoTerminal 1-minute candles of the STONKS pool, run on the server 2026-10-15):**
- 13:33 candle: open 6.255e-7, high 6.584e-7, low 5.920e-7, close 6.066e-7, 39.7 SOL traded.
- The price then rose to 1.10e-6 by 13:39.
- The paper fills (6.258e-7 to 6.262e-7 at 13:33:29) were **inside the range that actually traded**. Even the minute's low was +63% above the entry, so a sell 2 to 5 s later would also have been well in profit.
- **D1 is downgraded:** for STONKS the migration-crossing exit was executable, not a simulator artifact.
- What remains open is why the 5 LIVE trades of the same kind won 0 times. Their sizes (0.003 to 0.01 SOL) made fees 5% to 10% of size, and their sell timing is not recorded per trade.
- So the realistic part of D1 is only that all three take-profits filled in one tick. In the market the price kept rising, so this cost profit rather than inflating it.

### 3.3 Momentum winners

| Position | Date | Size | Result | Hold |
|---|---|---|---|---|
| `5e6b350e` Slice | Sep 29 | 0.406 SOL | +0.1924 (+47.4%) | 28 min |
| `5fb45991` PRIMED | Oct 11 | 0.179 SOL | +0.1075 (+60.2%) | 3.7 min |
| `144d4f12` PAPER | Oct 10 | 0.051 SOL | +0.0295 (+57.8%) | 2.1 min |

- **Signals at entry** (all six momentum conditions passed):
  - price change +22% to +70% against the previous window;
  - volume acceleration 1.6x to 2.8x;
  - transaction acceleration at least 1.5x;
  - buyer acceleration 2.1x to 2.9x;
  - buy/sell imbalance 2.2 to 4.6.
- **Token state:** curve at 34% to 79%, token age 1 to 4.6 hours.
- **Exits:** all three ran TP1 to TP3 within minutes. PRIMED's paper buy was re-priced at the measured 1.97 s LIVE latency with a 0.00% move, and it survived one simulated exit failure.
- These were real curve prices from the pump stream, so I see no price artifact here.

### 3.4 Representative losers (VERIFIED, S1)

| Position | Engine | Size | Loss | What happened |
|---|---|---|---|---|
| `ca9e23bc` ZEREBRO (Oct 15) | migration | 1.0001 | -0.1329 | established token, 5% floor stop on near-zero volatility, stop filled at -13% after 104 minutes |
| `09813445` ROSE (Oct 10) | momentum | 0.4274 | -0.0989 | risk 2% era (GLOBAL v14); "buyer_stall" was already showing at entry; never went up (MFE -1%) |
| `44280778` OUTSIDE (Oct 12) | migration | 0.4694 | -0.0968 | seller acceleration at entry; exit-intel cut half; stop at -35% price |
| `0c50b1cf` VRAX (Oct 13) | migration | 0.5471 | -0.0921 | stop |
| `269a401d` (Oct 14 12:27) | migration | 0.3967 | -0.0680 | **this single stop blocked migration for the rest of Oct 14** |

The pattern: **losers are ordinary stops at risk-based sizes of 0.4 to 1 SOL**. Each one costs 4 to 13 times what the same stop cost at the original 0.01 to 0.025 SOL sizes.

---

## 4. Results per strategy and per token category (PAPER unless noted; S1)

| Group | n | Win % | Net | Avg win | Avg loss | PF | Max DD | Fees % size | Avg size |
|---|---|---|---|---|---|---|---|---|---|
| fresh_launch_flow | 44 | 48 | -0.0633 | +0.0051 | -0.0074 | 0.63 | 0.102 | 3.4 | 0.020 |
| post_migration_flow | 93 | 40 | -0.1737 | +0.0098 | -0.0096 | 0.68 | 0.354 | 2.2 | 0.063 |
| solana_momentum | 49 | 37 | -0.4924 | +0.0332 | -0.0352 | 0.55 | 0.721 | 2.6 | 0.257 |
| LIVE fresh | 22 | 18 | -0.0131 | | | 0.11 | | 6.0 | 0.005 |
| LIVE migration | 17 | 6 | -0.0135 | | | 0.02 | | 5.1 | 0.007 |
| LIVE momentum | 33 | 9 | -0.0212 | | | 0.05 | | 7.7 | 0.005 |

**Token category at entry** (fresh-engine curve entries corrected as in 3.2):

| Category | Paper n | Paper net | Notes |
|---|---|---|---|
| FRESH curve (<70%) | 11 | -0.0179 | PF 0.29 |
| NEAR_MIGRATION curve, exited on the curve | 25 | -0.0990 | PF 0.31 |
| Curve entry held through migration | ~8 | ~+0.054 | sold at the first pool price; LIVE 5 trades, 0 wins |
| MIGRATED, pool < 10 min (migration engine) | 1 | +0.0005 | |
| MIGRATED, pool 10 to 30 min | 12 | -0.0052 | PF 0.51 |
| MIGRATED, pool 30 min to 24 h | 63 | -0.1723 | PF 0.55; the bulk of migration trades |
| MIGRATED, established (mcap > 3k SOL) | 17 | +0.0033 | -0.1357 without 915561d1 |
| MOMENTUM | 49 | -0.4924 | |

**Migration recalculated** (the brief asked for it without Oct 6):

| Set | n | Net | PF |
|---|---|---|---|
| all | 93 | -0.1737 | 0.68 |
| Oct 6 only | 24 | +0.1243 | 5.98 |
| Oct 6 without 915561d1 | 23 | -0.0147 | <1 |
| excluding Oct 6 | 69 | -0.2980 | 0.42 |
| before #53 | 78 | +0.1054 | 2.54 |
| before #53 without 915561d1 | 77 | -0.0336 | <1 |
| after #53 (size about 0.34) | 15 | -0.2791 | 0.40 |
| excluding its 3 largest winners | 90 | -0.4433 | 0.17 |

**Momentum, early positive and later negative** (VERIFIED):
- **Early:** before #53 it was +0.1714 over 9 trades. Slice (+0.1924) was one of them; the other 8 lost -0.021.
- **Oct 10 to 11:** 39 trades, -0.6416. 22 stops cost -0.97 SOL in total.
- **Signals did not change** (same file).
- **What changed:**
  1. Oct 10 08:25, global mode PAPER: momentum signals that had gone LIVE at about 0.004 SOL now traded the 10 SOL paper book (INFERRED from the mode change and the trade counts: paper momentum went from 10 trades before Oct 10 to 39 on Oct 10 to 11).
  2. Risk 2% and max 2 SOL from Oct 9 23:29 to Oct 10 13:34.
  3. #69 more frequent evaluations: 28 trades on Oct 10, against about 1 per day before.
  4. 38 of 49 trades were sized by `pool_fraction` (up to the pool-fraction cap of curve liquidity): -0.6325, PF 0.24.
- **Stops:** stop distances of 19% to 26% (2 x window volatility) on 0.2 to 0.5 SOL positions mean 0.04 to 0.10 SOL per stop.

**Exit reasons across all paper trades:** stops lost -1.62 SOL (fresh -0.12, migration -0.52, momentum -0.97). TP3 and trailing exits made +0.94.

**Sample size:**
- Every category has fewer than 100 trades, and most have fewer than 20.
- The top 3 trades decide the sign of the migration result.
- No result here supports a claim of edge.

---

## 5. Why migration trading stopped (VERIFIED, S1 migration section)

| Event (they are different) | Last time (UTC) |
|---|---|
| migration candidate created | Oct 15 04:03 (the feed works) |
| migration decision stored | Oct 15 02:06 |
| approved (EXECUTE) | Oct 15 02:06 |
| position opened | Oct 15 02:06 (Scout, 0.3875) |
| position closed | Oct 15 02:15 |

Sequence:
1. **Oct 14, 11:40 to 12:29.** After the restart, migration evaluated normally. Its stored hard blocks were INSUFFICIENT_MIGRATED_LIQUIDITY, AUTO_SL_NO_VOLATILITY (no trades yet on new pools), FLOW_UNAVAILABLE and EXECUTION_UNAVAILABLE.
2. **12:27:52.** Entered CJsXsHNq at **0.3967 SOL**. The size came from equity 9.49 x 1% = 0.0949 planned loss, bound by "risk".
3. **12:29:55.** Stop: **-0.06796**. Migration's own realized PnL for the day was now -0.068, past its own limit of 0.02.
4. **12:34:59 to 23:59.** 181 migration candidates were refused before any token data was fetched (`STRATEGY_DAILY_LOSS_LIMIT`, plus 6 also in LOSS_COOLDOWN). Today's counter shows 1,725 refused evaluations; **no RPC was spent on them** (PR #76).
5. **00:00 UTC reset.** Three entries followed: ZEREBRO 1.0 SOL at 00:30, one still open since 00:49, and Scout 0.3875 at 02:06. Fresh and migration also hit **MAX_OPEN_POSITIONS** (account-wide 3): 560 refusals today.
6. **02:15.** Scout +0.0435, ZEREBRO -0.1329, so migration's own day is -0.0893. **Blocked until 00:00 UTC.**

**Answers to the questions in the brief:**
- **Migration events detected:** yes, 6 to 19 per hour.
- **Correct category:** mostly yes, but "established" tokens with a market cap above 3,000 SOL entered as if newly migrated (section 3.1).
- **Data fresh:** yes for the evaluations that ran.
- **Cooldown:** yes, briefly. LOSS_COOLDOWN is **account-wide** (`store.account_state` uses the last losing close on the account), so a fresh loss pauses migration too.
- **Daily limit:** yes, this is the cause.
- **Executable quote, signal, order:** none attempted while blocked, by design.

**The precise conflict:**
- `solana_migration.max_daily_loss_quote` = 0.02 (kept since Sep 26).
- Its planned loss per trade = equity x GLOBAL `risk_per_trade_pct` = about 9.4 x 0.01 = **0.094**.
- Before Oct 9 23:29 the 0.01 SOL size cap made the realistic loss per stop about 0.002, so 0.02 allowed roughly 7 to 10 losing trades. Since then, **one ordinary stop ends the day.**
- A persistent block cannot outlast the UTC day; it resets at 00:00 (VERIFIED by the 00:00 entries).

**Smallest safe correction (proposed, not applied; needs approval):**
- Set the per-engine override `solana_migration.max_loss_per_trade_sol` = **0.0067 SOL** (a third of the 0.02 limit). The setting exists since #76, and it lowers risk.
- With a ~14% stop plus costs, that gives sizes of about 0.035 SOL (up to about 0.08 SOL on a 5% stop), with fixed costs (0.000225) near 0.3% to 0.7% of size.
- The daily limit is not raised, nothing is reset, and LIVE stays off.
- The alternative of restoring the exact historical 0.01 cap is faithful to history, but at 0.01 SOL the post-#53 fixed costs are 2.25% of size. That is what throttled approvals on Oct 7 to 9 (STOP_INSIDE_COSTS).

---

## 6. The Oct 13 to 14 outage (VERIFIED symptoms, UNKNOWN cause)

- **Errors:** from Oct 13 14:00 UTC, `data-solana` and `engine-solana-discovery` each logged about 120 errors per hour (about 2 per minute) for 21.7 hours.
- **Effect:**
  - Discovery candidates stopped at once.
  - Migration candidates stopped after 17:00.
  - No candidates at all in 18 of the 49 hours examined.
  - The decision engine had nothing to evaluate.
- **Recovery:** it ended only when `paper-trading`, `data-solana`, `decision-engine`, `engine-solana-discovery` and `ml` were restarted at 11:39 to 11:40 Oct 14 (the #76 deploy). The server has not rebooted since Sep 30.
- **Possible earlier gap:** Oct 13 04:00 to 10:08 also had migration candidates but no discovery candidates. That ended at the #75 deploy restart (10:09).

**Update after check V3 (error kinds, run 2026-10-15 04:40 UTC):**

| Service | Event | Count | First .. last | Latest detail |
|---|---|---|---|---|
| data-solana | `rpc_health_check_failed` | 2,597 | Oct 13 05:33 .. Oct 14 11:44 | "All RPC endpoints failed for method=getHealth (env:primary: HTTP 429)"; the endpoint list starts with `SOLANA_RPC_URL` (env), `disabled: true` |
| engine-solana-discovery | `rpc_health_check_failed` | 2,594 | Oct 13 04:42 .. Oct 14 11:44 | same |
| engine-solana-discovery | `pump_stream_unhealthy` | 131 | **Oct 13 14:01 .. Oct 14 11:50** | "the stream delivered 26 of 114 launches PumpPortal announced in the last 10 min (23%)"; action "WebSocket reconnected to the next provider" |
| decision-engine | `candidate_evaluation_failed` | 427 | Oct 14 12:30 .. 12:53 | the `Finding.decision` bug, fixed by #77 |

VERIFIED: in both services the RPC health check (one `getHealth` every 30 s) failed continuously from the morning of Oct 13. The only endpoint it tried was the env backup, which answered HTTP 429.

From 14:01 the pump.fun WebSocket delivered only about a quarter of new launches. Reconnecting "to the next provider" did not repair it for 21.7 hours, and only the restart at 11:40 Oct 14 did. Discovery then had almost nothing to promote.

HYPOTHESIS, to confirm with check V5: the dashboard-configured Helius endpoints had dropped out of these two services' RPC manager, either disabled or rate-limited without recovery, or not reloaded. That left only the public backup. The restart reloaded them.

This is an availability defect: a long-running service must recover its providers without a restart (Stage B, R7).

---

## 7. Paper vs LIVE

**Shared, VERIFIED (C):**
- one strategy implementation (`strategies/solana.py`);
- one gate (`safety/gate.py assess`);
- one planner (`safety/planning.plan_trade`) and the same settings scopes;
- one exit engine (`paper_engine.manage`) with LIVE sells routed by `live_trading`;
- since #53 and #62 paper also pays the LIVE fixed costs and the measured LIVE drift;
- since the Oct 10 18:01 deploy (commit 0d2357a, in #72/#73) paper buys are re-priced at the measured LIVE latency (1.97 s, median of 67 LIVE buys).

**Intentional differences:** the separate book ("solana" 10 SOL vs the LIVE wallet of about 0.08 to 0.10 SOL), and simulated fills.

**Distorting differences, in order of impact:**

| # | Difference | Evidence | Effect |
|---|---|---|---|
| D1 | Positions that cross a migration: paper fills TP1 to TP3 in one tick at the **first pool price**, 46% to 52% above the curve's final price | 3.2; paper ~8 trades +0.054 vs LIVE 5 trades, 0 wins | overstates the fresh strategy |
| D2 | **Size scale**: paper 0.01 to 1 SOL, LIVE 0.003 to 0.012 SOL; fixed costs 2.2% to 3.4% of size on paper vs 5% to 8% LIVE | S1 strategy table | paper looks cheaper |
| D3 | Paper entry re-pricing needs pump-stream trades: it is **measured for curve tokens only**. Migrated (PumpSwap) entries keep the decision price | `paper_execution.settle_entry_delay`; fresh P3 6/6 measured | migrated entries get a zero-latency fill |
| D4 | Stops on quiet pools fill far past the stop (ZEREBRO stop 5%, fill -13%) | 3.1 | real, and LIVE would suffer it too; the risk model underestimates it |
| D5 | Simulated exit failures (2.9% to 4.2%, measured from LIVE sells) only delay a paper exit; LIVE had 6,769 failed sells, 6,706 of them `Custom 6053`, which earlier evidence ties mostly to one position | S1 parity | the LIVE tail risk is larger |
| D6 | LIVE `entry_price` is the all-in cost basis including the ~0.002 SOL token-account rent, which is refunded and re-booked later | 35 accounting flags: "price move -60%" vs realized -17% | LIVE MFE/MAE figures based on entry_price are distorted; realized PnL is correct |

- **Matching:** no token was traded in both modes within 10 minutes, so per-opportunity matching is impossible with the stored data. A forward test running a paper shadow of every LIVE decision would be needed. That is new data collection.
- **Hidden LIVE losses:** the 33 LIVE positions in status `failed` cost 0 (buys that never filled), so there is no hidden LIVE loss there.

---

## 8. Data-quality defects found (they affect review and ML, not PnL)

1. **Opportunity outcome horizons for migrated tokens use bonding-curve prices.**
   - Every migrated token gets `price_raw 0.00041088...`, the graduation price, from `pump_stream trades`.
   - So "T+5m -99.56%" (D O T F), "+197.63%" (OUTSIDE), "peak None" and "executable_return -3.0" are artifacts.
   - Every migrated decision's ML label and missed-winner statistic is affected.
2. **Category at entry was overwritten** (3.2). Fixed in the tool; the database field still changes at migration by design.
3. **Credential exposure:** decision reasons stored a provider RPC URL that **embeds a provider API key** (a third-party RPC host in a Sep 29 CREATOR_HISTORY reason). The tool printed it; it now hides every URL.
   - The key has appeared in this session's output. **Rotating it is recommended.**
   - Writers of reasons should pass errors through `redact.redact_text` (Stage B).

---

## 9. Helius usage (S2, call counts, not credits)

Calls in one hour (18,929 successful):

| Caller | Calls | Breakdown |
|---|---|---|
| decision-engine | 16,560 | getAccountInfo 6,401; getTokenLargestAccounts 3,816; getMultipleAccounts 3,816; getSignaturesForAddress 2,527 (background) |
| discovery | 944 | |
| paper-trading | 801 | getTokenAccountsByOwner 364, getBalance 182 (critical) |
| data-solana | 541 | |

- **Fresh re-evaluation dominates.** The fresh engine evaluates 1,000 to 2,000 times per hour for 20 to 30 new candidates per hour (S1 migration table), about 60 evaluations per candidate-hour, each with a mint read and a holder read. Caches save 57% (mint) and 15% (holders).
- **The hard-block pre-check works:** 2,588 entries were refused today without token data (5 cooldown or limit codes).
- **Migration evaluations of new pools** fail repeatedly on AUTO_SL_NO_VOLATILITY or INSUFFICIENT_LIQUIDITY (up to 333 per hour). Each failure still pays the full read.
- **Creator history** (getProgramAccounts) is refused by Helius, and the refusal is cached for a day (`solana/creator_history.py`), so it is not a cost.
- **Backup endpoint:** 134 calls per hour hit `SOLANA_RPC_URL` with 429s and then succeeded on Helius. No Helius credit is wasted; there is a small latency cost.
- **Not changing:** open-position pricing, balances and exits (critical class) are never shed, and none should be reduced.

---

## 10. Confirmed causes (ranked)

1. **Selection:** the profitable history rests on one unverified 24x trade (915561d1). Without it no strategy was ever net positive.
2. **Risk configuration since Oct 9 23:29:** risk-based sizes of 0.4 to 1 SOL against a 0.02 migration daily limit and a shared 3-position account. Losses became 4 to 13 times larger, and one stop blocks migration for a day.
3. **Global PAPER switch (Oct 10 08:25) plus #69 frequency:** momentum traded large paper sizes 28 times on Oct 10 (-0.64 SOL in 2 days).
4. **Discovery outages:** about 21.7 h on Oct 13 to 14 and probably about 6 h on Oct 13 morning, with no candidates at all.
5. **Paper optimism at migration crossings (D1) and fresh-token selection:** fresh curve entries exited on the curve lost (36 trades, -0.117).
6. **Established-token entries in the migration engine** with a 5% floor stop and 1 SOL size (ZEREBRO).

## 11. Unresolved questions and checks (read-only, run on the server)

Setup (same as before, once per terminal):
```
cd /opt/yonixalpha && git fetch -q origin claude/yonixalpha-platform-architecture-804nb5 && C="docker compose --env-file .env -f infra/docker/docker-compose.yml -f infra/docker/docker-compose.prod.yml" && R(){ git show FETCH_HEAD:packages/core-py/yonixalpha_core/tools/strategy_recovery.py | $C exec -T api python - "$@"; }
```

- **V1. Was the 24x move real?** 5-minute candles of the D O T F pool, priced in SOL:
  ```
  curl -s "https://api.geckoterminal.com/api/v2/networks/solana/pools/HHWweFKzhuexnvGtz9SvKS62viE2WAJBC9AVqwxD2BPs/ohlcv/minute?aggregate=5&before_timestamp=1791262800&limit=30&currency=token" | python3 -c "import sys,json,datetime;[print(datetime.datetime.utcfromtimestamp(r[0]),r[1:]) for r in json.load(sys.stdin)['data']['attributes']['ohlcv_list']]"
  ```
- **V2. The STONKS migration minute** (pool `4LgLqjHD...`, Oct 8 13:33):
  ```
  curl -s "https://api.geckoterminal.com/api/v2/networks/solana/pools/4LgLqjHDRsVHbEBADe6LssV81PKpbSMqpkSPddU8cPQr/ohlcv/minute?aggregate=1&before_timestamp=1791466800&limit=10&currency=token" | python3 -c "import sys,json,datetime;[print(datetime.datetime.utcfromtimestamp(r[0]),r[1:]) for r in json.load(sys.stdin)['data']['attributes']['ohlcv_list']]"
  ```
- **V3. Outage error kinds:** `R --section activity`.
- **V5. Which endpoints the health check saw when the outage began (names only, no URLs):**
  ```
  $C exec -T -e PGOPTIONS='-c default_transaction_read_only=on' postgres psql -U yonixalpha -d yonixalpha -P pager=off -c "select created_at, e->>'name' n, e->>'label' l, e->>'source' src, e->>'disabled' dis, e->>'rate_limited' rl, e->>'auth_failed' auth, e->>'consecutive_failures' cf, e->>'successes' ok, e->>'failures' bad, left(regexp_replace(coalesce(e->>'last_error',''),'(https?|wss?)://[^ ]+','<url>','g'),80) err from system_events, jsonb_array_elements(detail->'endpoints') e where service='engine-solana-discovery' and event_type='rpc_health_check_failed' and created_at in ((select min(created_at) from system_events where service='engine-solana-discovery' and event_type='rpc_health_check_failed' and created_at > '2026-10-13 13:55+00'), (select max(created_at) from system_events where service='engine-solana-discovery' and event_type='rpc_health_check_failed')) order by 1, 4"
  ```
- **V4. Corrected categories and paper-realism rows:** `R --section trades > /tmp/yx_tr.txt; sed -n 1,40p /tmp/yx_tr.txt; tail -n 15 /tmp/yx_tr.txt`.

Still UNKNOWN:
- Helius credits per method and the billing period (dashboard only);
- whether `Custom 6053` LIVE sell failures were one position or many;
- whether any rejected candidate before Oct 12 was a missed winner (deleted by retention).

---

# Stage B - Proposed recovery plan (nothing below is applied)

## B1. Keep (historical logic that is sound)
- The three signal rules exactly as they are (they never changed).
- The safety gate and all hard blocks.
- The migration pool detection (#74).
- The account pre-check (#76).
- Paper paying LIVE fixed costs and drift (#53, #62).
- The measured entry latency for curve tokens (0d2357a, deployed Oct 10 18:01).
- The per-strategy daily limit, and the ability to have per-engine values that survive GLOBAL saves (#76).

## B2. Changes linked to the regressions
- **The Oct 9 23:29 override removal:** a configuration change. #76 already prevents it from happening again.
- **Global PAPER plus #69 frequency for momentum:** momentum is OFF and is not LIVE-eligible (#76). Keep it OFF until B5.

## B3. Corrections, smallest first (each one needs approval)

**R1 applied 2026-10-15** (operator approval):
- At 04:49:19 a Risk Settings action stored `solana_migration` v6 "follow GLOBAL for max_daily_loss_quote" (the "Use GLOBAL" button). That removed migration's own 0.02 limit, so the effective limit became 0.5, and `max_loss_per_trade_sol` stayed 0.
- This was corrected with `tools.engine_settings` (dry run first, then `--apply`), which wrote **v7**: own values `max_daily_loss_quote 0.02` and `max_loss_per_trade_sol 0.0067`, with an audit entry.
- Proposed UI follow-up: the "Use GLOBAL" buttons sit next to the save controls and remove a limit without a confirmation. They should ask for one.

| # | Change | Kind | Risk effect | Test |
|---|---|---|---|---|
| R1 | `solana_migration.max_loss_per_trade_sol = 0.0067` (per-engine value) | setting | lowers risk | the risk summary shows planned loss <= 0.0067 and sizes about 0.03 to 0.045; 3 stops per day fit in 0.02 |
| R2 | Refuse "migration" entries whose market cap at the decision is far above a completed curve (> 3,000 SOL), or route them to a separate, disabled-by-default category | code, default OFF or REJECT | lowers risk | unit test with the ZEREBRO and D O T F snapshots |
| R3 | Stop floor for tokens whose volatility is not measurable: when volatility < 1% (no movement), use the engine's 10% floor (the Sep 26 value) instead of 5%, which also shrinks the size | code plus setting | lowers risk | planner test: ZEREBRO inputs give a smaller size |
| R4 | Paper migration crossing: after `position_migrated`, keep the position unmanaged for the measured LIVE sell latency, then fill each take-profit at the pool price at that time, one sale per tick | code (paper only) | removes an optimism | replay STONKS/TAPE timelines; a test with a price that collapses within 2 s |
| R5 | Paper entry latency for PumpSwap entries: re-price with pool trade events, which are already fetched for flow | code (paper only) | realism | test with two pool trades around the latency |
| R6 | Outcome horizons for migrated tokens: use pool trade prices, not the curve stream | code (review data) | data quality | test: a migrated token no longer gets the 0.00041088 price |
| R7 | Stream watchdog: when discovery creates no candidate for N minutes while the stream heartbeat is old, alert and restart the stream client (find the V3 error first) | code | availability | fault-injection test |
| R8 | Redact URLs in stored reasons (`redact.redact_text` where RPC errors become reasons); rotate the exposed provider key | code plus operator | security | test: no URL survives into `risk_assessments.assessment` |
| R9 | Helius: back off re-evaluation of a candidate whose last decision was a token-level hard block that needs new trades (AUTO_SL_NO_VOLATILITY, INSUFFICIENT_LIQUIDITY, FLOW_UNAVAILABLE) from 10-30 s to 60 s, still woken by a new trade event; holder cache up to `max_data_age_seconds - 10` | code | none to safety (freshness unchanged) | counters before and after; the decision path still sees data within max age |
| R10 | Make the cooldown per strategy (with the account-wide limit unchanged) | code, default = current behaviour | neutral | test: a fresh loss does not pause migration when enabled |

## B4. What remains eligible for testing
- **Migration** (`post_migration_flow v1`): PAPER only, with R1 to R3 applied, on newly migrated pools only (pool age < 24 h, market cap at the decision below 3,000 SOL).
- **Fresh** (`fresh_launch_flow v1`): PAPER only, after R4. Report curve exits and migration-crossing exits separately.
- **Momentum:** stays OFF. It may be re-tested only in PAPER at a fixed 0.025 SOL with R3, as a separate experiment that cannot use the shared migration budget (enable after R10).

## B5. Validation rule (decided before looking)
- Run chronologically, forward, in PAPER.
- At least 50 closed trades per strategy or 14 days.
- **Pass:** PF > 1.3 and net > 0 after costs, **excluding the single best trade and the single best day**, with max drawdown < 3 x the strategy's daily limit.
- The migration-crossing share of the profit must be reported.
- No LIVE consideration until it passes, and then only with your explicit approval.

## B6. Rollback
- R1 and other settings: the previous `risk_settings` version is kept; restore it from the Risk Settings page (or "follow GLOBAL").
- Code changes ship disabled by default where they change behaviour. Each one is a separate PR, so a revert of one PR plus `scripts/deploy.sh` returns the system to today.
- No migration is destructive and no history is deleted.

## Final answer
- **The ~1000% trade:** real in the paper records (+1389.9%, +0.139 SOL on 0.01 SOL), but it is one trade on an "established" instant-bond token. Public market data confirms the move was real (V1).
- **Without that trade,** no strategy was ever net profitable. The system did not lose a working strategy; the strategy code never changed.
- **What changed:**
  - position sizes (Oct 9 23:29 and Oct 10 08:25);
  - one-stop-per-day migration limits;
  - two discovery outages;
  - paper fills that are kinder than LIVE at migration crossings.
- **Migration stopped** because one 0.068 SOL loss exceeded its 0.02 SOL daily limit at 12:29:55 UTC.
- **The smallest safe correction** is R1 (per-trade loss limit of 0.0067 SOL for migration). It lowers risk, changes no limit, and needs your approval.
