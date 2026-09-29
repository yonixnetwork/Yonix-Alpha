# Multi-chain upgrade — audit, research and implementation matrix (2026-09-29)

Scope: Solana (existing), BNB Smart Chain (BSC = BNB Smart Chain, one adapter), Robinhood Chain
(chain id 4663). No other trading chains in this phase.

Status words: VERIFIED (proven by a test against the real system or a real transaction),
IMPLEMENTED — NOT VERIFIED (code + unit tests against published ABIs; never run against the
live chain), NOT IMPLEMENTED, BLOCKED.

## 1. Research (July–September 2026)

**Access limits.** This build environment cannot reach BSC or Robinhood Chain RPC endpoints, and
blocks several documentation hosts (four-meme.gitbook.io, docs.flap.sh, docs.bitquery.io, dev.to).
Sources below were read from GitHub repositories (cloned) and a published npm package. Anything
that can only be proven on the chain is NOT VERIFIED here. `tools/launchpad_verify` (Phase 2) runs
those checks read-only on the production server and records the evidence.

| Resource | Last activity | What it gives | Use |
|---|---|---|---|
| four-meme-community/four-meme-ai (GitHub, official community skill) | 2026-03-30 | BSC addresses: TokenManager V1 `0xEC4549caDcE5DA21Df6E6422d448034B5233bFbC` (tokens before 2024-09-05), TokenManager2 V2 `0x5c952063c7fc8610FFDB798152D69F0B9550762b`, TokenManagerHelper3 `0xF251F83e40a78868FcfA3FA4599Dad6494E46034`. Events `TokenCreate`, `TokenPurchase`, `TokenSale`, `LiquidityAdded` (TokenManager2). `getTokenInfo` (version, tokenManager, quote, lastPrice, fee rates, offers/maxOffers, funds/maxFunds, liquidityAdded), `tryBuy`/`trySell`. V2 trade `buyTokenAMAP`/`sellToken` (sell needs ERC-20 approve). TaxToken = creatorType 5. | Primary reference for the Four.meme adapter |
| CoolBB97/flap_sniper (GitHub) | 2026-09-29 | Flap Portal on BSC `0xe2cE6ab80874Fa9Fa2aAE65D277Dd6B8e65C9De0`. Events `TokenCreated`, `TokenQuoteSet`, `FlapTokenTaxSet`, `FlapTokenAsymmetricTaxSet`, `TokenExtensionEnabled`, `TokenBought`, `TokenSold`. `getTokenV8Safe` (status, reserve, supply, price, r/h/k curve, dexSupplyThresh, quote token, buy/sell tax, pool, progress, dexId), `quoteExactInput`, `swapExactInput` (permit for sells). Status enum: Invalid 0, Tradable 1, InDuel 2, Killed 3, DEX 4, Staged 5. | Reference for the Flap adapter (built against the official Flap docs, which are not reachable from here) |
| ponsdotdev/pons-labs (GitHub, official) | 2026-09-29 | Robinhood: V1 factory `0xA5aAb3F0c6EeadF30Ef1D3Eb997108E976351feB` (instant Uniswap V3), V2 factory `0x7eD598BcEf8bd9Edd8C97A195C6d13f40801EC7e`. V2: `TokenLaunched(token, curve, deployer, pairToken, launchConfigId, graduationThreshold)`, `PoolGraduated`, `LaunchSwept`; per-token curve `CurveBuy`, `CurveSell`, `CurveCompleted`, `getReserves`, `readyToGraduate`, `buy(quoteIn, minTokensOut, recipient)`, `sell(tokensIn, minQuoteOut, recipient)`; graduates into a locked Uniswap V4 pool with a hook. | Primary reference for the Pons adapter (Solidity source) |
| hoodchain 0.1.1 (npm, nirholas/robinhood-chain-sdk) + nirholas/hood-oracle | 2026-09-15 | Robinhood: WETH `0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73`, USDG, Uniswap V3 factory / QuoterV2 / SwapRouter02, sequencer feed `wss://feed.mainnet.chain.robinhood.com`. NOXA factory `0xD9eC2db5f3D1b236843925949fe5bd8a3836FCcB` (`TokenLaunched`). Odyssey curve factory `0xEb3FeeD2716cF0eEAda05B22e67424794e1f5a80`, reflection `0x6Ce85c4b7cE12903E5867652C265bCcce57f935F`, instant `0xD7601cEe401306fdea5833c6898181D9c770F800`, legacy `0xAf9f3ce1d34909F59E88c23027f89d5807B0F915`. Odyssey events `TokenCreated`, `Traded`, `PoolCompleted`, `PoolMigrated`, `InstantTokenCreated`; `quoteBuy`/`quoteSell`/`buy`/`sell`/`getPool`. Its 2026-09-03 scan: **NOXA — 14,574 launches historically, none since block ~5.25M.** | Addresses and ABIs for NOXA / Odyssey, cross-checked with the SDK |
| CoinDesk 2026-07-15 | 2026-07 | NOXA stopped new launches after ~$12M in fees. | NOXA is registered DISABLED (inactive) |
| 0x | 2026-07-01 | Swap API supports Robinhood Chain from launch (and BSC). | Optional aggregator for DEX-listed tokens (needs an API key) |
| Honeypot.is | — | `api.honeypot.is/v2/IsHoneypot`; BSC/ETH/Base. Robinhood Chain support not confirmed. | Optional enrichment on BSC only, never the sole check |
| Bitquery / Dwellir / Alchemy / QuickNode / Chainstack | — | Robinhood + BSC RPC / indexing | Provider settings only (user-supplied keys) |

