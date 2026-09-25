# Implementation Matrix — Control Center Specification

Research and audit report for the "Ultimate Master Implementation" specification
(phases 1–3 of its own required order). Written before implementation, updated as phases land.
Status vocabulary follows the specification: VERIFIED / PARTIALLY VERIFIED /
IMPLEMENTED — AWAITING CREDENTIAL VERIFICATION / NOT VERIFIED / FAILED / BLOCKED.

## 1. How research was done, and its limits

The development sandbox's egress proxy **blocks every exchange and data API**. It was
tested directly on 2026-09-25: `lite-api.jup.ag`, `api.dexscreener.com`, `api.bybit.com`,
`fapi.binance.com`, `api.hyperliquid.xyz`, and `api.mainnet-beta.solana.com` all returned
`403` on CONNECT. `dev.jup.ag` and `docs.dexscreener.com` are blocked too. The sandbox
*can* reach `raw.githubusercontent.com` and PyPI, so research used primary sources hosted
there:

| Source | What was read | Authority |
|---|---|---|
| `jup-ag/jupiter-quote-api-node/swagger.yaml` | Jupiter Swap API v1 OpenAPI spec: `/quote` params, `QuoteResponse`, `RoutePlanStep`/`SwapInfo` | Official (Jupiter org) |
| `pump-fun/pump-public-docs` (`PUMP_PROGRAM_README.md`, `PUMP_SWAP_README.md`, `idl/pump.json`, `idl/pump_amm.json`) | Program IDs, `BondingCurve`/`Global` layouts, `create`/`buy`/`sell`/`migrate` instructions, event layouts + discriminators | Official (pump.fun org) |
| `anza-xyz/agave` `account-decoder` + `account-decoder-client-types/src/token.rs` | Exact `jsonParsed` shape of SPL/Token-2022 mints: `mintAuthority`, `freezeAuthority`, `extensions[].extension/state` tags | Official (Solana validator client) |
| PyPI JSON API | Current versions/maintenance of `pybit` 5.17.0 (2026-07), `binance-sdk-derivatives-trading-usds-futures` 17.5.0 (2026-09), `binance-futures-connector` 4.2.0, `hyperliquid-python-sdk` 0.24.0, `solders` 0.29.0, `MetaTrader5` 5.0.6180 | Official package metadata |
| PyPI `dexscreener` 1.3 client models | DexScreener pair field names (`liquidity.usd`, `txns.m5.buys`, `pairCreatedAt`, …) | **Third-party** — secondary evidence only |
| Web search | Jupiter lite vs paid host; PumpSwap migration (since 2025-03-20); Chainstack's pump.fun `logsSubscribe` guide; Binance XAUUSDT/PAXGUSDT perpetuals (2026-01) | Supplementary |

Anchor discriminator convention was verified offline: `sha256("event:<Name>")[:8]` and
`sha256("account:<Name>")[:8]` reproduce every discriminator in the official pump.fun IDL
exactly (CreateEvent, TradeEvent, CompleteEvent, CompletePumpAmmMigrationEvent,
BondingCurve).

**Consequence:** every new external integration is implemented against these primary
specs and fixture-tested here, but **none can be live-verified from the sandbox**. Live
verification moves to the droplet via a read-only script (`scripts/verify-live-data.py`)
that calls only public, unauthenticated endpoints and never trades. Until its output is
reviewed, those integrations are **NOT VERIFIED (live)**.

YouTube: not used. Official specs and source code were available for every question that
mattered, and a video demonstration isn't evidence of current API behaviour.

## 2. Existing system classification

