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

## 6. Phase 3 — EVM discovery, safety and paper trading (IMPLEMENTED, NOT VERIFIED on chain)

New service `services/data-evm` runs one loop per chain (BSC, Robinhood Chain). It is read-only on chain.

**Each loop, per step:**
1. **Discovery** (every ~3 s):
   - `eth_getLogs` from the persisted cursor (`evm_cursors`) up to `head − confirmations`.
   - Launches go to `evm_tokens` and trades go to `evm_trades`. The trade key is `chain:tx:log_index` with ON CONFLICT DO NOTHING.
   - The cursor advances in the same transaction, so a restart or re-scan never duplicates an event.
   - Curves and pools a launchpad announced are restored into the adapters at start.
2. **Stats and category.**
   - Stats are computed from the trades' own amounts (not from unverified launchpad price fields), over 5 minutes: buys, sells, distinct buyers, volumes, buy share, price change, realized volatility (the Solana `realized_volatility`).
   - **MIGRATED:** migrated in the last 60 min.
   - **FRESH:** launch observed, younger than 30 min, still on its curve.
   - **MOMENTUM:** ≥ 10 buys, ≥ 6 buyers, buy share ≥ 55 %.
   - **OTHER:** anything else.
   - A token whose launch was not observed is never FRESH.
3. **Safety** (`chains/evm/safety.py`). The verdict is PASS / WARN / FAIL / UNKNOWN. UNKNOWN is never read as safe, and entries need PASS.
   - **Round trip:** a buy quote of the position size, then a sell quote of exactly the tokens it returns. A sell quote is required; being buyable is not enough.
   - Round-trip loss limit.
   - Launchpad taxes, status, extensions and the Pons V2 near-graduation block.
   - Token code; EIP-1967 proxy slots (upgradeable token = FAIL); `owner()`.
   - **Restricted movement:** Odyssey `limitsActive` / `maxWallet`, and Pons V1 launch restrictions.
   - **Honeypot.is (optional, BSC):** a flag fails the token; a clean result never passes it.
4. **Paper entry** (`chains/evm/paper.py`). Every blocker is recorded on the token (`extra.entry_decision`):
   - kill switch;
   - trading controls;
   - launchpad status must be PAPER ONLY or LIVE, i.e. evidence;
   - category and trade signal;
   - a safety PASS less than 5 min old;
   - liquidity;
   - per-chain limits (open positions, exposure, daily loss, 24 h re-entry cooldown).

   Sizing uses the shared `plan_trade`:
   - percentages come from the shared risk settings;
   - amounts come from `evm_trading` in BNB / ETH, never SOL;
   - the executable round trip is the exit cost.

   A partial unique index (`uq_paper_positions_evm_open`) makes a second open position on the same token impossible.
5. **Management.**
   - Each position is marked at the executable sell quote of its remaining tokens, net of fees and taxes, through the shared `paper_engine.apply_step` (stop, take-profits, breakeven, trailing).
   - After a migration the adapter quotes the DEX: same position row, and a `venue_switched` event is recorded.
   - An unquotable position stays open as UNPRICED; it is never marked at an invented price.
   - The Solana position loop skips `evm_*` engines.
6. **Evidence** (every 30 min). The service records what it observed on the real chain as `launchpad_checks` (source `data-evm`):
   - ACTIVE;
   - DISCOVERY: PASS only, because a quiet window is not a failure and a 24 h expiry handles inactivity;
   - EVENTS; QUOTE; LIQUIDITY; SAFETY: ran to a verdict; MIGRATION_DETECTION: confirmed by `detect_migration`.

   It never records BUY, SELL or TX_MONITORING. So a BSC or Robinhood Chain launchpad reaches PAPER ONLY only from real-chain evidence, and LIVE stays impossible: there is no EVM execution.

**Other additions:**
- **API:** `/api/evm/tokens`, `/api/evm/tokens/{chain}/{token}`, `/api/evm/positions`, `/api/evm/settings` (GET / PUT). The PUT is validated and audited, and bumps the config revision. Trading-control writes now bump it too.
- **UI:** Dashboard → BSC / Robinhood.
- **Migration:** 0022 (additive).

**Tests (fake node + real Postgres / Redis):**
- discovery idempotent across a re-scan;
- safety PASS;
- entry refused while the launchpad is unverified, opened once evidence exists, never duplicated;
- stop-loss exit at the executable quote;
- kill switch and chain switch block entries;
- restart restores announced curves;
- the Solana manager ignores EVM positions.

