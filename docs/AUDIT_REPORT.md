# Final implementation audit: Pump.fun sniper

Date: 2026-09-25. Branch `claude/yonixalpha-platform-architecture-804nb5`.

Every claim below comes from tracing code and from tests that ran in this
repository. **No real transaction was built by PumpPortal, signed with a real
key, or sent to Solana mainnet.** The sandbox cannot reach pumpportal.fun or any
Solana RPC (proxy 403), and no credentials were provided. Everything live is
therefore reported as **IMPLEMENTED — AWAITING CREDENTIAL VERIFICATION**. The
locks remain `TRADING_ENABLED=false`, `LIVE_TRADING_ENABLED=false` and
`PAPER_TRADING=true`.

## Have I actually finished every task?

No, not in the sense of "verified against real services". The answer is split below.

| Status | Items |
|---|---|
| **COMPLETED** (code path traced end to end, tested with byte-exact synthetic data or fakes at the provider boundary) | Pump.fun-only scope; fresh-token path; migration detection (Pump.fun migration event) and the canonical PumpSwap pool; AUTO never waits for approval (MANUAL is the only approval mode); tax gate 5%/5% with UNKNOWN → NO_TRADE in AUTO; mandatory sellability; size-relative liquidity (REDUCE_SIZE / NO_TRADE); word filters (scopes, BLOCK/ALLOW, 5 match types, 5 fields, dashboard CRUD, immutable system checks); creator/holder/funding indicators with neutral wording; fake-volume metrics; automatic risk plan plus a validated operator exit plan; exits TP1–3 / trailing / stop / REDUCE / EXIT, executed automatically; paper and live sharing the gate, `manage_step` and `close_position`; the order lifecycle (build → guard → sign → persist signature → simulate → send → confirm → fill from wallet balance deltas → realized PnL); reconciliation (wallet SOL, token balances, stuck orders, missing tokens, unknown holdings, duplicate prevention); provenance on every position; .env inventory; full E2E paper scenario; LIVE architecture tests with mocked provider boundaries |
| **PARTIALLY COMPLETED** | Exit engine: no ML exit probability, and holder-distribution changes are not re-read after entry. Paper "failed execution": only insufficient liquidity and fill errors are simulated, not network or confirmation failures. Automatic TP: volatility/R-multiple based; no resistance or historical-behaviour input |
| **MISSING** | ML-driven exit model; any live execution for non-Pump.fun venues (intentionally out of scope) |
| **BLOCKED** | Real PumpPortal trade-local responses, Solana mainnet RPC/WS, Helius: no network egress from the build sandbox, and no credentials |
| **UNVERIFIED** | That PumpPortal's current transactions pass the transaction guard's allowlist (derived from the official Pump/PumpSwap IDLs). If PumpPortal adds an unknown instruction, the guard refuses the trade (fail closed) and the allowlist must be extended. Also unverified: real-world confirmation latency and slippage. PumpSwap addresses **are** verified against real mainnet data: pool authority, canonical pool and both vault ATAs match the pool documented in pump-public-docs. The account/event decoders are tested against encodings built from the current official IDL, not against live accounts |

## Paper / live parity matrix

VERIFIED = exercised by a test in this repository (fakes at the network boundary).
"credential" = awaiting a real wallet and endpoints.

| Feature | Paper | Live | Verified | Notes |
|---|---|---|---|---|
| Detection (create/trade/migration from Pump.fun program logs) | yes | same | yes | one stream feeds both |
| Word filters / blacklist | yes | same gate | yes | |
| Token safety (authorities, Token-2022 extensions) | yes | same gate | yes | |
| Tax gate 5%/5% | yes | same gate | yes | UNKNOWN → NO_TRADE in AUTO |
| Sellability | yes | same gate | yes | curve/pool sell simulated at planned size |
| Liquidity vs size | yes | same gate | yes | REDUCE_SIZE / NO_TRADE |
| Creator / holder / funding indicators | yes | same gate | yes | |
| Fake-volume metrics | yes | same gate | yes | churn, repeated wallets, buyers stopping |
| Risk plan (SL/TP/trailing/size/max loss) | yes | same gate | yes | undefined max loss → NO_TRADE |
| Live readiness check | n/a | yes | yes | locks, worker ready, fresh wallet sync, reserve |
| Entry | simulated curve/pool fill | PumpPortal tx → guard → sign → simulate → send → confirm | yes (mocked executor) / credential | live position stays `pending_entry` until confirmed |
| Confirmation | n/a | signature status polled, rebroadcast every 3 s, EXPIRED after 75 s | yes (fake RPC) | "sent" is never treated as filled |
| Actual fill recording | simulated | pre/post wallet balances of our owner+mint | yes (fake tx) | ATA rent and network fee included in cost |
| Monitoring / pricing | curve (stream) or PumpSwap pool (RPC) | same | yes | |
| TP1–3, trailing, stop, manual exit | `manage_step` | same `manage_step`, exits become SELL orders | yes | |
| Exit intelligence (REDUCE/EXIT) | yes | same | yes | curve and PumpSwap flow |
| Failed exit handling | n/a | retried with +10% slippage per failure (capped), critical alert from the 2nd failure | yes | |
| Realized PnL | proceeds − cost (simulated) | proceeds − cost (confirmed) | yes | same `close_position` |
| ML sample + label | yes | yes (`live_execution_realized_pnl`) | yes | |
| DB records / timeline | yes | yes + `execution_orders` | yes | |
| Realtime events | yes | yes | yes | E2E asserts trade.created/updated/closed |
| Restart recovery | n/a | reconcile first; SIGNED/SUBMITTED looked up; stale PENDING cancelled | yes | |
| Duplicate prevention | n/a | idempotency key, one live position per mint, one pending order per position, row lock | yes | |
| Unknown holdings | n/a | recorded once per 24 h | yes | |
| Provider errors | n/a | guard refusal / HTTP error / simulation failure → FAILED, no fill | yes | |

