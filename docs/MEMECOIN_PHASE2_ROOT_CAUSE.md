# Memecoin Refocus: Phase 2 root-cause report (2026-10-20)

This report extends `docs/YONIXALPHA_DEEP_STRATEGY_RECOVERY_REPORT.md` (Oct 15, "the recovery report") with the server evidence of 2026-10-20. Evidence labels:
- VERIFIED: shown by server output, code or git;
- INFERRED: follows from verified facts; the check that would prove it is named;
- UNKNOWN: no evidence.

Sources:
- **S4** (09:47 UTC): `git log -1`, `memecoin.sh status`, the live locks, `strategy_recovery --section book/compare/activity`.
- **S5** (10:14 UTC): `strategy_recovery --section equity --split 2026-10-15T12:00` and `--section trades` (by category).

All amounts are SOL, realized, net of every fee the simulator charged.

---

## 1. Findings that needed action first

### 1.1 The environment permitted LIVE trading. Fixed by the operator on 2026-10-20.

- At 09:47 UTC: `TRADING_ENABLED True`, `LIVE_TRADING_ENABLED True`, `PAPER_TRADING False`. All three environment locks were open; only the database `global_mode` (PAPER since Oct 10) prevented LIVE entries.
- At about 10:00 UTC the operator set `LIVE_TRADING_ENABLED=false` and `PAPER_TRADING=true` in `.env` (backup kept) and restarted the API. The API now reports `LIVE_TRADING_ENABLED False PAPER_TRADING True` (VERIFIED).
- No LIVE position has opened since Oct 10 07:25 (S4).
- The LIVE wallet account holds 0.1032 SOL.

### 1.2 Memecoin has been paused since 2026-10-17 13:16 UTC (VERIFIED, S4)

- No Memecoin container exists.
- No candidate appeared in 49 hours and no service error was logged. That is the pause, not a scanner fault.
- No open Solana paper position (S5: `open PAPER positions on this account: 0`). The 7 open PAPER positions counted by `memecoin.sh` belong to the EVM books.

## 2. The paper account over time (VERIFIED, S5)

| | SOL |
|---|---|
| Start (09-26) | 10.0000 |
| **Peak** | **10.2916 at 10-06 04:52** |
| Trough | 9.0634 at 10-17 12:22 |
| **Maximum drawdown** | **1.2282 (11.93% of the peak)** |
| End (10-17) | 9.1201 (-0.8799, -8.80%) |
| End without the single best trade (`5e6b350e`, momentum, +0.1924) | 8.9277 |

**Per period, by exit time:**

