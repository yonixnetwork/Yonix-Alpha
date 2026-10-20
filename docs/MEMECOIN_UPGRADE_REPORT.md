# Memecoin Intelligence and Profitability Recovery: report (2026-10-20)

This report answers the deliverables of the operator's brief (section 16). Details and evidence:
- `docs/MEMECOIN_REFOCUS_AUDIT.md`: Phase 1 audit and gap matrix.
- `docs/MEMECOIN_PHASE2_ROOT_CAUSE.md`: the root cause, with the server evidence of 2026-10-20.
- `docs/YONIXALPHA_DEEP_STRATEGY_RECOVERY_REPORT.md`: the Oct 15 forensic report and the R2-R10 changes.

Live trading stays locked. The environment locks are closed since 2026-10-20 (`LIVE_TRADING_ENABLED=false`, `PAPER_TRADING=true`). Nothing in this work can place a real order.

---

## 1. Root cause (summary)

1. **There was no profitable strategy to lose.**
   - The paper account peaked at 10.2916 SOL on Oct 6.
   - The period before Oct 7 (+0.2704 over 108 trades) came from two trades: +0.1924 (momentum) and +0.1390 (migration "D O T F"). Without them that period was about -0.06.
   - Every category that looks positive depends on one trade or has fewer than 20 trades.
2. **The decline was larger sizes on the same losing rules.**
   - From the Oct 9 23:29 risk save, sizes rose: the GLOBAL maximum went to 2 SOL and migration lost its 0.01 cap.
   - The next 67 trades lost 0.94 SOL (P2-P4).
   - The strategy code has been unchanged since Sep 25.
3. **Stops gap through their price on thin memecoin liquidity, and sizing assumed they would not.**
   - After Oct 15, a 0.397 SOL fresh position planned to lose about 0.02 at its stop lost 0.178 at -44.7%. Another fresh stop filled at -53.9%.
   - Migrated-pool stops (about 10%) filled at -12% to -22%.
4. **Discovery stopped twice for operational reasons.**
   - Oct 13-14: the only Helius provider was rate-limited and its replacement was disabled.
   - From Oct 17: the operator pause.
5. **The environment allowed LIVE trading** until 2026-10-20.

Maximum drawdown of the paper account: **1.2282 SOL (11.93% of the peak)**. End: 9.1201 SOL.

## 2. Code and configuration changes (this upgrade)

| Area | Change | Files |
|---|---|---|
| Product focus | Memecoin is the main menu and start page. Crypto / exchange trading is archived (menu only; code, schema, keys and worker unchanged; snapshot branch `archive/crypto-exchange-trading-2026-10-20`). Strategies and Smart Wallets are in the menu. | `apps/web/components/archivedEngines.ts`, `apps/web/app/dashboard/layout.tsx`, `apps/web/app/dashboard/page.tsx`, `apps/web/app/dashboard/archived/page.tsx` |
| Risk (§11) | **Stop-gap position sizing**, Solana only: the size assumes a stop fills at least `sizing_stop_gap_curve_pct` (0.50, bonding curve) or `sizing_stop_gap_pool_pct` (0.25, migrated pool) below entry. The stop level is unchanged; sizes only shrink; both values are editable on Risk Management, and 0 turns them off. The plan records `sizing_loss_pct` and `planned_loss_at_gap`. Under FIXED_SOL with policy REJECT, a fixed size whose gap loss exceeds `max_loss_per_trade_sol` is refused with the reason. | `safety/settings.py`, `safety/planning.py`, `safety/gate.py`, `risk-settings/page.tsx` |
| Scanner reliability (§5) | **Scanner pipeline health**: for each stage (on-chain stream, PumpPortal, token discovered, candidate promoted, gate decision, paper trade), the time it last actually did something, in the states CONNECTED / STALE / DISCONNECTED / DATA GAP. Also the gate wake-queue depth, PumpPortal counters and disconnects, the last hour's RPC outcomes (ok, HTTP 429 with its share, failed, failover retries), and on-chain coverage of PumpPortal launches. API `GET /api/system/scanner-health`; panel on System Health and on the Dashboard. | `yonixalpha_core/scanner_health.py`, `routes/system.py`, `components/ScannerPipeline.tsx` |
| Wallet intelligence (§6) | **Smart Wallets refreshed without copy trading**: while copy-engine is off (SOLANA_ONLY), the ml service rebuilds the Solana wallet profiles every 6 h (`WALLET_PROFILES_INTERVAL_SECONDS`) with the existing bounded `rebuild_solana`. It is gated by operating mode and host resources, and skipped while copy-engine runs. Research only: nothing is copied. | `services/ml/app/main.py` |
| Strategy comparison (§12-13) | **Strategy Comparison** API and page: every closed Solana position, PAPER and LIVE apart, by strategy, by token category and both. Shows trades, win rate, net, **net without the best trade**, average win / loss, profit factor, max drawdown, fees, size, hold, best / worst token, and whether the sample is large enough (20 trades). Time windows: all, 7 d, 3 d, 24 h. | `yonixalpha_core/strategy_comparison.py`, `routes/strategies.py`, `dashboard/strategy-comparison/page.tsx` |
| Forensics (§3, §12) | `strategy_recovery --section equity`: the paper equity curve, peak, drawdown, without the best trade, per period / day, every trade after the last marker. `--section stops`: planned stop vs realized loss on every stop exit (p75 / p90 / worst), to check the gap defaults against all history. | `tools/strategy_recovery.py` |