## The 44 report items

1. **Requirements completed**: see the COMPLETED row above.
2. **Requirements partially completed**: see the PARTIALLY COMPLETED row.
3. **Requirements missing**: an ML exit model. Live execution for non-Pump.fun venues is out of scope by design.
4. **Existing architecture**: Solana engines (discovery, momentum, generic migration) → Redis stream store → decision-engine (safety gate) → paper-trading (entries, management, live worker) → API → Next.js dashboard. PostgreSQL holds state; Redis carries events, caches and readiness.
5. **Features preserved**: futures strategies (paper), grid, Gold vs BTC analytics, ML champion/challenger, notifications, kill switch, every earlier gate check.
6. **Features upgraded**:
   - migrated tokens are now read from the canonical PumpSwap pool on chain, with trader flow from pool events; they no longer fall into approval for missing wallet data;
   - AUTO never waits for approval;
   - the tax gate is split into buy and sell tax;
   - word filters were rewritten;
   - funding-link and demand-quality checks were added;
   - Pump.fun strategies accept an operator exit plan.
7. **Files changed**: `git diff --stat 71fb8f5` (about 55 files). Main ones:
   - `safety/gate.py`, `safety/rules.py`, `safety/settings.py`, `safety/pipeline.py`;
   - `solana/assembler.py`, `solana/flow.py`, `paper_engine.py`, `db/models.py`, `config.py`, `strategies/catalog.py`;
   - `decision-engine/app/gate_eval.py`, `paper-trading/app/gate_manage.py`, `paper-trading/app/main.py`;
   - API `control.py`, `paper.py`, `strategies.py`;
   - web pages for rules, decisions, trades, settings and the strategy panel;
   - `.env.example`, compose, web Dockerfile.
8. **Files created**:
   - `live_trading.py`;
   - `solana/{wallet,pumpportal,txguard,live_exec,pumpswap,funding}.py`;
   - `paper-trading/app/live_worker.py`;
   - API `routes/live.py`;
   - web `dashboard/live/page.tsx`;
   - migration `0011`;
   - `testing/pumpswap.py` (IDL-layout fixtures);
   - tests `test_live_exec.py`, `test_live_worker.py`, `test_e2e_paper.py`, `test_live_api.py`, `test_funding_links.py`, `test_pumpswap.py`, `test_pumpswap_manage.py`;
   - `docs/CONFIGURATION.md` and this report.
9. **Database changes**: migration 0011. Upgrade, downgrade and upgrade were run, and `alembic check` is clean. It adds:
   - `execution_orders` (unique idempotency key, check constraints);
   - `reconciliation_events`;
   - `blacklist_rules.action`;
   - provenance columns on positions (`execution_mode`, source, lifecycle, provider, route, pool, strategy, model/feature version, pending order, exit failures);
   - a quantity check that allows `pending_entry` / `failed` / `needs_review` rows at quantity 0.
10. **API changes**:
    - `/api/live/{status,settings,orders,orders/{id},reconciliation,positions}`;
    - blacklist endpoints take `action`, new fields, match types and scopes;
    - positions expose provenance, and trade details include orders;
    - strategy config accepts the operator exit plan;
    - decision detail carries the tax, sellability and liquidity reports.