| Component | Status | Notes |
|---|---|---|
| Auth (JWT, refresh rotation, lockout, rate limit) | EXISTING AND WORKING | Verified live on yonixalpha.com. DO NOT TOUCH beyond additions. |
| Deployment (Compose, nginx TLS, certbot, deploy/backup scripts) | EXISTING AND WORKING | Verified live; 6 deployment bugs fixed during go-live. |
| Kill switch (Redis + AOF) | EXISTING AND WORKING | Keep. |
| `core-py/risk.py` limit checks | EXISTING BUT INCOMPLETE | Correct fail-closed semantics, but it only checks account-level limits. There's no sellability/liquidity/impact/token safety, no automatic sizing/SL/TP, no decision states beyond approve/reject. |
| `decision-engine` | EXISTING BUT INCOMPLETE | Features are tx-count acceleration and token age only; confidence is capped because no price/liquidity feed exists, so LONG is unreachable by design. |
| `engine-solana-discovery` | NEEDS REFACTOR | Subscribes to **all SPL Token program logs** (every token transfer on Solana) and calls `getTransaction` per mint. Unsustainable on a 2GB droplet and free-tier RPC credits. Pump.fun-scoped event decoding replaces it. |
| `engine-solana-momentum` | NEEDS REFACTOR | Same Token-program firehose; only counts `transferChecked`. There's no buyer/seller, SOL volume, or price data. |
| `engine-solana-migration` | EXISTING BUT BROKEN (by design) | Ships with zero parsers, so it detects nothing. Replaced by deterministic bonding-curve `complete` flag + `CompletePumpAmmMigrationEvent`. |
| `execution_router` | EXISTING AND WORKING | Honestly routes Solana to UNSUPPORTED. Keep. There's no live Solana execution, and this spec doesn't need it in paper mode. |
| `engine-binance-futures` | EXISTING AND WORKING (unexercised) | Hand-rolled signed REST + user stream; idempotent orders, reconciliation tested. `place_order_idempotent` has no production caller. DO NOT TOUCH. |
| `data-binance` | EXISTING AND WORKING | Idle until `BINANCE_SYMBOLS` is set. It can ingest XAUUSDT/PAXGUSDT for Gold vs BTC with no code change. |
| `paper-trading` | EXISTING BUT INCOMPLETE | Correct PnL + failure isolation. There's no entry for Solana (no price feed), no fees by default, no partial TPs/trailing, and no sizing. |
| ML (train, registry, gates) | EXISTING AND WORKING | Leakage + significance gates were fixed in the prior audit. There's no champion/challenger or drift monitoring. |
| Dashboard (7 pages) | EXISTING BUT INCOMPLETE | Works, but it's a basic shell. It has no collapsible/mobile nav, no top-bar status, and no settings/blacklist/strategy pages. |
| `audit_logs` table | EXISTING BUT INCOMPLETE | Table exists; only auth writes to it. |
| Realtime (WebSocket/SSE) | MISSING | Dashboard polls. |
| Runtime risk config in DB | MISSING | All limits are `.env`-only. |
| Blacklist / custom rules | MISSING | |
| Bybit | MISSING | |
| Meta Muse / Confluence / Hyperliquid / Gold vs BTC | MISSING | See section 5. |

## 3. Key technical decisions (and where the spec was corrected)

1. **Sellability is measured, not inferred.** For bonding-curve tokens, buy and sell
   outcomes are computed exactly from on-chain reserves with pump.fun's constant-product
   formula. For migrated tokens, a Jupiter buy quote followed by a sell quote for the
   resulting amount gives a **round-trip loss** that captures both-direction impact and
   fees. A sell route that doesn't exist (no quote) is a hard block.
2. **Price impact is computed, not read.** Sources disagree on whether Jupiter's
   `priceImpactPct` is a fraction or a percent. Rather than bet a hard block on that
   unit, impact is computed from quote amounts: the intended size is compared against a
   tiny reference quote. The raw field is stored for audit only.
