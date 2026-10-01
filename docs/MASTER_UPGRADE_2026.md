# Master multi-chain upgrade — audit and gap matrix (2026-09-30)

Scope: the "MASTER MULTI-CHAIN TRADING, SNIPING, COPY-TRADING, WALLET
INTELLIGENCE, ML & RELIABILITY UPGRADE" prompt (sections 0–84), audited
against the code on `main` at 84d6b86 and the production evidence pasted from
the server on 2026-09-30.

Status words:

| Word | Meaning |
|---|---|
| DONE | implemented and tested in code; production evidence where stated |
| PARTIAL | part of the requirement exists; the gap is named |
| MISSING | not implemented |
| NOT VERIFIED | implemented, but not proven on the real chain / with a real transaction |

Nothing here is LIVE VERIFIED on BSC or Robinhood Chain: no authorized EVM
transaction has been sent. Solana live execution exists (Pump.fun / PumpSwap)
and is unchanged by this upgrade.

## 1. Production evidence already collected (2026-09-30)

| Chain | Launchpad | Evidence (launchpad_verify, data-evm) |
|---|---|---|
| BSC | Four.meme | ACTIVE, DISCOVERY (807 launches / 1 h), EVENTS (1233 trades), QUOTE (buy+sell), LIQUIDITY PASS; discovery live, 730 trades / 30 min |
| BSC | Flap | discovery live, 19,129 trades / 30 min; verify run hit an RPC timeout (retry added in 84d6b86) |
| Robinhood | Pons V2 | ACTIVE, DISCOVERY (308), EVENTS (14,601), QUOTE, LIQUIDITY, MIGRATION_DETECTION PASS; 3,181 trades / 30 min |
| Robinhood | Pons V1, NOXA, Odyssey curve / instant / reflection | ACTIVE PASS; DISCOVERY FAIL: 0 launches in 1 h, and Odyssey reflection 0 in 24 h (855k blocks). Inactive venue or wrong event set: undecided (M1 raw-log check) |
| BSC / Robinhood | all | BUY, SELL, TX_MONITORING: NOT RUN (need a real authorized transaction) |

RPC: BSC logs served only by bsc-rpc.publicnode.com (bsc-dataseed* refuse
eth_getLogs); Robinhood has one public RPC that rate-limits (~1 req/s after
pacing). Section 51/52's "do not use public RPC as the sole production path"
is therefore **not met today** — a keyed provider is needed (Settings →
Providers), see M7.

## 2. Requirement matrix