11. **WebSocket changes**: live position/trade/balance events use source `live`. No new event types were needed.
12. **PumpPortal integration**: Local Transaction API only (`trade-local`); the custodial Lightning API is not used. Transactions are guarded and signed locally. IMPLEMENTED — AWAITING CREDENTIAL VERIFICATION.
13. **Helius integration**: through standard Solana RPC/WS URLs (Helius URLs derived from `HELIUS_API_KEY`); no Helius-only API is required. Not exercised against the real service.
14. **Solana execution integration**:
    - `SolanaLiveExecutor` runs guard → sign → persist signature → simulate (sigVerify) → send (maxRetries 0, rebroadcast) → confirm or expire → `getTransaction` → fill;
    - `wallet_balances` feeds reconciliation.
15. **Pump.fun fresh-token implementation**: create/trade events decoded from program logs, curve state, gate, curve-simulated paper fills, and the `pump` route for live trades.
16. **Pump.fun migration implementation**: Pump.fun's migration event → migration candidate → canonical PumpSwap pool PDA, verified (owner and mints) → reserves, fee from the pool's latest trade event, trader flow → gate. No pool means MIGRATION_PENDING.
17. **PumpSwap execution implementation**: the `pump-amm` pool through PumpPortal. The guard knows the PumpSwap buy / buy_exact_quote_in / sell discriminators. Paper fills are simulated against the pool model.
18. **Buy implementation**:
    - SOL-denominated; `max_sol_in` = size × (1 + entry slippage);
    - bounded fee and priority fee;
    - the position opens only on a confirmed fill that shows tokens in and SOL out.
19. **Sell implementation**:
    - token-denominated;
    - `min_sol_out` comes from the model's expected proceeds less exit slippage;
    - the guard bounds tokens in;
    - partial and full exits are supported, with dust closing the position.
20. **Automatic exit implementation**:
    - `manage_step` covers stop, TPs, breakeven and trailing; exit intelligence adds REDUCE/EXIT;
    - LIVE exits become SELL orders executed by the worker without human action;
    - failures retry with more slippage and raise an alert.
21. **Tax implementation**:
    - Token-2022 `TransferFeeConfig` (max of the current/next epoch) sets both buy and sell tax;
    - a transfer hook or an unparseable extension means UNKNOWN;
    - limits are 5%/5% (hard maximum 25%), and protocol/venue fees are kept separate;
    - the dashboard shows Buy Tax, Sell Tax, Tax Limit, Source, Confidence and Decision.
22. **Sellability implementation**:
    - SELLABLE only when a sell at the planned size was simulated or quoted and nothing restricts transfers;
    - otherwise NOT SELLABLE or UNKNOWN (never executable in AUTO).
23. **Liquidity implementation**:
    - usable liquidity, position/liquidity, entry and exit impact, and the binding size cap;
    - the size is reduced to the caps, and NO_TRADE applies when even the minimum does not fit.
24. **Token-risk implementation**: mint/freeze authority, Token-2022 extensions (hooks, permanent delegate, and so on), metadata, and the tax above.
25. **Wallet-risk implementation**:
    - holder concentration and creator share;
    - serial creator;
    - early-buyer funding links, reported as CREATOR-LINKED INDICATOR and RELATED-WALLET INDICATOR, with exchange-like funders ignored and an unavailable check reported as FUNDING_UNCHECKED;
    - wording is always "indicator", never an accusation.
26. **Word-filter implementation**:
    - scopes GLOBAL/FRESH/MIGRATED (plus per-engine);
    - fields name/symbol/metadata/any/mint;
    - match types exact/word/substring/pattern/regex, all case-insensitive; regexes with catastrophic shapes are refused;
    - ALLOW waives word blocks only;
    - dashboard add/edit/delete/enable/disable, audited.
27. **Blacklist implementation**: the same rule table, with BLOCK as the default action.
28. **Automatic risk implementation**:
    - SL from volatility within the min/max stop;
    - size from equity × risk per trade ÷ stop distance, capped by pool fraction, exposure, slippage and impact;
    - TPs as R-multiples, and trailing with activation, distance and step;
    - the stop is never widened;
    - the operator exit plan is validated against the same limits.
29. **Paper implementation**: the same gate, simulated curve/pool fills including fee and impact, partial exits, and the same `close_position`. It is not a PnL generator: fills come from reserves moved by real (or test) trades.
30. **Live implementation**: complete, locked by three environment flags plus global mode LIVE and worker readiness. IMPLEMENTED — AWAITING CREDENTIAL VERIFICATION.
31. **ML implementation**:
    - feature snapshot at decision time, labels from realized PnL (paper or live source kept distinct);
    - champion/challenger with no automatic promotion;
    - a high score cannot override any risk block.
32. **Strategy implementation**: `fresh_launch_flow` (fresh), a migrated-pool flow signal, and momentum. Modes are OFF/MANUAL/PAPER/AUTO.
33. **UI implementation**:
    - a Live Execution page (preflight, settings, positions, orders, reconciliation);
    - word filters with actions, scopes and match types;
    - tax, sellability and liquidity panels on decisions;
    - provenance and on-chain orders on trade details;
    - operator exit-plan fields on strategy pages.
    - Checked in Chromium at desktop and mobile widths: no console errors apart from one intentional 422 validation test, and no horizontal overflow.