## 3. Best strategy out of sample

**None qualifies.**
- No strategy or category has at least 20 trades with a positive net that survives removing its single best trade.
- No strategy is recommended for continued use as "working".
- Paper testing continues so evidence can accumulate under the new sizing.

## 4. Wallet-intelligence methodology

Existing and verified in code:
- **P/L:** FIFO wallet P/L (`wallet_pnl.py`), with cost basis from on-chain buys.
- **Validation** (`wallet_validation.py`) needs a minimum of trades, closed trades, active days and weeks, unique tokens and history coverage. It also checks profitable-period share, profit factor, maximum drawdown and the best trade's share of gains. A wallet with incomplete history is `INSUFFICIENT_DATA`, never "profitable".
- **Launch-buyer reputation and dump clusters:** `wallet_intel.py`, `wallet_graph.py`.
- **Behaviour labels:** SNIPER, SCALPER, POSSIBLE_BOT, CONTRACT (`ml/wallet_labels.py`).

New: the profiles now refresh without copy trading.

**Not yet built:**
- the follower-delay simulation per qualified wallet trade ("what if we entered N seconds after this wallet");
- a qualified-wallet confirmation strategy.

Examples from verified data cannot be given until the profiles have been rebuilt on the server with Memecoin running.

## 5. Fresh vs migrated vs momentum (PAPER, all history; from `docs/MEMECOIN_PHASE2_ROOT_CAUSE.md` section 4)

| Category | Trades | Net SOL | PF | Verdict |
|---|---|---|---|---|
| FRESH | 15 | -0.031 | 0.67 | no edge (small sample) |
| NEAR_MIGRATION | 26 | -0.277 | 0.14 | losing |
| MIGRATED pool 30m-24h | 72 | -0.203 | 0.52 | losing |
| MIGRATED established | 18 | +0.081 | 1.56 | -0.058 without its best trade; refused by R2 |
| MOMENTUM | 49 | -0.492 | 0.55 | losing; off since Oct 11 |

The same breakdown is now on the Strategy Comparison page, computed live.

## 6. Scanner reliability

**Original failures (VERIFIED):**
- 21.7 h on Oct 13-14: provider rate limit plus a disabled replacement.
- 49 h+ since Oct 17: the operator pause. No errors were logged.

**Fixes:**
- R7 (Oct 15): outage alarm after 5 min, repeated hourly, and a warning when the change would leave only the env backup or a disabled provider.
- This upgrade: the pipeline panel shows each stage's last real activity, so a silent stop is visible within minutes (stale after 60 s for the stream, after 5 min for discovery).

The existing reconnect-with-backoff (PumpPortal), stream gap fill (`stream_guard`), RPC failover and hot reload are unchanged.

**Not verified on the server:** whether discovery continues while PumpPortal, DexScreener or X fail. The code paths exist, but this round added no failure-injection test.

## 7. Before / after paper performance

| | Before (to Oct 17) |
|---|---|
| Start | 10.0000 |
| Peak | 10.2916 (Oct 6) |
| End | 9.1201 |
| Net | -0.8799 (-8.80%) |
| Max drawdown | 1.2282 (11.93%) |
| Trades | 203 closed |

**After:** no data yet. Memecoin has been paused since Oct 17, and the stop-gap sizing is not deployed yet. The "after" period starts when the operator deploys and resumes. It will appear on Strategy Comparison (last 24 h / 3 d / 7 d) and in `strategy_recovery --section equity --split <deploy time>`.

**Expected effect of the sizing change:** each position is about 2-10x smaller for the same planned loss on curve tokens. Profit potential falls with it. Nothing here predicts a profit.

## 8. Tests, limitations, open issues

**Tests:** all suites run locally (results in the PR). New tests cover:
- gap sizing: 8 tests;
- the scanner-health API: 2;
- the comparison API: 1;
- the wallet-profile loop: 3;
- equity and stops: 3.

Existing fixed-cost / fixed-SOL tests now pin the gap off explicitly, because they test the loss at the stop. Gap behaviour has its own tests.

**Limitations:**
- Gap defaults come from a small sample (2 curve stops, 7 pool stops after Oct 15). Run `--section stops` on the server to check them against all history.
- Smaller sizes make fixed LIVE costs a larger share; LIVE stays locked.

**Open (not built this round):**
- the replay harness (old vs new rules on the same recorded snapshots);
- the follower-delay simulation;
- qualified-wallet and pullback strategies (shadow);
- the X with / without comparison;
- a failure-injection test for discovery with optional providers down;
- a walk-forward ML report per strategy.

## 9. Archived exchange modules

**Archived from the active menu:**
- Strategy Center, Engine (Strategy Lab), Backtests, Compare, Portfolio Risk, Exchange Trades, Exchange Connections, Alerts and Audit;
- APIs `/api/crypto/*` (still served for the archived pages);
- worker `crypto-trading` (unchanged; it exits by itself while its engine is off).

**Older archive:** Binance futures, MT5/forex and the grid were removed from the stack on Sep 29 (branch `archive/legacy-futures-forex-grid-2026-09-29`).

**Dormant references** to those engine names remain in `safety/settings.py`, `gate.py` and `execution_router.py`. No running service produces them.

The active dashboard is memecoin-focused (verified in a browser).

## 10. Readiness

**Ready for continued PAPER testing** once the operator deploys this change and resumes Memecoin.

**Not live-ready.** No strategy has a demonstrated edge, and the brief requires explicit approval for live trading in any case.