**NOT VERIFIED:** any of this against the real chains (no RPC egress from the build environment).

## 7. Phase 4 — wallet profiles and copy trading (paper) (IMPLEMENTED, NOT VERIFIED on chain)

**Wallet profiles** (`yonixalpha_core/wallet_profiles.py`, table `wallet_profiles`) are rebuilt every 10 minutes by `copy-engine`.

Sources:
- **BSC / Robinhood:** every decoded launchpad / pool trade in `evm_trades`.
- **Solana:** `launch_buyers` (early buyers with resolved launch outcomes). This is a narrower view, and each profile states its source.

Metrics:
- trades, tokens, closed round trips, wins, realized PnL;
- median buy, average hold;
- early-entry share (bought within 60 s of an observed launch);
- median gap between trades.

Descriptive labels: SNIPER, SCALPER, HOLDER, HIGH_ACTIVITY, POSSIBLE_BOT.

Score:
- A weighted, configurable score, returned with its components:
  - win rate, shrunk toward a base rate with a Beta prior;
  - realized PnL;
  - early entries;
  - sample size.
- Fewer than 5 closed round trips means no score (INSUFFICIENT_DATA).
- There is no ranking field and no "best" label. The UI sorts by whatever metric the operator picks.

**Copy trading** runs in the `copy-engine` service. It is paper only and nothing is signed or sent. Targets are set per chain with one of three modes:
- NOTIFY: signal only.
- BUY_ONLY: copy entries; exits come from our own risk plan.
- MIRROR: copy entries and the target's sells.

Each target has its own limits: fixed or proportional size and a maximum size, minimum target buy, maximum open copies, maximum delay, and a chase guard.

A target's trade is a candidate, never an order. "Smart wallet bought, so buy" does not exist here.

Every copied buy passes, in order:
1. kill switch;
2. trading controls: COPY TRADING, chain, NEW ENTRIES;
3. delay limit;
4. per-target open-position limit;
5. venue approval:
   - **Solana:** the Solana gate must have marked this exact mint executable in the last 10 minutes; otherwise `NO_GATE_APPROVAL`;
   - **EVM:** the launchpad's evidence-based status must allow paper, and there must be a fresh safety PASS. The check is run on demand if stale;
6. the shared risk planner;
7. the chase guard: no entry when our price is more than N % above the target's.

**Idempotency:** each target trade is recorded in `copy_events` before any decision, and `(target_id, source_event_id)` is unique. Solana stream trades carry no signature, so their id is a hash of the trade's own fields.

**Exits:**
- **EVM (MIRROR):** a target sell is mirrored as the same fraction of the target's observed holding (partial sells included), at the executable sell quote. Copy positions (`evm_copy_<chain>`) are managed by `copy-engine` with the shared `apply_step`.
- **Solana:** copy positions (`copy_solana`) use the Solana venue schema, so the existing Solana position loop manages them: curve, PumpSwap after migration, exit intelligence, stops and take-profits.
  - A target sell of at least 50 % of its holding requests a full exit.
  - Smaller sells are recorded as `PARTIAL_NOT_MIRRORED_ON_SOLANA`. The Solana loop has no external partial-exit hook, and adding one to the working Solana path was out of scope.
- A partial unique index blocks a second open copy position per (engine, token).

**Latency stages** are recorded per event, and the API reports medians:
- detection: target trade to seen. This includes the feed's own delay: EVM confirmations plus the data-evm pass, and the Solana stream plus the 1 s poll;
- analysis;
- risk;
- execution: paper fill;
- total.

Landing is "not applicable (paper)".

**API and UI:**
- `/api/copy/targets` (create / update / delete, audited, bumps the config revision), `/api/copy/events`, `/api/copy/positions`, `/api/wallets/profiles`.
- UI: Copy Trading and Smart Wallets. Watching a profile adds it as a NOTIFY target.
- Migration: 0023 (additive).

**Tests:**
- EVM: copies are refused until the launchpad is verified, idempotent across passes, opened with the planner, and partial and full sells are mirrored;
- NOTIFY, kill switch, delay, and trades made before the target was added;
- Solana: no gate approval means no copy; approved means copied; small partial sells are not mirrored; a large sell requests the exit;
- profile metrics, labels and the shrunk score;
- API validation and audit.

**NOT VERIFIED:** any of this against real target wallets on chain.
