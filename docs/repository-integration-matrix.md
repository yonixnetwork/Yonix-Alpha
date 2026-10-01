# Repository integration matrix

One row per repository feature, covering what was taken, how it is wired,
and what is verified.

- **Integrated?**:
  - yes: running in YonixAlpha;
  - replaced: a YonixAlpha equivalent does the job;
  - adapter: reachable through the control-API adapter;
  - no.
- **Tested?**:
  - automated: a test in this repository exercises it, with fakes at the network boundary;
  - no: nothing tests it.

  Nothing here was tested against a real exchange, a real wallet or a real
  MT5 terminal: there were no credentials, and this environment blocks
  egress to exchanges and Solana.
- **Decision**: ACCEPT / PARTIALLY USE / REJECT.

## The user's repositories

| Repository | Feature | Useful Component | Credential Requirements | API Requirements | Environment Variables | Configuration Files | Dependencies | Paper Support | Live Support | Integrated? | Tested? | Issues | Decision |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| meta-muse-crossover-strategy | BTC/ETH EMA 9/21 divergence | signal + exit rules | Binance or Bybit futures key | Binance USDⓈ-M / Bybit V5 | `EXCHANGE`, `BINANCE_*`, `BYBIT_*`, `CONTROL_API_*` | `config.py` | ccxt, numpy | yes (`strategies/meta_muse.py`) | yes (`futures_live` via Binance/Bybit/Hyperliquid providers) | yes | automated | ccxt `stopLossPrice` on Binance vs Algo migration (-4120); forming-candle reads; fixed 10x leverage | PARTIALLY USE: logic ported, execution rebuilt |
| meta-muse-crossover-strategy | control API | status/close/config contract | bot token | the bot's HTTP API | `META_MUSE_CONTROL_URL`, `META_MUSE_TOKEN` | – | – | – | status/close | adapter | automated (mock) | must stay on a private network | ACCEPT (behind YonixAlpha auth) |
| goldvsbtc-binance-future | gold/BTC dual-trend (per-asset thresholds) | signal + three exit rules | Binance or Bybit futures key | as above | `EXCHANGE`, `BINANCE_*`, `BYBIT_*`, `CONTROL_API_*` | `config.py` | ccxt, numpy | yes (`strategies/gold_btc_trend.py`) | yes (`futures_live`) | yes | automated | same Algo issue; keeps positions open unprotected when SL fails | PARTIALLY USE: logic ported, execution rebuilt |
| goldvsbtc-binance-future | control API | as Meta Muse | bot token | – | `GOLDVSBTC_CONTROL_URL`, `GOLDVSBTC_TOKEN` | – | – | – | status/close | adapter | automated (mock) | – | ACCEPT |
| confluence-matrix-forex | confluence scoring, pivots, TP extensions | `signals.py`, `analysis.py` | MT5 login | MetaTrader5 (Windows) | `MT5_LOGIN/PASSWORD/SERVER` | `config.py` | MetaTrader5, pandas | yes (Binance XAUUSDT or MT5 rates) | yes, venue `mt5` through services/mt5-bridge | yes | automated (fake MT5 terminal) | placeholder credential defaults; MT5 is Windows-only; brokers may publish no depth of market (then NO_TRADE) | PARTIALLY USE: scoring ported; MT5 via bridge |
| confluence-matrix-forex | MT5 execution (`executor.py`) | order request shapes, IOC filling, SLTP modify | MT5 login | MT5 terminal | as above | – | MetaTrader5 | – | yes (bridge) | yes (rebuilt in the bridge) | automated (fake) | Windows-only; the bot logs in itself with credentials from its own `.env` | PARTIALLY USE: same magic-number ownership as the original (`executor.py:29`), but behind an authenticated bridge with idempotent client ids |
| hyperliquid-grid-trading-bot | grid math, breakers | `src/grid.py`, `src/risk.py` | API wallet + account address | Hyperliquid exchange/info | `HYPERLIQUID_*`, `TELEGRAM_*`, `CONTROL_API_*` | `config.yaml` | hyperliquid-python-sdk | yes (paper grid) | yes (`grid_live.py`: ALO orders, confirmed fills, exchange stop) | yes | automated | "assume filled" when an order leaves the book (`src/bot.py:212`); referral link in config | PARTIALLY USE: math ported, execution rebuilt |
| hyperliquid-grid-trading-bot | control API | status/close/config | bot token | – | `HYPERLIQUID_GRID_CONTROL_URL/TOKEN` | – | – | – | status/close | adapter | automated (mock) | – | ACCEPT |
| solana-token-scanner | token checks (authorities, holders) | ideas / checks | none | Solana RPC | – | – | requests | yes (gate checks) | same gate | replaced | automated | public RPC only, no rate limiting | PARTIALLY USE (checks re-implemented in the safety gate) |
| solana-sniper-jupiter-swap-api | Jupiter quote/swap | quote shapes, `api.jup.ag/swap/v1` | Jupiter key (optional) | Jupiter Swap API | `PRIVATE_KEY`, `RPC_URL`, `JUPITER_API_KEY` | – | solders, requests | quotes only | not used for execution (Pump.fun goes through PumpPortal Local) | yes (quotes) | automated (mock) | signs whatever Jupiter returns (no transaction guard) | PARTIALLY USE: quotes only |
| Meme-bot (private) | Pump.fun sniper: new-token WS, filters, TP/SL | filter ideas, PumpPortal data WS | PumpPortal key + custodial wallet | PumpPortal Lightning `/api/trade`, data WS | `HELIUS_*`, `PUMPPORTAL_API_KEY`, `WALLET_*`, `CONTROL_API_*` | `config.py` | websockets, requests | yes (native Pump.fun engines) | yes (native, PumpPortal **Local**, signed locally) | replaced; data WS used as a cross-check | automated | custodial Lightning API (PumpPortal holds the key and the wallet) | PARTIALLY USE: data WS accepted, Lightning rejected |
| Meme-bot (private) | control API | status/close/config | bot token | – | `MEME_BOT_CONTROL_URL/TOKEN` | – | – | – | status/close | adapter | automated (mock) | – | ACCEPT |
| trading-command-center | multi-bot dashboard | `CONTROL_API_CONTRACT.md`, `bot_client.py` pattern | dashboard + bot tokens | bots' control APIs | `DASHBOARD_TOKEN`, per-bot tokens | `bots.yaml` | FastAPI | – | – | adapter (`external_bots.py`) | automated | a static dashboard token; subprocess start/stop | PARTIALLY USE: contract adopted, auth replaced by the YonixAlpha login |
| trade-alpha | Android analysis app (Kotlin) | decision outcomes LONG/SHORT/WAIT/NO TRADE, "prefer NO TRADE" | exchange / AI keys (in-app) | Binance REST, AI providers | – | Gradle | Kotlin/Android | – | – | no | – | different platform; no execution | REJECT for integration (MIT; ideas already reflected in the gate) |
| hoziertom44-arch/solana_pumpswap_migration_bot (reference) | PumpSwap migration sniping | PDA derivations (cross-checked) | PumpPortal key + wallet | PumpPortal Lightning, Helius, Jupiter | `HELIUS_*`, `PUMPPORTAL_API_KEY`, `WALLET_*` | – | solders | – | – | no | – | **hidden 0.5% Jupiter referral fee to a hard-coded third party** (`trade.py:111-129`); custodial API; bare `except` | REJECT (no code taken) |