Not trusted as sources: star counts, Telegram claims, single wallets, marketing posts.

## 2. Existing architecture (audited)

- **Solana (working, VERIFIED in production):**
  - pump stream → observation → gate → paper / LIVE execution (PumpPortal, PumpSwap);
  - position loop every 2 s; ledger; wallet intel; deployer intel; ML shadow / ablation.
  - This path is preserved unchanged. New work wraps it, it does not rewrite it.
- **Legacy (to remove from the active app, archived first):**
  - Services: `data-binance`, `engine-binance-futures`, `execution-futures`, `mt5-bridge`.
  - Core: `venues/` (binance_public, bybit, hyperliquid, mt5), `execution/{binance,bybit,hyperliquid,mt5_bridge}`, `futures_live`, `grid_live`, `strategies/{grid,meta_muse,confluence,gold_btc*}`, `external_bots`.
  - Decision engine: `futures_eval`. Paper trading: `grid_engine`.
  - API routes: `venues`, `strategies`, `bots`, parts of `analytics` / `live` / `control`.
  - Web pages: `venues`, `strategies`, `bots`.
  - **Shared dependencies found:**
    - `venues.common` (Ticker/Candle/VenueError types) is imported by `solana/market_data.py`.
    - `gate_manage.py` takes a `venues` argument.
    - `safety/pipeline.py` imports `futures_live`.
    - `config_validation` / `health_state` import `external_bots`.

    These must be decoupled before deletion (Phase 9).

## 3. Implementation matrix