| § | Requirement | Status | Evidence / gap | Phase |
|---|---|---|---|---|
| 1 | Preserve working Solana / Pump.fun / PumpSwap execution | DONE | no change to `solana/live_exec.py`, PumpSwap or the Solana gate in this upgrade | — |
| 2–3 | Research Jul–Sep 2026, repository records | PARTIAL | `MULTICHAIN_AUDIT_2026.md` §1, `PUMPFUN_EXECUTION_RESEARCH.md`, `SCANNER_INTELLIGENCE_2026.md`; the new repos in §7–12 not yet recorded | M9 |
| 4 | Only Solana, BSC, Robinhood in the active UI | DONE | legacy futures/forex/grid removed (archive branch) | — |
| 5 | Launchpad health: activity status, last launch/trade/migration, 7d counts, verified flags | DONE in code (M1), NOT VERIFIED in production yet | `chains/activity.py`, table `launchpad_activity` (migration 0024), rollup written in `evm/store.persist_scan`, `/api/launchpads`, Launchpads page; `tests/test_launchpad_activity.py`, `test_control_center` | M1 |
| 6 | 7-day inactivity → INACTIVE, hidden from active filter, adapter kept, auto-reactivation | DONE in code (M1) | INACTIVE needs 7 days without activity AND 7 days of monitoring (else UNVERIFIED); Active / Archived tabs; discovery keeps scanning, so activity returns the venue to ACTIVE; Solana trade counts are "not tracked" (None), never 0 | M1 |
| 7 | Solana launchpads beyond Pump.fun/PumpSwap (LetsBONK, LaunchLab, Meteora DBC, Bags, Moonshot, Jupiter Studio) | MISSING | registry has `pumpfun`, `pumpswap` only | M10 |
| 8 | BSC: Four.meme, Flap verified; Genius.fun etc. researched | PARTIAL | Four.meme / Flap adapters, discovery live, read-only checks PASS; others not researched | M10 |
| 9 | BSC mempool wallet copying | MISSING | copy engine reads confirmed trades only; no pending-tx stream | M8 |
| 10 | Robinhood: Pons, NOXA, Odyssey | PARTIAL | adapters exist; only Pons V2 proven active | M1 |
| 11 | Pons coordinated-launch safety (privileged / creator-linked / common-funder / simultaneous buyers) | DONE (paper; on-chain assumptions NOT VERIFIED until coordination_check runs on the server) | launch_coordination: 13 detections, configurable NO_TRADE / REDUCE_SIZE / MANUAL_APPROVAL / NONE, data-evm entries + EVM copy buys; see section 13 | M5 |
| 12 | Robinhood reference repos inspected | PARTIAL | pons-launch-engine and pons-terminal read for M5 (section 13); the other five still M9 | M9 |
| 13 | Robinhood sequencer feed (+ delayed feed fallback), latency / gaps measured | MISSING | registry note only | M8 |
| 14–17 | Observation state machine for every token on all chains, windows T0..T+60, expiry, stored | DONE (EVM, paper); Solana PARTIAL (own state names, see section 15) | EVM: `evm_observations`, full state machine, T0/T+5/T+10/T+20/T+30/T+60 snapshots with the §16 fields, adaptive MIGRATED / MOMENTUM windows, EXPIRED_NO_ENTRY, entries only while observed; Solana: `token_observations` + follow-ups, T+20m added | M6 |
| 18–23 | Wallet performance model: 24H–180D windows, avg/median win and loss, profit factor, drawdown, FIFO ledger, INSUFFICIENT DATA | DONE in code for BSC / Robinhood (M3); Solana PARTIAL | `wallet_pnl.py` (FIFO lots, usually earns / usually loses, profit factor, drawdown, holds, best / worst), windows 24H / 7D (14D+ INSUFFICIENT DATA: 7-day profile history, 14-day trade retention); fees listed not subtracted (NOT VERIFIED per launchpad), gas not included; Solana profiles have no sells (launch_buyers) and say so | M3 |
| 24 | Nansen / MadeOnSol enrichment | MISSING | | M11 |
| 25–28 | Wallet discovery, validation gates, outlier test, regime test | DONE for BSC / Robinhood (M3b); Solana INSUFFICIENT DATA (no sells recorded) | outlier test (M3); `wallet_validation` (12 configurable checks, per-day consistency, INSUFFICIENT DATA vs NOT VALIDATED); `market_regimes` (hourly volume / net flow / price range, migration 0026; CONSISTENT / REGIME_DEPENDENT); discovery stage COLLECTING_HISTORY → VALIDATED → PAPER_FOLLOWED / REJECTED, never auto-copied; Smart Wallets UI + rules editor. External smart-money sources (§24-25 Nansen, MadeOnSol) not connected | M3b |
| 29 | Copy BUY ONLY / SELL ONLY / BUY+SELL | DONE (paper) | modes NOTIFY, BUY_ONLY, MIRROR (buy+sell), SELL_ONLY (M4); SELL ONLY exits PAPER positions only | M4 |
| 30–31 | Copy buy checks, chase guard; sell 20/50/100 % replication | DONE (paper) | `copy-engine`, partial sells on Solana (queued) and EVM | — |
| 32 | Copy position link fields | DONE (paper) | `copy_outcomes.link`: source wallet / tx / position, our position, ratio, mode, target vs our entry and exit, latency, displacement, PnL; slippage None for paper (measured on live fills only); on `/api/copy/positions` and the Copy page | M4b |
| 33 | Copy latency stages on dashboard | DONE (paper) | detection / analysis / risk / decision / execution / total (ms) on the Copy page; build / sign / submission / landing / confirmation are None and labelled live only (no live copy); the target's own submit time is not observable from confirmed trades | M4b |
| 34–35 | Copy safety never overridden; paper copy with would-have-won / missed | DONE (paper), NOT VERIFIED in production | safety enforced; every target buy (copied, skipped, notify-only) gets a paper outcome after 60 min (`copy_events.outcome`, migration 0025): simulated entry / exit, result, best / worst move, class COPIED / MISSED / BLOCKED_BY_SAFETY / FILTERED_BY_SETTINGS / NOT_COPYABLE / NOTIFY_ONLY; NO_PRICE_DATA instead of 0 % | M4b |
| 36–44 | ML: wallet behaviour, mistake labels, frozen validation set, staged contribution, champion/challenger, no look-ahead, paper as training data | PARTIAL | Solana ML: multi-target shadow models, champion/challenger, labels, contribution 0 until validated, no-look-ahead audit; missing: wallet-behaviour labels (§37), EVM features, BUY/WAIT/REJECT/SELL/HOLD comparison (§41) | M12 |
| 45 | Manual BUY/SELL on all chains | PARTIAL | Solana only (`manual_trade.py`); EVM manual paper missing | M13 |
| 46–47 | Automatic-vs-manual sell diagnosis with stage-level evidence | DONE in code (M2); production result pending the server run | `tools/exit_diagnosis.py` (read-only report from `execution_orders` + position timeline + reconciliation); `tests/test_exit_diagnosis.py` | M2 |
| 48–53 | Provider dashboard, roles, plan health / UPGRADE REQUIRED | DONE (routing + reporting); mempool / sequencer streaming is M8 | roles per endpoint (dashboard, .env and public), role-preferred routing on Solana and EVM with counted fallbacks, operator-stated plan, WSS stored for BSC / Robinhood, plan health from observed limits on RPC / Data Providers and System Health; see section 16 | M7 |
| 54–55 | Token explorer all chains, explorer links per chain | PARTIAL | Solana token pages; EVM page lists tokens; link builder per chain not audited | M14 |
| 56–58 | Balances, gas reserve, INSUFFICIENT GAS, unified wallet (Solana + EVM accounts) | PARTIAL | Solana live wallet panel; EVM wallet module (`chains/evm/wallet.py`) read-only; gas-reserve NO_TRADE not wired for EVM paper | M13 |
| 59–61 | PnL always shown with colour, market cap $K/$M | PARTIAL | Solana positions show PnL; USD market cap done for Solana (G3); EVM positions page not audited | M14 |
| 60 | NO EMOJIS | DONE (this phase) | alert prefixes and the live page tick mark removed | M0 |
| 62–63 | 24/7 server-side workers | DONE | all engines are containers; dashboard is a viewer | — |
| 64–66 | GitHub / provider update monitor with Telegram + System Health | MISSING | | M15 |
| 67–70 | Multiple detection methods, source priority, NO_TRADE on provider failure | PARTIAL | NO_TRADE on unavailable data holds on both chains; single detection path per chain | M8 |
| 71–75 | Paper trading all chains feeding ML | PARTIAL | Solana complete; EVM paper entries exist, not yet ML features | M12 |
| 76–77 | Safety hierarchy, decision states EXECUTE / REDUCE_SIZE / WAIT / MANUAL_APPROVAL / REJECT / NO_TRADE | PARTIAL | Solana gate implements the hierarchy; decision words differ (PROMOTE/REJECT/...); MANUAL_APPROVAL not implemented | M6 |
| 78 | Preserve historical data | DONE | migrations are additive only | — |
| 79–81 | Test matrix, automatic-sell regression, 24/7 acceptance | PARTIAL | automatic-vs-manual exit regression added (`services/paper-trading/tests/test_exit_parity.py`); 24/7 acceptance (§81) is an operator procedure on the server, not automated | M2 |
| 82–83 | Final requirement audit and report | this document, updated per phase | | every phase |

