# Memecoin Refocus: Phase 2 root-cause report (2026-10-20)

This report extends `docs/YONIXALPHA_DEEP_STRATEGY_RECOVERY_REPORT.md` (Oct 15, "the recovery report") with the server evidence of 2026-10-20 09:47 UTC. It changes no trading code or setting. Evidence labels:
- VERIFIED: shown by server output, code or git;
- INFERRED: follows from verified facts; the check that would prove it is named;
- UNKNOWN: no evidence yet.

Sources:
- **S4:** `git log -1`, `scripts/memecoin.sh status`, the live-lock values, and `strategy_recovery --section book --section compare --split 2026-10-15T12:00` plus `--section activity`. All were operator pastes from 2026-10-20 09:47 to 09:48 UTC.

---

## 1. Findings that need action before anything else

### 1.1 The environment permits LIVE trading (VERIFIED, S4)

```
TRADING_ENABLED True  LIVE_TRADING_ENABLED True  PAPER_TRADING False
```

- `safety/store.py live_trading_permitted()` is true when `TRADING_ENABLED` and `LIVE_TRADING_ENABLED` are true and `PAPER_TRADING` is false. All three conditions hold now, so the **environment locks are open**.
- Today the only remaining barrier is the database `global_mode`. It was set to PAPER on Oct 10 (recovery report section 2). No LIVE position has opened since Oct 10 07:25 (S4 book: the last LIVE entry is `10-10 07:25:18`).
- The brief requires live trading disabled by default. `.env.example` ships `TRADING_ENABLED=false`, `LIVE_TRADING_ENABLED=false`, `PAPER_TRADING=true`.
- **Recommended operator action before any Memecoin resume:** set `LIVE_TRADING_ENABLED=false` and `PAPER_TRADING=true` in `.env`. These keys are server-only; neither the dashboard nor `set-keys.sh` changes them.
- The LIVE wallet account still holds funds: `live_solana` start 0.0829, cash 0.1032 SOL (S4).

### 1.2 The Memecoin system has been stopped since 2026-10-17 13:16 UTC (VERIFIED, S4)

- `Memecoin: PAUSED since 2026-10-17T13:16:50Z by root`.
- No Memecoin container exists.
- The activity section shows **no candidate in any of the last 49 hours** and no service error. That is the pause (no worker), not a scanner fault.
- No paper evidence has accumulated since Oct 17 12:42 (the last paper exit).
- The paper book has no open Solana position. `memecoin.sh` counts 7 open PAPER positions across all chains. The Solana book (S4) lists only closed and failed rows, so those 7 belong to the EVM paper books (`evm_bsc` and `evm_robinhood` cash is below their start). They stay frozen until a resume. INFERRED; the new `equity` section prints the Solana open count directly.

## 2. Paper results now (VERIFIED, S4 book; SOL, net of every fee charged)

| Engine (PAPER) | Closed trades | Realized | At Oct 15 04:27 (recovery report) | Change since |
|---|---|---|---|---|
| solana_fresh | 49 | **-0.25400** | 44, -0.0633 | +5 trades, **-0.1907** |
| solana_migration | 105 | -0.13352 | 93, -0.1737 | +12 trades, +0.0402 |
| solana_momentum | 49 | -0.49241 | 49, -0.4924 | none (off since Oct 11) |
| **Total** | 203 | **-0.87993** | -0.7294 | -0.1505 |

- The account started at 10 SOL; cash is 9.1201 = 10 - 0.87993, consistent with no open Solana position.
- LIVE (all closed): 72 trades, -0.04786 SOL; 33 failed entries (no fill).
- **Fresh lost 0.1907 SOL in 5 trades**, an average of -0.038 SOL per trade.
  - That is more than the whole 0.025 SOL fresh token-exposure cap recorded in the recovery report (section 2).
  - So fresh positions after Oct 14 were larger than 0.025 SOL. INFERRED: a trade cannot lose much more than its cost. The SOL-denominated sizing of #76 (Oct 14) is the likely change.
  - Which trades, at what size and with which exit reason is UNKNOWN until the `equity` section below is run. It lists every trade closed after the marker.