34. **.env variables**: see `docs/CONFIGURATION.md`. Added `WALLET_PUBLIC_KEY`, `WALLET_PRIVATE_KEY` and the `HELIUS_RPC_URL`/`HELIUS_WS_URL` aliases. Removed the unused `HELIUS_WEBHOOK_SECRET`, `NEXT_PUBLIC_WS_URL` and `PUMPPORTAL_API_KEY`. Documented `SOLANA_WATCHED_ADDRESSES`, the token TTLs and the legacy cost setting.
35. **Dashboard settings**: risk settings, modes, word filters and rules, operator exit plans, live execution settings. See `docs/CONFIGURATION.md`.
36. **Tests performed**: pytest for core, API and every service, ruff, `alembic check` plus a downgrade/upgrade round trip, the Next.js lint and build, and the browser check.
37. **Tests passed** (final run):

    | Suite | Passed |
    |---|---|
    | core | 319 |
    | API | 97 |
    | decision-engine | 45 |
    | paper-trading | 56 |
    | engine-binance-futures | 48 |
    | ml | 23 |
    | data-binance | 14 |
    | data-solana | 10 |
    | discovery | 8 |
    | migration | 11 |
    | momentum | 11 |
    | **Total** | **642** |

38. **Tests failed**: none in the final run. During the audit, tests and the IDL cross-check found these real bugs, all fixed:
    - a DB check forbade the pre-fill LIVE row;
    - the `reconcile_required` status overflowed its column, and was renamed `needs_review`;
    - the PumpSwap fee ignored the buyback fee that the current program charges. The fee is now derived from the amounts each trader actually paid, with the declared bps as a floor;
    - a failed funding check was silent, and is now reported as FUNDING_UNCHECKED;
    - two catalog tests asserted the old "no config" behaviour.
39. **Tests blocked**: anything needing pumpportal.fun, Solana mainnet or Helius (no egress, no credentials).
40. **Unverified integrations**:
    - PumpPortal trade-local against the live service;
    - the transaction guard allowlist against PumpPortal's current transactions;
    - Solana RPC/WS against mainnet;
    - Jupiter;
    - Telegram.
41. **Security findings**:
    - In the reference bot (`solana_pumpswap_migration_bot`):
      - a hidden `_ROUTE_KEY` / `platformFee` of 0.5% is added to Jupiter swaps and paid to a third-party referral account. It was not ported and is flagged as a security issue;
      - it uses the custodial Lightning API with the API key in the URL;
      - success is assumed from a returned signature;
      - PnL comes from wallet balance deltas across unrelated activity;
      - it uses bare `except` and `skipPreflight`.
    - In this code:
      - the private key is a `SecretStr`, redacted in `repr`, never logged or returned;
      - the guard refuses any transaction whose fee payer or sole signer is not our wallet, and any unknown program, unknown discriminator, token move, authority change, account close to another wallet, fee above the bound, or lookup-table use;
      - the signature is persisted before sending.
42. **Performance findings** (2 GB droplet):
    - the funding check costs ≤ 2 RPC calls per checked wallet (default 6) + 1 per shared funder, cached 7 days;
    - PumpSwap pool trades cost one `getTransaction` per new signature, cached 1 h;
    - the live worker makes 3 RPC calls every 30 s and one row-locked order at a time;
    - no new long-running processes (the live worker runs inside paper-trading).
43. **Data-quality findings**:
    - the PumpSwap fee is unknown until the pool's first trade event (the pool is not priced until then). It is the fee actually paid, which covers LP, protocol, creator, buyback and cashback without double counting holder rewards;
    - "fresh wallet" means fewer than 25 signatures and "busy funder" means 1000 or more (heuristics, reported as indicators);
    - a Token-2022 transfer hook makes the tax unknowable by design;
    - evidence from the UI seed showed funding errors correctly surfacing as FUNDING_UNCHECKED instead of being silently read as clean.
44. **Remaining work**:
    1. Run the live path once with a dedicated low-balance wallet and real RPC:
       - set the three locks;
       - global mode LIVE;
       - one strategy set to AUTO or MANUAL with a very small `manual_position_size_sol`;
       - confirm the preflight on the Live Execution page;
       - observe one buy and one sell and compare the fill with Solscan.
    2. Extend the guard allowlist if PumpPortal's transactions contain an instruction it refuses; the refusal reason is stored on the order.
    3. An ML exit model, and holder-change monitoring after entry.
    4. Simulating failed network or confirmation paths in paper mode.