## 3. Phase plan (smallest safe steps, evidence first)

| Phase | Content | Why this order |
|---|---|---|
| M0 | this audit; emoji removal | cheap, required by §60 |
| **M1** | Launchpad health + 7-day rule + raw-log check for the five quiet Robinhood adapters | the pasted evidence already shows five adapters with no activity; the dashboard must not present them as active |
| **M2** | Automatic-vs-manual sell diagnosis from existing `execution_orders` data; regression suite | §46 says diagnose before changing execution; the data already exists |
| M3 | Wallet P/L model: win/loss averages & medians, profit factor, drawdown, windows, outlier test, INSUFFICIENT DATA | the copy engine depends on it |
| M4 | Copy SELL ONLY, position link fields, missed/would-have-won outcomes | |
| M5 | Pons / EVM launch-window coordination checks | Pons is the most active Robinhood venue |
| M6 | EVM observation state machine | |
| M7 | Provider roles and plan-capability health | public RPCs are the current bottleneck |
| M8 | Robinhood sequencer feed, BSC pending-tx evaluation | |
| M9–M15 | research records, more launchpads, enrichment providers, ML extensions, EVM manual trading and gas, explorer/PnL UI audit, update monitor | |

EVM live execution (signing, nonces, submission) stays off and locked until
a launchpad is paper-verified and the operator explicitly authorizes a smoke
test.

## 4. M1 — launchpad health (2026-09-30)

- `activity_status` per launchpad: ACTIVE (activity in 24 h), QUIET (in 7 d),
  INACTIVE (none for 7 d after at least 7 d of monitoring), UNVERIFIED (no
  activity yet, under 7 d of monitoring), DEGRADED (EVM discovery cursor
  older than 15 min), DISABLED (registry inactive or operator OFF).
- Separate from the verification status; neither replaces the other.
- EVM activity comes from a daily rollup written in the same transaction as
  the stored events; only newly inserted launches / trades / migrations are
  counted, so re-scans after a restart do not double-count, and the rollup
  survives the 14-day `evm_trades` pruning.
- Solana: Pump.fun launches from `token_observations`, PumpSwap migrations
  from `token_events`; Solana trades per venue are not stored and are shown
  as "not tracked".
- The rollup starts empty on deploy: a Robinhood venue with no activity shows
  UNVERIFIED until 7 days of monitoring have passed, then INACTIVE.

## 5. M2 — automatic vs manual sells (2026-09-30)

Audit (code, before any change):
- Automatic and manual exits use ONE path. Dashboard SELL
  (`/api/trade/.../sell`), paper/position exit, CLOSE POSITIONS /
  EMERGENCY EXIT and the copy engine only set `exit_requested` and write a
  timeline event (`operator_exit` / `copy_exit_requested`); the Solana
  position loop (every 2 s; RPC-priced positions every 5 s, except that a
  requested exit is always due) calls `live_trading.manage_live_position`,
  which queues the SELL through `request_live_exit` exactly like a stop loss.
  The order worker (`process_order`) treats every SELL identically.