- Migration gained 0.040 SOL over 12 trades in the same window. That is a small sample; no conclusion.

## 3. Winners vs losers at entry (VERIFIED, S4 compare; medians, PAPER)

| Group | n | Median curve progress | Median buyers | Median top-10 share |
|---|---|---|---|---|
| fresh WIN (incl. 2 big wins) | 22 | 0.90-0.98 | 241-292 | 0.215-0.226 |
| fresh LOSS | 26 | **0.81** | **125.5** | 0.219 |
| migration WIN | 39 (+1 big) | - | 14 | 0.209 |
| migration LOSS | 65 | - | 13 | 0.237 |
| momentum WIN | 16 (+2 big) | 0.74 | 29.5 | 0.232 |
| momentum LOSS | 30 | 0.67 | 22 | 0.231 |

Read with care: these are medians of small groups, and the wins are counted by number, not by size. The pattern is consistent across engines: **losing fresh entries were earlier on the curve (median 0.81 vs about 0.90) with half the buyers.** This is a candidate feature for Phase 5, to be tested out of sample. It is not a rule to apply now.

**"MIGRATED established" (market cap above 3,000 SOL at the decision):**
- PAPER: 13 wins and 1 big win vs 4 losses (18 trades).
- R2 (deployed Oct 15) refuses these entries for `solana_migration`. That rule came from a few large losses (recovery report sections 3 and 5).
- With a 14/18 win count, R2 may be refusing a profitable category. The net SOL of that group is not in the compare section. **Check:** `--section trades`, the "by category" lines. R2 stays as is until that number is known (no change without evidence).

## 4. Questions from the brief (§3), updated

| # | Question | Answer | Label |
|---|---|---|---|
| 1-2 | Strategy and code in the profitable period | rules from `strategies/solana.py` (unchanged since Sep 25), code `871e46a` on Oct 6 | VERIFIED |
| 3 | Start, peak, end, drawdown | start 10.0000, end 9.1201 realized. Peak and maximum drawdown come from the new `equity` section (section 6). | VERIFIED / pending |
| 6 | When performance changed | It never was positive without trade `915561d1` (+0.139 SOL, Oct 6). From Oct 15 to 17 fresh lost a further 0.19 SOL in 5 trades. | VERIFIED |
| 7 | Discovery stopped, or trades unprofitable? | Oct 13-14: discovery stopped (provider rate limit, recovery report section 6). Oct 14: migration blocked by its daily limit. Since Oct 17: everything stopped by the operator pause. Trades lost money throughout. | VERIFIED |
| 8-9 | Realism of fills and costs | paper optimism corrected by R3-R5 (Oct 15); paper paid LIVE fixed costs from Oct 7 | VERIFIED |
| 10 | Provider changes | pump.fun change Oct 8 (fixed Oct 11), Helius rate limit Oct 13 | VERIFIED |

## 5. Confirmed, probable, unresolved

**Confirmed:**
- No strategy has a demonstrated positive net edge in paper. The only large profit is a single trade.
- Discovery outages had a known external cause (Oct 13-14) and an operator pause (Oct 17 onward).
- The environment currently permits LIVE trading.

**Probable:**
- Early fresh entries (lower curve progress, fewer buyers) lose more often. Needs out-of-sample confirmation.

**Unresolved:**
- the 5 fresh trades from Oct 15 to 17 (sizes, exits);
- the net SOL of "MIGRATED established" (and so whether R2 is right);
- whether any rejected candidate before Oct 12 was a missed winner (deleted by retention; cannot be established).

## 6. Next read-only check (after this tool change is on `main`)

This runs the updated tool straight from git; no deploy is needed. It prints:
- the equity curve (peak, drawdown, per period, per day);
- every trade closed after Oct 15 12:00;
- the per-category nets.

The two commands are in the operator instructions of the PR that adds this report.