## Third-party components and official sources

| Component | Source | License | Used for | Security review | Decision |
|---|---|---|---|---|---|
| hyperliquid-python-sdk 0.24.0 | github.com/hyperliquid-dex (official) | MIT (Hyperliquid Labs) | EIP-712 L1 action signing, order wire format, `Cloid` | pinned; only `utils.signing` / `utils.types` are imported. Transport is our own httpx; the SDK's `requests` client is never used | ACCEPT (the `hyperliquid` extra, execution-futures only) |
| eth-account 0.13.7 (SDK dependency) | PyPI | MIT | local key → signer | key only in memory, never logged | ACCEPT |
| binance-connector-python | github.com/binance (official) | MIT | reference for endpoints, parameters and signing (Algo service) | not a dependency | PARTIALLY USE (reference only) |
| pybit | github.com/bybit-exchange (official) | MIT | reference for V5 paths and signing | not a dependency | PARTIALLY USE (reference only) |
| MetaTrader5 5.0.6180 | PyPI / metaquotes (official) | MIT (per PyPI) | terminal API on the bridge host | Windows-only; runs outside the server | ACCEPT (services/mt5-bridge only) |
| ccxt | – | MIT | used by the user's futures bots | exchange-specific conditional-order behaviour hidden behind ccxt, unpinned (`>=4.4.0`) | REJECT as a dependency (direct, pinned venue clients instead) |
| PumpPortal Local Transaction API | pumpportal.fun docs (official) | service | unsigned Pump.fun / PumpSwap transactions | every transaction checked by `txguard` before signing | ACCEPT |
| PumpPortal Lightning API | pumpportal.fun docs | service | custodial trading | PumpPortal holds the private key | REJECT |
| PumpPortal data WebSocket | pumpportal.fun docs | service | coverage/migration cross-check; held-mint trades with a key | key only in the connection URL, never logged | ACCEPT |
| Jupiter Swap API v1 | dev.jup.ag (official) | service | quotes | keyed `api.jup.ag`; keyless lite-api is deprecated | ACCEPT (quotes) |
| 1chimaruGin/bsc-mempool @212d4463 | github.com/1chimaruGin/bsc-mempool | Apache-2.0 OR MIT | reference for BSC mempool copy trading (full pending bodies, shadow-first rollout) | not a dependency; needs its own bsc-geth node and relay accounts | PARTIALLY USE (ideas only; see MASTER_UPGRADE_2026.md §17) |
| chainstacklabs/robinhood-chain-sequencer-feed @8ea0972 | github.com/chainstacklabs (Chainstack Labs) | Apache-2.0 | feed measurements (backlog, compression, signatures), 12 real frames as a test fixture | experimental by its own label; no keys | PARTIALLY USE (data and findings; no code; MASTER_UPGRADE_2026.md §18) |
| ponsdotdev/ponsfamily @44a3db9 | github.com/ponsdotdev (official Pons) | MIT | Pons V1 / V2 Solidity source: event and ABI verification | deployed bytecode not compared from here | ACCEPT (primary source) |
| pons-launch-engine, pons-terminal, robinhood-trading-tools | github.com (community) | MIT | address cross-checks; router labels; evidence for launch coordination and wash volume | multi-wallet self-buying, 0.5% router skim, roundTrip | REJECT for integration (§18) |
| nirholas robinhood-chain-sdk / -alerts / -trading-bot | github.com/nirholas | All rights reserved / Apache-2.0 | none | proprietary licence; activity refreshed by empty commits | REJECT (§18) |
| four-meme-community/four-meme-ai @c81f0ee | github.com/four-meme-community | MIT | Four.meme address / event cross-check; X Mode / AntiSniperFeeMode flags | private key in env | PARTIALLY USE (reference only) |
| MeteoraAg dynamic-bonding-curve (+sdk) | github.com/MeteoraAg (official) | Non-commercial (program) / MIT (SDK) | Solana DBC research for M10 | Token-2022 transfer hooks possible | research only |
| coincurve 21.0.0 | PyPI (ofek/coincurve) | MIT OR Apache-2.0 | fast secp256k1 for eth-keys (senders, feed signatures) | prebuilt wheels; pinned | ACCEPT |
| Arbitrum Nitro sequencer feed format | docs.arbitrum.io (official) / Robinhood Chain feed | service | Robinhood Chain sequencer feed (broadcast messages, L2 message kinds, resume header) | message format only, decoded by our own code; no Nitro code taken | ACCEPT (format) |
| pyrlp 5.0.0 | PyPI (github.com/ApeWorX/pyrlp) | MIT | decoding signed transactions from the feed | already installed by eth-account; now pinned | ACCEPT |

Research note: from this environment, pumpportal.fun, dev.jup.ag and
developers.binance.com were blocked. The facts above come from official SDK
sources (cloned from GitHub or installed from PyPI) and search results
quoting the official documentation. Nothing was taken from third-party blog
posts.