- Per order the system already records: decision time, created / signed /
  confirmed times, attempts, slippage (raised by `exit_slippage_step_pct`
  per failed exit), minimum output, route, final stage, error, RPC calls.

Added (no execution change, because no evidence points at a component yet):
- `tools/exit_diagnosis.py`: per origin (MANUAL / COPY / AUTOMATIC) the
  confirmed share, latency split (trigger → order, order → signed, signed →
  confirmed, total; median / p90 / max), positions that needed a second
  sell, failure stages and errors, slippage, minimum-output use, routes, and
  the count of positions whose tokens left the wallet outside YonixAlpha
  (a sell made in another wallet app has no order to compare).
- Regression (§80): `test_exit_parity.py` runs a stop-loss exit and a
  dashboard exit under identical conditions; both must create one full SELL
  with the same route, slippage and limits, close the position from the
  fill, update PnL and never need a second sell. Both pass.

Result: in code the two paths are identical, so a production difference
must come from runtime conditions (timing, quote age, slippage after
failures, RPC). The server run of `exit_diagnosis` decides which; only then
is a component changed.

### M2 production result (server, 2026-09-30, last 30 days)

| Origin | Sells | Confirmed | Median total | Needed a 2nd sell |
|---|---|---|---|---|
| AUTOMATIC | 72 (33 positions) | 97.2 % | 3.1 s | 1 |
| MANUAL | 3 | 100 % | 10.1 s | 0 |

Automatic sells are not slower: manual ones wait for the next position-loop
tick after the click (trigger → order median 1.1 s, max 10.5 s vs 13 ms).
Signing (0.3 s) and landing (2.4 s median) are the same for both.

The two failed automatic sells, traced with the orders, timeline and
reconciliation rows:
- MCASH, `Custom 3012` in simulation (17:43:40): reconciliation found the
  tokens gone from the wallet 27 s later (sold outside YonixAlpha).
  Simulation correctly refused a sell that could not fill; nothing sent.
- NEAR, `Custom 6005` on chain (22:24:58): a take-profit went to the Pump
  bonding curve after it completed (Pump BondingCurveComplete). The position
  moved to PumpSwap only at 22:25:15, when its price source changed; the
  PumpSwap sell then confirmed. **Fixed**: that rejection now moves the
  position to PumpSwap at once (`live_trading.curve_complete_rejection` /
  `switch_to_pumpswap`), the retry keeps the same slippage, and a migrated
  curve position is priced from its pool, never from the stale curve
  (`gate_manage.price_position`). Tests: `test_exit_parity.py`.

## 6. M3 — wallet profit and loss (2026-09-30)

- `wallet_pnl.py`: FIFO cost basis per token; a closed trade is one token's
  matched lots. Wins and losses are reported separately ("usually earns" /
  "usually loses": average, median, % and largest), with win rate, realized
  PnL, ROI, profit factor, max drawdown (cumulative realized PnL in exit
  order), average / median hold and best / worst trade.
- Outlier test (§27): total PnL with and without the best trade and the top
  3 trades, the best trade's share of gains, and a dependence level.
- Never shown as zero when missing (§23): fewer than 5 closed trades,
  windows longer than the retained history, open holdings (not valued),
  sells with no recorded buy (not counted as profit) and the fee / gas
  limits are all named in `reasons` / `notes`.
- Rebuild is bounded: the 2000 most active wallets per chain, their trades
  loaded 100 wallets at a time (it used to load every trade of 7 days at
  once; BSC records close to a million launchpad trades a day).
- Smart Wallets page: a detail row per wallet with the P/L blocks and the
  window table.

## 7. M4 — copy SELL ONLY (2026-09-30)

- New mode `SELL_ONLY`: a target's buy is never copied (`SELL_ONLY_TARGET`).
  When the target sells a token we already hold, the same fraction of our
  own open position is queued for exit (a target selling 40% of its bag
  queues 40% of ours; the fraction is capped at 100%).
- The fraction needs the target's holding before the sell. EVM: the sum of
  its recorded buys minus sells of that token; Solana: its trades in the
  observed stream. If the holding was never observed (tokens received by
  transfer, or bought before the retained history) nothing is guessed: the
  event is skipped with `TARGET_HOLDING_UNKNOWN`.
- The copy engine only queues the exit (plan key `copy_partial_exit`); the
  service that owns the position fills it on its next management pass
  through its normal exit path (data-evm `manage_pass` for EVM paper
  positions), then clears the plan and records `copy_partial_exit_filled`.
- PAPER only. LIVE positions are never selected by SELL ONLY; the copy
  controls (`blocked_by(..., "copy")`) still apply; the kill switch does not
  block it because it only reduces exposure.
- NOT VERIFIED in production yet: needs a SELL_ONLY target whose wallet
  sells a token we hold in paper.