| # | Requirement | Plan | Phase | Status after this session |
|---|---|---|---|---|
| 3 | Chain adapters behind interfaces | `yonixalpha_core/chains/`: `ChainAdapter`, `LaunchpadAdapter`, `QuoteProvider`, `TokenSafetyAdapter`, `ExecutionProvider`, `WalletActivityAdapter`, `CopyTradingProvider`; one module per chain / launchpad, no if/else across chains | 1 | see final matrix |
| 4, 8, 57 | Launchpad registry + lifecycle model + verification states | Declarative specs (lifecycle DIRECT_DEX / BONDING_CURVE / BONDING_CURVE_TO_DEX / INSTANT_POOL, curve / liquidity / migration / execution models, events). Status computed from recorded evidence: LIVE only with verified buy+sell; PAPER ONLY with verified discovery + quotes + sell check; otherwise UNVERIFIED / DEGRADED / DISABLED | 1–2 | |
| 5 | Solana launchpads | Pump.fun curve + PumpSwap as adapters over the existing code (no behaviour change) | 1 | |
| 6 | BSC: Four.meme, Flap | Event discovery (eth_getLogs polling + optional WS), token state, quotes via Helper3 / Portal, sellability simulation, migration detection | 2–3 | |
| 7 | Robinhood: Pons, NOXA, Odyssey | Pons V1 (instant V3) / V2 (curve → V4), Odyssey curve (→ V3) / instant / reflection, NOXA registered DISABLED (inactive since block ~5.25M) | 2–3 | |
| 9–12 | FRESH / MIGRATED / MOMENTUM / OTHER; migration keeps position identity | Category stored per token; EVM migration switches the quote/execution venue on the same position | 3–4 | |
| 13–16 | Token safety, EVM sellability, Solana sellability, restricted movement | EVM: eth_call buy/sell quote, ERC-20 checks (owner, EIP-1967 proxy, tax from launchpad state, max tx/wallet where readable), Honeypot.is optional on BSC; API unavailable ≠ safe. Solana: existing checks | 3 | |
| 17–27 | Wallet profiles, scoring, strategy labels, funding graph, effective buyers, organic demand, deployer, smart money, coordination, manufactured pump | Solana: exists (wallet_graph, deployer_intel, wallet_intel). Add a chain-agnostic `wallet_profiles` table built from observed trades (Solana stream + EVM launchpad events) with the requested metrics and a configurable score; no "best wallet" ranking | 5 | |
| 28–32, 55–56 | Copy trading (3 chains) | `copy_targets`, `copy_positions`, `copy_events`; watch target trades from the chain feeds; buy/sell/partial mirroring with modes and limits; chase guard; latency stages; PAPER first, LIVE per chain only after verified execution | 6 | |
| 33–37 | Monitoring, provider policy, execution stages, Solana low latency, EVM execution | EVM RPC manager (failover, health, 429); EVM signer (eth-account, key encrypted server-side) — LIVE disabled until verified | 2, 7 | |
| 38–40, 45 | Positions, exits, risk, paper | Reuse `plan_trade` + `manage_step` for EVM paper positions (quote-denominated) | 4 | |
| 41–44 | ML | Existing readiness / shadow / ablation; add chain + launchpad as features once EVM rows exist | 8 | |
| 46 | Database | New tables per phase (launchpads evidence, evm tokens / trades, wallet_profiles, copy_*), all additive | each | |
| 47–48 | 24/7, restart | New EVM discovery + copy engine run as server services with heartbeats; cursors persisted, events idempotent (tx hash + log index) | 2, 6 | |
| 49–54 | Rebrand, nav, top bar, trade display, scanner, token details | Navigation per spec, chain pages, launchpad filter, copy-trading and smart-wallet pages | 8 | |
| 58–59 | Provider settings + test connection | Extend the existing settings center with BSC / Robinhood RPC, 0x, Honeypot.is, MadeOnSol, Nansen | 7 | |
| 60, 63 | Wallet architecture, security | One trading-wallet abstraction: Solana keypair + one EVM account for BSC and Robinhood; EVM key encrypted with the existing secretbox, never returned by the API | 7 | |
| 62 | Legacy removal | Archive branch first, decouple the shared modules, then remove services / routes / pages / nav | 9 | |
| 64 | Kill switches | Global (exists) + per chain + sniper + copy + new entries | 7 | |
| 65–67 | Tests, acceptance, final matrix | Unit tests against published ABIs; on-server read-only verification tool; final matrix | each / 10 | |

## 4. Principles for this phase

- **Never LIVE without verification.**
  - Every new EVM launchpad starts UNVERIFIED.
  - It can reach PAPER ONLY only after the on-server verification records discovery, events and a quote.
  - LIVE also requires a verified buy AND sell on the real chain, explicitly authorized by the operator.
- **The existing Solana engine is not re-routed through the new abstractions** until they are proven equivalent. The adapters wrap it.
- **An API that is unavailable is never read as "safe"**; missing data is UNKNOWN and blocks automatic entries.

## 5. Phase 2 — EVM core and launchpad adapters (IMPLEMENTED, NOT VERIFIED on chain)