| Period | Trades | Net | Equity at the end |
|---|---|---|---|
| P0, to Oct 7 13:15 (before #53: paper did not pay LIVE fixed costs) | 108 | **+0.2704** | 10.2704 |
| P1, to Oct 9 23:29 | 13 | +0.0107 | 10.2811 |
| P2, from the Oct 9 23:29 risk save (sizes up: GLOBAL max size 2, migration lost its 0.01 cap) | 18 | **-0.3641** | 9.9170 |
| P3, from Oct 10 16:44 (#69-#71: faster promotion and re-checks) | 28 | -0.2929 | 9.6241 |
| P4, from Oct 11 08:09 (#74: migration pool detection) | 21 | -0.2867 | 9.3375 |
| P5, from Oct 15 12:00 (R2-R10 deployed) | 15 | -0.2174 | 9.1201 |

**What the "profitable period" was:**
- P0 made +0.2704 over 108 trades.
- Two trades made more than all of it: momentum `5e6b350e` +0.1924 and the migration trade "D O T F" +0.1390 (Oct 6).
  - The momentum trade most likely closed on Sep 29: that day netted +0.1957 over 5 trades. INFERRED; check with `--section top`.
- Together that is +0.3314. **Without those two trades, P0 was about -0.061 SOL.** INFERRED: the arithmetic of the two VERIFIED trades against the VERIFIED P0 net.
- In the same period, paper did not yet pay LIVE fixed costs (#53, Oct 7).

**When it turned:**
- The account peaked on Oct 6.
- The large losses start in P2, right after the Oct 9 23:29 risk save that raised sizes.
- Oct 10 alone lost 0.4487 SOL in 32 trades (momentum, average size about 0.26 SOL).
- The strategy rules did not change (`strategies/solana.py`, last changed Sep 25). **The same negative-expectancy rules traded larger, so the losses grew.**

## 3. Every trade after the R2-R10 deploy (P5, 15 trades, VERIFIED, S5)

| | Trades | Net | Notes |
|---|---|---|---|
| fresh | 6 | -0.1907 | **MEIZHU** (NEAR_MIGRATION) cost **0.3971**, trailing stop after 70 s at **-44.7%**: -0.1776. **SPDOPE** cost 0.1072, trailing stop at **-53.9%**: -0.0577. Last trade AZUMA +0.0566 (+22.1%). |
| migration | 9 | -0.0267 | sizes 0.027-0.048; 7 stop-loss exits at -6.6% to -22.2%; 2 small trailing-stop gains |

**Root cause of the P5 loss: position size, not entry selection alone (VERIFIED):**
- The planner sizes a position as `max loss / (stop distance + costs)`.
- A 0.397 SOL fresh position was planned to lose about 0.02 at its stop. It lost 0.178 because the price fell through the stop between ticks, on a thin bonding curve.
- Two of the four fresh stop exits filled at -45% and -54%.
- Migration stops (about 10%) filled at -12% to -22%.
- Paper models this gap correctly. The **sizing** assumed it away.

## 4. Results per token category, all history (VERIFIED, S5 `trades`, PAPER)

| Category at entry | Trades | Win | Net | Profit factor | Max DD | Comment |
|---|---|---|---|---|---|---|
| MIGRATED established (> 3,000 SOL mcap) | 18 | 78% | **+0.0807** | 1.56 | 0.139 | includes D O T F (+0.139); **without it -0.058**. R2 (refusing these) stays. |
| NEAR_MIGRATION, migrated while held | 8 | 88% | +0.0536 | 188.5 | 0.000 | small sample; before R4 (Oct 15) the crossing filled optimistically |
| MIGRATED pool <10m | 1 | - | +0.0005 | - | - | |
| MIGRATED pool 10-30m | 14 | 21% | -0.0117 | 0.32 | 0.013 | |
| MIGRATED pool 30m-24h | 72 | 31% | **-0.2030** | 0.52 | 0.221 | the largest group: steady losses |
| FRESH | 15 | 33% | -0.0310 | 0.67 | 0.088 | |
| NEAR_MIGRATION (not migrated while held) | 26 | 38% | **-0.2766** | 0.14 | 0.277 | includes MEIZHU -0.178 |
| MOMENTUM | 49 | 37% | **-0.4924** | 0.55 | 0.721 | average size 0.2565; off since Oct 11 |

LIVE (72 closed trades at 0.005-0.012 SOL): every category is negative; total -0.0479.

**Winners vs losers at entry (S4 compare, PAPER fresh):** losing entries came earlier on the curve (median progress 0.81 vs about 0.90) with about half the buyers (125 vs 241). This is a candidate feature to test out of sample, not a rule.

## 5. Conclusions

**Confirmed:**
1. **No strategy has a demonstrated positive net edge.** The earlier profitable period was two outlier trades (+0.33 SOL). Without them it was slightly negative. Every category that looks positive depends on one trade or has fewer than 20 trades.
2. **Deterioration = larger sizes on the same rules.** From the Oct 9 23:29 risk save, sizes rose and the losses scaled with them.
3. **Stops gap on memecoin liquidity, and sizing ignored it.** Fresh stops filled at -45% and -54%; migrated-pool stops at -12% to -22%. A position sized for a 0.02 loss lost 0.178.
4. **Discovery outages:**
   - Oct 13-14: the only Helius provider was rate-limited and its replacement was disabled (recovery report section 6).
   - From Oct 17: operator pause.
5. **The environment locks allowed LIVE** until 2026-10-20 (now locked).

**Unresolved:**
- whether the early-curve / buyer-count pattern holds out of sample (needs new paper data);
- whether any rejected candidate before about Oct 12 was a missed winner (deleted by retention; cannot be established).

## 6. What changed because of these findings (Phase 5, see `docs/MEMECOIN_UPGRADE_REPORT.md`)

- **Stop-gap sizing:** Solana positions are sized as if a stop fills at least 50% (bonding curve) or 25% (migrated pool) below entry. Under the same budget the MEIZHU position would have been about 0.04-0.06 SOL, not 0.397. Sizes only shrink.
- **`strategy_recovery --section stops`** measures the real gap on every historical stop exit, so the operator can check those defaults against all history.
- **R2 stays.** The established-token category is positive only because of one trade.