## 8. BSC event coverage check (2026-09-30, server)

Every log the Four.meme and Flap contracts emitted over ~1500 blocks was
counted by event type, and a closed block range was compared with the
database:

- Stored equals on chain: Four.meme 37/37 trades and 53/53 launches, Flap
  2540/2540 trades and 82/82 launches; no `discovery_gap_skipped` in 24 h.
- Undecoded events identified by their signature hash: Four.meme
  `TokenPurchase2` / `TokenSale2` (one per trade, a second event of the same
  trade); Flap `FlapTokenCirculatingSupplyChanged` / `FlapTokenProgressChanged`
  (one per trade) and `TokenCurveSetV2` / `TokenDexSupplyThreshSet` /
  `TokenVersionSet` (one per launch). None is a missed trade. A few Flap
  events (one about as frequent as trades) remain unidentified; none matches
  the trade or launch counts.
- Missing: Flap graduation `LaunchedToDEX(token, pool, amount, eth)` was not
  decoded (graduations were only found by polling getTokenV8Safe, so the
  activity rollup showed 0 Flap migrations). Now decoded as a migration;
  layout confirmed from a real log (all fields in data; 200M tokens and
  ~89.29 BNB per graduation) and covered by a test built from that log.
- RPC: publicnode answered 403 even for single blocks right after a burst of
  log requests, and the client then skipped eth_getLogs on it for 30
  minutes (BSC's only public logs endpoint). A node that already served
  logs now gets a short doubling cooldown instead.
- Four.meme graduation: the community integration (four-meme-community/
  four-meme-ai) lists the same TokenManager2 address and the same
  `LiquidityAdded(base, offers, quote, funds)` event we decode. On the server
  none of the 6,392 Four.meme tokens tracked had graduated by its own
  contract state (stage DEX = 0), so no graduation was missed. Flap: 48
  graduated tokens, 44 of them found by state polling before the
  LaunchedToDEX fix (no event time).

## 9. M4b — copy link, latency stages, paper copy outcomes (2026-10-01)

- Link (§32): `copy_outcomes.link` builds each copied position's link from
  rows already stored (copy_events, copy_positions, paper_positions), so it
  also covers positions opened before this change. Prices are native per
  whole token. Displacement = our entry against the target's. Slippage is
  None for paper (the paper fill is the executable quote at decision time).
- Latency (§33): `decision` stage added; build / sign / submission /
  landing / confirmation are None (never 0) and listed as live only.
- Outcomes (§35): the copy engine evaluates every target BUY 60 minutes
  after it was seen, from trade prices of the same token (EVM: evm_trades,
  kept 14 days; Solana: the pump stream, kept 3 hours, so a Solana event
  not evaluated within ~2h45 becomes NO_PRICE_DATA). Entry: first trade
  after we saw it. Exit: the target's own sell for MIRROR targets, else the
  last trade at the horizon. Before our fees, price impact and gas.
  Evaluated once (`outcome_at`). It never changes a decision.
- Copy page: open / closed copy positions with link columns and a detail
  row; "Paper copy outcomes" table per target and per class; outcome column
  on copy events; full latency stage line. Rendered locally with seeded
  data (no console errors); NOT VERIFIED with real target wallets.

## 10. RPC alerts seen in Telegram (2026-10-01)

- `[data-solana] rpc_health_check_failed`: the 30-second getHealth check
  in data-solana and the three Solana engines sent every failure straight
  to Telegram, bypassing the alert throttle every other service uses. It now
  goes through `notify.alert_error` (once per 5 minutes per event, with the
  number suppressed); every failure is still stored as a SystemEvent.
  The failure itself is the endpoints: in the reported snapshot the
  SOLANA_RPC_URL Helius key had 0 successes in 88 calls (all HTTP 429),
  Chainstack 0 / 20 (HTTP 403), Ankr 0 / 2 (getHealth not supported,
  timeouts), dashboard Helius 44 % with 7.7 s latency; only Alchemy was
  healthy (96.6 %).
- `[data-evm] robinhood.<launchpad>.discovery_failed: all cooling down`:
  Robinhood Chain has no ROBINHOOD_RPC_URLS configured, so data-evm uses the
  single public endpoint, which rate-limits. A cooldown loses nothing (the
  cursor resumes; a backlog over 60 minutes is skipped and alerted). Such
  RPC-unavailable failures are now alerted once they persist 2 minutes
  (per launchpad and per chain); any other discovery error is alerted at once.

## 11. One place for every chain's RPC (2026-10-01)

- RPC & Data Providers now takes BSC and Robinhood Chain endpoints as well as
  Solana (same table, `rpc_providers.chain`; URLs encrypted; admin password
  to add or change a URL). data-evm and copy-engine apply the list on the
  configuration revision (no restart). Order: dashboard (150) → .env
  BSC_RPC_URLS / ROBINHOOD_RPC_URLS (500+) → built-in public (900+); .env and
  public endpoints can be reordered or disabled; a chain never ends up with
  no endpoint.
- EVM TEST CONNECTION (`chains/evm/rpc_registry.test_evm_rpc`, also used by
  `tools/evm_rpc_probe`): chain id must match; eth_getLogs over 10 / 100 /
  1000 / 2000 blocks of the chain's busiest launchpad contract, ending 5
  blocks under the head. The old probe asked a dead Four.meme V1 contract up
  to the exact head and reported the working publicnode as NO LOGS.
- The EVM client now remembers the eth_getLogs span each endpoint accepted
  (a 10-block free tier is no longer asked 2000, 1000, ... first on every
  call) and retries twice that after 50 answers.
- Provider research (2026-10-01, from provider pages / docs via search):
  Alchemy and QuickNode both serve Solana, BNB Smart Chain and Robinhood Chain
  mainnet. Free tiers limit eth_getLogs (Alchemy free: 10 blocks on BNB and
  Robinhood; QuickNode free trial: 5 blocks); paid plans lift it (Alchemy Pay
  As You Go: unlimited on BNB and Robinhood; QuickNode paid: 10,000 blocks).
  Exact per-chain URL formats are copied from the provider's dashboard, not
  built by this app (NOT VERIFIED here).

## 12. M3b — wallet validation, market regimes, discovery (2026-10-01)

- Profiles now cover the full 14 days of retained trades (was 7), so the
  14D window and weekly checks are measured.
- Validation (§26, `wallet_validation`): trades, closed trades, active days
  and weeks, unique tokens and history coverage decide whether there is
  enough history (else INSUFFICIENT DATA, with the missing checks named);
  profitable days, share of active days profitable (consistency), max
  drawdown as % of capital put in, profit factor, best-trade share of gains
  and median return decide VALIDATED / NOT VALIDATED. Every check is shown
  with value and requirement; thresholds are edited on the Smart Wallets page
  (platform setting `wallet_validation`).
- Regimes (§28, `market_regimes`): each completed hour of a chain's
  launchpad trades is summarised once (`market_regime_hours`, migration
  0026): volume, net buy/sell flow, median price range. Hours are classified
  HIGH/LOW volume, BULLISH/BEARISH, HIGH/LOW volatility against the window's
  medians (launchpad-market regimes, not the BNB / ETH price). A wallet's
  closed trades are split by the regime of the hour they were opened in:
  CONSISTENT, REGIME_DEPENDENT (names where it lost) or INSUFFICIENT_DATA.
  The first run starts at the first traded hour and fills up to a week of
  hours per profile rebuild.
- Discovery (§25): COLLECTING_HISTORY → VALIDATED → PAPER_FOLLOWED, or
  REJECTED. A validated wallet's last 20 first-buys are replayed with the
  copy outcome evaluator (entry 3 s after their buy at the first trade
  after it, exit at their sell or 60 min; refreshed hourly). Nothing adds a
  copy target: that stays the operator's decision.
- Rendered locally on profiles built by the real rebuild: a consistent
  wallet VALIDATED and paper-followed, a wallet carried by one 30x trade
  REJECTED. NOT VERIFIED on production data yet.

## 13. M5 — launch-window coordination (2026-10-01)

Master §11: Pons is active, not safe. Every EVM token in an entry category is
assessed with its safety check (data-evm, every 2 minutes while it trades)
and again before an EVM copy buy when the assessment is older than 5
minutes (`launch_coordination`, migration 0027: `evm_tokens.coordination`,
`evm_wallet_funders`).

| Master §11 item | Detection | Data |
|---|---|---|
| anti-sniping exemptions | DECLARED_EXEMPTIONS | Pons V2 launch calldata (four entrypoints below) |
| privileged wallets / special treatment | PRIVILEGED_BUYERS | declared list, and `curve.currentSnipeTaxBps(buyer)` at the buy's block = 0 while a reference address pays tax |
| creator-linked wallets | CREATOR_BOUGHT | deployer, fee recipient, launch sender, launchAndBuy opening-buy recipient |
| creator-funded wallets | CREATOR_FUNDED_BUYERS | first funder from the explorer |
| common funders | COMMON_FUNDER | same (disperse contracts resolved to their caller; operator ignore list for exchanges / bridges) |
| wallets buying almost simultaneously | LAUNCH_BLOCK_BUNDLE, NEAR_SIMULTANEOUS_BUYERS | stored trades |
| abnormal initial ownership | ABNORMAL_INITIAL_OWNERSHIP | Pons V2 mints all supply to the curve: totalSupply - balanceOf(curve) vs net curve buys at the last stored block |
| supply concentration | WINDOW_SUPPLY_CONCENTRATION, SINGLE_WALLET_CONCENTRATION | curve buys minus sells of window buyers / totalSupply |
| coordinated exits | COORDINATED_EXIT | window buyers' sells within a span |
| (extra) fresh wallets | FRESH_WALLET_CLUSTER | eth_getTransactionCount at the window's last block |
| (extra) unknown launch / failed reads | WINDOW_NOT_OBSERVED, COORDINATION_DATA_UNAVAILABLE | NO_TRADE by default (provider failure is never "safe") |

- Actions per detection are set on EVM Markets → Launch-window coordination
  (platform setting `launch_coordination`); the strictest applies. NO_TRADE
  blocks, MANUAL_APPROVAL waits for an operator approval bound to the exact
  findings (fingerprint) and expiring after `approval_minutes`, REDUCE_SIZE
  multiplies the paper size, NONE reports only. A NO_TRADE finding cannot be
  approved. Copy skips count as BLOCKED_BY_SAFETY in the copy outcomes.
- Thresholds are configurable defaults, not verified thresholds; the
  per-launchpad summary (24 h: assessed, detected, actions, detections,
  checks without data) is there to tune them on real launches.
- Funding: Robinhood Chain uses its public Blockscout; BSC uses Etherscan
  API V2 with `ETHERSCAN_API_KEY` (Settings → Block explorers, with a
  connection test). Without a key the funding checks are NOT_CONFIGURED,
  never read as "no common funder". Holdings follow launchpad trades only.

Research (§12) used here:
- github.com/ponsdotdev/pons-labs (commit b51431f, 2026-09-29),
  `PonsV2LaunchFactory`: snipe tax starts at 99 % and decays over 15 s
  (max 60 s); `launchToken(params, configId, pairToken, address[] exemptions)`
  is documented as "the sanctioned pathway for organized teams that bundle
  their opening buys"; up to 32 exemptions; deployer and fee recipient are
  exempt automatically; `launchTokenFor` is callable only by the launch
  forwarder. The published `PonsV2BondingCurve` source has no snipe-tax code
  although the factory calls `exemptFromSnipeTax` on it: the repository is
  not the complete deployed source.
- github.com/slightlyuseless/pons-launch-engine: an open-source multi-wallet
  "launch and buy your own token" engine for Pons. Gives the launchAndBuy
  router (0xe33E9E479dF8802cb0866d5d05258bEc4cF62948) ABI with its
  exemption list, the full V2 TokenParams struct (ends with
  expectedEconomics and a CREATE2 salt) and the curve view
  `currentSnipeTaxBps(recipient)`. It is exactly the coordinated launch the
  master prompt warns about; nothing is imported from it.
- github.com/yesiambroke/pons-terminal: a trading terminal; nothing used.
- Selectors (from the struct above): launchToken 0xf35abbcf, launchToken
  with list 0xa72101af, launchTokenFor 0xd6a0eef5, launchAndBuy 0xf85f8e41.
  NOT VERIFIED against real launches from this sandbox (Robinhood RPC not
  reachable here): run
  `python -m yonixalpha_core.tools.coordination_check` in data-evm on the
  server; an unrecognised selector is reported, never read as "no
  exemptions".
- Tests: 10 core (constructed launches, ABI-encoded calldata for each
  entrypoint, the snipe-tax confirmation on a fake node, approval rules,
  mocked Blockscout / Etherscan), a data-evm pipeline test (bundle blocked,
  then reported only after the operator sets NONE), a copy-engine test (a
  target buying into a bundled launch is not copied), an API test
  (settings, approval, revoke, summary), provider tests for both explorers.

## 14. M5 on the server (2026-10-01, deploy a9792a4)

`coordination_check` on production:
- Pons V2 launches (last 30) by entrypoint: launchAndBuy router 9,
  factory.launchToken 7, factory.launchToken with an exemption list 6,
  unrecognised 8. The decoded selectors are now VERIFIED on real launches.
  Non-empty exemption lists: 3 (sizes 1, 1 and 20 wallets).
- Unrecognised: 0xa3a3ee69 (x2) is `launch(TokenParams, address)`, a
  third-party wrapper (found by signature search; named in the report, its
  exemption list is not in its calldata). 0x89942133 (x1) and 0x0a5f3d53
  (x5) are not identified yet; the tool now prints the contract they were
  sent to and checks whether the launch receipt names the exempted wallets
  (an event per exemption would make the list readable for every
  entrypoint). Until then those launches show DECLARED_EXEMPTIONS UNKNOWN,
  never "none"; PRIVILEGED_BUYERS is still checked on chain for them.
- `currentSnipeTaxBps` answers at a past block (a buyer at the launch block
  paid 9900 bps, like the reference address): the on-chain exemption check
  works. VERIFIED.
- Robinhood Blockscout answered HTTP 403 to the default client; the funding
  lookups now send a browser-like User-Agent and report the response body
  when refused. NOT VERIFIED until the next run.
- One Pons V2 launch assessed: creator-linked buy in the window (REDUCE_SIZE),
  everything else passed; one Four.meme launch: nothing detected.

## 15. M6 — token observation (2026-10-01)

Master §14-17. Every BSC / Robinhood token enters observation before it can
be traded (`chains/evm/observation.py`, migration 0028 `evm_observations`,
one row per token and category, never deleted):

- Opened at the launch (FRESH), the migration (MIGRATED) or the first
  momentum signal (MOMENTUM), with `observation_started_at`, deadline and
  reason.
- States: DISCOVERED → OBSERVING → ANALYZING (signal not met) → QUALIFIED →
  WAITING_FOR_ENTRY (held by limits, launchpad evidence, a coordination
  approval…, the blocker recorded) → ENTRY_PENDING → ENTERED; OBSERVING →
  NO_ENTRY → EXPIRED (`expiry_reason` EXPIRED_NO_ENTRY plus the last
  blocker); OBSERVING → SAFETY_FAILURE → REJECTED after 3 consecutive
  safety FAILs (one FAIL is SAFETY_FAILURE only, UNKNOWN never rejects).
  Every transition is in the state history.
- data-evm enters a token only while its observation for the current
  category is open: an expired, rejected or entered observation is not
  entered again.
- Snapshots T0, T+5, T+10, T+20, T+30, T+60 (configurable), computed as of
  that moment from stored trades even when the pass runs late, and kept
  after an entry through the window (the path after entry is outcome data):
  price, market cap (native), interval and total volume, buy / sell volume,
  buyers, sellers, effective buyers (without the wallets the coordination
  check tagged), holders and holder growth (from launchpad trades),
  liquidity and its change, curve progress, creator trades, top buyer share,
  smart-money buyers (wallets the profile rebuild VALIDATED), net flow,
  organic net flow, coordination (manipulation), safety, decision. ML is
  None with "no EVM model yet": nothing is invented.
- Adaptive windows (MIGRATED, MOMENTUM): while the token keeps trading
  (>= 5 trades in the last 5 minutes) the deadline moves out 10 minutes at a
  time, up to 180 minutes. All of it is set on EVM Markets → Observation.
- Solana keeps its own observation (`token_observations`: OBSERVING,
  PROMOTED / REJECTED / EXPIRED, follow-ups); T+20m was added to its
  follow-ups. Mapping its outcomes onto the §15 state names is not done.
- Tests: core (snapshots as of their time, expiry without entry, terminal
  states, qualified → waiting → entered, adaptive extension, safety
  rejection, settings), data-evm pipeline (observed from launch, entered
  through it, snapshots after entry), API (list, counts, held-by, detail,
  settings).

## 16. M7 — provider roles and plan health (2026-10-01)

Master §48-53 (`provider_roles`, migration 0029: `rpc_providers.roles`,
`rpc_providers.plan`; .env / public endpoints keep theirs in the existing
override settings):

- Roles DISCOVERY, MARKET_DATA, EXECUTION, CONFIRMATION, HISTORICAL_DATA,
  WALLET_DATA can be ticked per endpoint on RPC / Data Providers, for
  dashboard, .env and built-in public endpoints alike. Every request maps
  to a role by its method (Solana: sendTransaction / simulate / blockhash /
  fees = EXECUTION, signature statuses / getTransaction = CONFIRMATION,
  signature history / blocks = HISTORICAL_DATA, balances / token accounts
  = WALLET_DATA, the rest MARKET_DATA; EVM: eth_getLogs = DISCOVERY, reads
  at a numbered past block = HISTORICAL_DATA, receipts / tx by hash =
  CONFIRMATION, balance / nonce = WALLET_DATA, send / gas = EXECUTION) and
  goes to the healthy endpoints holding that role first.
- An endpoint without roles serves every role, so nothing changes until
  roles are set. When no healthy holder exists the request still goes out
  to any usable endpoint and the fallback is counted (shown per role and
  chain); roles never stop traffic.
- Plan: the plan name the operator records per endpoint (not verified with
  the provider), shown with every finding.
- WSS URLs are accepted and stored (redacted) for BSC and Robinhood too,
  for mempool / sequencer streaming (M8); nothing consumes them yet.
- Plan health (`GET /api/rpc/plan-health`, RPC / Data Providers and System
  Health): UPGRADE REQUIRED only on an observed limitation — sendTransaction
  or simulate refused ("AUTOMATIC SELL LATENCY MAY BE LIMITED BY CURRENT
  RPC PLAN"), getProgramAccounts / holder / history methods refused, >= 5 %
  HTTP 429 over >= 200 requests, eth_getLogs refused or served under 100
  blocks (the span data-evm learned on real requests), Helius
  transactionSubscribe refused in the probe — or on a chain served by
  public endpoints only. Authentication refusals are CONFIGURATION; no
  WSS for an EVM chain is INFO. Each finding: provider, current plan,
  required capability, observed limitation, impact, recommended upgrade.
- Launch coordination (M5) now reads the snipe-tax exemptions from the
  launch receipt: the curve emits `SnipeTaxExempted(address)` once per
  exempted wallet (identified on the server: 31 declared + deployer + fee
  recipient = 33 events), so every entrypoint, including the two still
  unidentified wrappers, has a readable list. The calldata decoder stays
  as the fallback.