3. **Honeypot/sell restriction on Solana is not an EVM concept.** Following the spec's
   own section 19, the checks use the real Token-2022 model: `freezeAuthority` (can freeze
   holder accounts), `mintAuthority` (unlimited dilution), `permanentDelegate` (can move
   anyone's tokens), `transferHook` (arbitrary program on every transfer), `nonTransferable`,
   `defaultAccountState: frozen`, `pausableConfig`, and `transferFeeConfig` (fee bps
   deducted on every sell). Several of these are hard blocks. A missing sell route is
   another.
4. **Holder concentration excludes the pool.** `getTokenLargestAccounts` returns token
   accounts, and for a fresh token the largest one is the bonding curve's vault. Owners
   are resolved and known curve/pool addresses excluded; otherwise every fresh token would
   read as "one wallet holds 80%".
5. **Wallet-cluster ownership claims are never made.** Following the spec's section 22,
   only measurable indicators are reported (e.g. a small number of wallets accounting for
   most buy volume). "Common funding source" analysis needs per-wallet transaction-history
   crawling that free-tier RPC can't sustain, so it's **not implemented**, and it's shown
   as unavailable rather than guessed.
6. **Migration does not mean BUY.** Migration detection only moves a candidate into
   analysis on the migrated-token pipeline.
7. **One safety gate, one hierarchy.** A single pure-function gate implements the spec's
   section 99 order (data → token → liquidity → execution → risk → strategy → ML). ML is
   an input to the strategy stage only and cannot clear a hard block. Manual approval
   re-runs the gate.
8. **Paper and live share the gate.** Paper mode consumes the same assessment, including
   sizing, SL/TP/trailing, and simulated impact/fees, so paper results aren't
   artificially optimistic.
9. **Memory budget.** The droplet has 2GB RAM (OOM kills observed during a rebuild). New
   logic goes into existing services instead of new containers wherever possible.

## 4. Data sources adopted

| Need | Source | Credential | Rate limit / cost | Status |
|---|---|---|---|---|
| Fresh launches, trades, curve completion, migration | pump.fun program logs (`logsSubscribe` mentions `6EF8…F6P`), decoded with official IDL layouts | Solana RPC/WS (Helius key) | One WS subscription; no per-event RPC | NOT VERIFIED (live) |
| Curve reserves / lifecycle | `getAccountInfo` on bonding-curve PDA `["bonding-curve", mint]` | Solana RPC | 1 call per assessment | NOT VERIFIED (live) |
| Mint/freeze authority, Token-2022 extensions | `getAccountInfo` (`jsonParsed`) on mint | Solana RPC | 1 call per assessment | NOT VERIFIED (live) |
| Holder concentration | `getTokenLargestAccounts` + `getMultipleAccounts` | Solana RPC | 2 calls per assessment | NOT VERIFIED (live) |
| Migrated-token buy/sell routes, impact | Jupiter Swap API v1 `/quote` (`lite-api.jup.ag` free; `api.jup.ag` with `x-api-key`) | Optional `JUPITER_API_KEY` | Free tier is low; 3 quotes per assessment | NOT VERIFIED (live) |
| Migrated-token liquidity/volume/txns | DexScreener `/latest/dex/tokens/{mint}` | None | 300 req/min | NOT VERIFIED (live) |
| Gold price | Binance USDⓈ-M `XAUUSDT` / `PAXGUSDT` via existing `data-binance` | None (public) | Existing | NOT VERIFIED (live) |

## 5. Strategy and exchange integrations — feasibility

| Item | Finding | Decision |
|---|---|---|
| Bybit | Official `pybit` 5.17.0 is maintained (MIT). V5 unified API. | Phase 11: read-only public market data + authenticated account/positions via `pybit`. |
| Meta Muse Crossover | Phase 0 audit: 9/21 EMA divergence, **no backtest evidence of edge**, ccxt execution, no idempotency. | Integrate as signal-only, PAPER-only strategy on Binance klines; it can never reach AUTO/LIVE without an out-of-sample backtest. |
| Confluence Matrix | Requires MetaTrader5, whose Python package ships **only `win_amd64` wheels** (verified on PyPI). | **BLOCKED** on the Linux droplet. Needs a Windows host or an MT5 bridge. |
| Hyperliquid Grid | Official `hyperliquid-python-sdk` 0.24.0 maintained. Repo is single-asset, REST-only, no restart reconciliation. | Separate module later; public `/info` market data first, execution last. |
| Gold vs BTC | User previously excluded `goldvsbtc-binance-future`, so it's treated as missing. Binance lists `XAUUSDT` (2026-01) and `PAXGUSDT` perps. | Ratio analytics on existing Binance ingestion. No trading signal until backtested. |

## 6. Session scope (this implementation pass)

In the spec's order, prioritising the safety core. Everything else stays tracked here
rather than being half-built:

1. Master safety gate + automatic sizing/SL/TP/trailing with provenance (phase 6).
2. Solana data layer: pump.fun event decoding, bonding-curve math, mint risk, holder
   concentration, Jupiter/DexScreener adapters, with per-provider rate budgets (phase 8).
3. DB: `risk_assessments`, runtime `risk_settings`, `blacklist_rules`, `custom_rules`,
   `trade_timeline_events`; audit-logged config changes.
4. Decision engine and paper trading wired to the gate; rejected opportunities stored
   (phase 7).
5. API + UI: risk settings, blacklist, decision detail with reasons, strategy/system
   status, and the application shell (phase 4 subset).
6. Droplet verification script for all live data sources.

All six items above are implemented and tested in the build environment.

**Second pass (this branch, after the first deploy):** everything that had been deferred is
now built, each integration together with the tests that can be run without network access:

| Deferred item | Now | Verification state |
|---|---|---|
| Realtime WebSocket bus | `/api/ws` (first-message auth, Origin check, ping), Redis pub/sub, heartbeats | VERIFIED (integration + browser) |
| Bybit | V5 public data + signed read-only account | IMPLEMENTED — AWAITING CREDENTIAL VERIFICATION |
| Meta Muse | ported logic, closed candles, gate + paper, LONG/SHORT | VERIFIED on synthetic candles; no edge claimed |
| Hyperliquid | info API adapter + paper grid engine | VERIFIED on synthetic mids; live data NOT VERIFIED |
| Gold vs BTC | ratio / z-score / correlation analytics | analytics only; live data NOT VERIFIED |
| Champion/challenger + drift | quality quarantine, temporal holdout, operator promotion, PSI drift | VERIFIED on synthetic samples; no real model yet (no labeled trades) |
| Confluence Matrix | scoring ported, run on Binance XAUUSDT | MT5/forex BLOCKED |

`docs/FINAL_REPORT.md` has the complete §97 report.