Code: `packages/core-py/yonixalpha_core/chains/evm/`. Nothing in it signs or sends a transaction.

| Module | What it does |
|---|---|
| `rpc.py` | JSON-RPC client with ordered failover. Checks `eth_chainId` per endpoint before use (a wrong-chain URL is disabled). HTTP 429 honours Retry-After, else a doubling backoff (2–120 s). A method an endpoint does not serve moves to the next endpoint. A revert is returned to the caller, never retried elsewhere. `eth_getLogs` halves a range the node refuses. URLs are shown only as scheme://host. Configured by `BSC_RPC_URLS` / `ROBINHOOD_RPC_URLS`, with the public RPCs as the last fallback. |
| `abi.py` | Selectors, call encoding, event topics, log decoding (eth_abi). |
| `dex.py`, `v3pools.py` | ERC-20 reads; Uniswap V3 QuoterV2 quotes, pool Swap → trade; PancakeSwap V2 `getAmountsOut`. |
| `fourmeme.py` | TokenCreate / TokenPurchase / TokenSale / LiquidityAdded from TokenManager2. Quotes via Helper3 `tryBuy` / `trySell`; PancakeSwap V2 after liquidity is added; BEP-20-quoted tokens are refused. TaxToken `feeRate()`. |
| `flap.py` | Portal events; `getTokenV8Safe` state (status, taxes, extension, progress); `quoteExactInput` simulated. Non-BNB quote tokens and non-tradable statuses are refused. |
| `pons.py` | V2: factory `TokenLaunched` announces each curve, and only announced curves' CurveBuy / CurveSell are accepted. The buy quote simulates `curve.buy` via `eth_call` with a balance state override. If the node lacks overrides, it uses the source formula with `exact=False`. The sell quote uses the source formula on live reserves (`exact=False`). Graduated (Uniswap V4) tokens are observe-only. V1 and NOXA: V3 pool from `TokenLaunched`. |
| `odyssey.py` | Curve (bonding + legacy factories): the budget is solved locally, then confirmed with the contract's exact-out `quoteBuy`. `quoteSell` is used as-is. `PoolMigrated` switches the token to its V3 pool. Instant: V3 pool from `InstantTokenCreated`. Reflection: observe only. |
| `launchpad.py` | One `eth_getLogs` pass per range. Logs are accepted only from the launchpad's own or announced contracts; a look-alike from another contract is counted as `rejected_foreign`. Decode failures are counted, never dropped. |

**Tests:** `packages/core-py/tests/test_evm_chains.py` runs against a fake JSON-RPC node and covers:
- the ABI constants (ERC-20 Transfer, Uniswap V3 Swap);
- failover (429, wrong chain, unsupported method) and revert handling;
- every adapter's decoding and quotes;
- the foreign-contract rejection;
- the verification tool.

These prove the logic, **not** that the deployed contracts behave as their published ABIs and sources say.

**On-chain verification (run on the server, read-only):**

```
$C run --rm decision-engine python -m yonixalpha_core.tools.launchpad_verify --hours 6 --dry-run   # look first
$C run --rm decision-engine python -m yonixalpha_core.tools.launchpad_verify --hours 6             # record evidence
```

It records ACTIVE, DISCOVERY, EVENTS, QUOTE, LIQUIDITY and MIGRATION_DETECTION rows only when the check actually ran. An unavailable RPC records nothing.

It never records the following:
- **SAFETY:** needs the EVM safety checks of Phase 3.
- **BUY, SELL and TX_MONITORING:** need a real, authorized transaction.

So after it runs, a launchpad can at best be *missing SAFETY for paper*. No EVM launchpad can reach PAPER ONLY or LIVE in Phase 2.

**Known uncertainties (resolved only by the on-chain run):**
- The Flap `price` scale.
- Whether Four.meme `trySell.funds` is net of the fee (the docs say it is what the seller receives).
- Whether the deployed Pons V2 curve applies the factory's anti-snipe tax (the published curve source does not). This is why the buy quote is a simulation.
- Which Pons V1 factory is current.
