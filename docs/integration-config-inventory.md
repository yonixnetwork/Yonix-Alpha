# Integration configuration inventory

Every configuration variable found in the user's repositories, and in
YonixAlpha itself where it takes over their role. Each entry was found by
reading the repositories' source (every `os.getenv` / `os.environ`, including
indirect reads such as `os.getenv(f"{env_prefix}_API_KEY")`) and their
`.env.example` and YAML files, not only their READMEs.

Repositories inspected, with the commit read:

| Repository | Commit |
|---|---|
| yonixnetwork/yonix-alpha (this repository) | — |
| hyperliquid-grid-trading-bot | `bfec033` |
| confluence-matrix-forex | `8389d89` |
| solana-token-scanner | `8d04ed2` |
| meta-muse-crossover-strategy | `ab3b52d` |
| solana-sniper-jupiter-swap-api | `ad86e7a` |
| goldvsbtc-binance-future | `332bbca` |
| Meme-bot (private) | `f6bd3d3` |
| trading-command-center | `58d7bdc` |
| trade-alpha | `e9f5f10` |
| reference bot hoziertom44-arch/solana_pumpswap_migration_bot | `466a205` |

Columns:

- **Used By**: the YonixAlpha component that now consumes the variable (or its replacement).
- **Source**: where the original repository reads it.
- **Status**: one of IMPLEMENTED — AWAITING CREDENTIAL VERIFICATION, PARTIALLY IMPLEMENTED, NOT IMPLEMENTED, REPLACED (a YonixAlpha equivalent exists), or NOT USED (deliberately not carried over).

No credential for any external service was available in this environment,
so nothing below is marked VERIFIED against a real service.

| Variable | Repository | Purpose | Secret? | Required? | Used By | Paper | Live | Source | Status |
|---|---|---|---|---|---|---|---|---|---|
| `HYPERLIQUID_API_WALLET_PRIVATE_KEY` | hyperliquid-grid-trading-bot | API (agent) wallet that signs orders | yes | live | `execution/hyperliquid.py` (same name) | no | yes | `src/config.py:126` | IMPLEMENTED — AWAITING CREDENTIAL VERIFICATION |
| `HYPERLIQUID_ACCOUNT_ADDRESS` | hyperliquid-grid-trading-bot | main account holding USDC | no | live | `venues/hyperliquid.py`, `execution/hyperliquid.py` (same name) | read-only views | yes | `src/config.py:134` | IMPLEMENTED — AWAITING CREDENTIAL VERIFICATION |
| `CONTROL_API_TOKEN`, `CONTROL_API_PORT` | hyperliquid-grid-trading-bot | the bot's own control API | token yes | optional | `HYPERLIQUID_GRID_CONTROL_URL` / `HYPERLIQUID_GRID_TOKEN` → `external_bots.py` | optional | optional | `src/main.py:76-77` | IMPLEMENTED — AWAITING CREDENTIAL VERIFICATION |
| `network` (mainnet/testnet) | hyperliquid-grid-trading-bot | network | no | yes | `HYPERLIQUID_TESTNET` | – | yes | `config.yaml` | REPLACED |
| `asset, range_mode, range_pct, range_lower/upper, grid_levels, capital_usdc, leverage, max_drawdown_pct, range_break_pct, flatten_on_pause` | hyperliquid-grid-trading-bot | grid parameters | no | yes | database strategy config `hyperliquid_grid` (dashboard, validated) | yes | yes | `config.yaml` | REPLACED |
| `post_only, time_in_force` | hyperliquid-grid-trading-bot | order behaviour | no | – | fixed: post-only (ALO) resting orders in `grid_live.py` | – | yes | `config.yaml` | REPLACED |
| `refresh_interval_seconds, verbose_logging, persist_trades` | hyperliquid-grid-trading-bot | loop and logging | no | – | execution-futures loop (5 s), structured logs, `execution_orders` rows | – | – | `config.yaml` | REPLACED |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | hyperliquid-grid-trading-bot | alerts | token yes | optional | `notify.py` (same names) | optional | optional | `src/config.py:142-143` | REPLACED |
| `MT5_LOGIN`, `MT5_PASSWORD`, `MT5_SERVER` | confluence-matrix-forex | MT5 terminal login | password yes | live | services/mt5-bridge (Windows host env only); YonixAlpha uses `MT5_BRIDGE_URL` + `MT5_BRIDGE_TOKEN` | bridge needed for data | yes | `config.py:32-34` (with hard-coded placeholder defaults) | IMPLEMENTED — AWAITING CREDENTIAL VERIFICATION |
| `SYMBOLS, TIMEFRAME, risk/confluence thresholds` | confluence-matrix-forex | strategy parameters | no | yes | database strategy config `confluence_matrix` | yes | yes | `config.py` | REPLACED |
| — (no env) | solana-token-scanner | public RPC scanner | – | – | YonixAlpha Solana safety checks (`SOLANA_RPC_URL`) | yes | yes | `scanner.py` | REPLACED |
| `EXCHANGE` | meta-muse-crossover-strategy | binance / bybit switch | no | yes | per-strategy DB setting `venue` (binance / bybit / hyperliquid) | yes | yes | `config.py:18` | REPLACED |
| `BINANCE_API_KEY`, `BINANCE_API_SECRET` | meta-muse-crossover-strategy | ccxt futures keys | yes | live | `execution/binance.py` (same names) | no | yes | `bot.py:61-62` via `f"{env_prefix}_API_KEY"` | IMPLEMENTED — AWAITING CREDENTIAL VERIFICATION |
| `BYBIT_API_KEY`, `BYBIT_API_SECRET` | meta-muse-crossover-strategy | ccxt Bybit keys | yes | live | `execution/bybit.py` (same names) | no | yes | same | IMPLEMENTED — AWAITING CREDENTIAL VERIFICATION |
| `CONTROL_API_TOKEN`, `CONTROL_API_PORT` | meta-muse-crossover-strategy | the bot's control API | token yes | optional | `META_MUSE_CONTROL_URL` / `META_MUSE_TOKEN` | optional | optional | `control_api.py` | IMPLEMENTED — AWAITING CREDENTIAL VERIFICATION |
| `PRIVATE_KEY` | solana-sniper-jupiter-swap-api | swap signer | yes | live | `WALLET_PRIVATE_KEY` | no | yes | `.env.example`, `swap.py` | REPLACED |
| `RPC_URL` | solana-sniper-jupiter-swap-api | Solana RPC | yes (key in URL) | yes | `SOLANA_RPC_URL` | yes | yes | same | REPLACED |
| `JUPITER_API_KEY` | solana-sniper-jupiter-swap-api | `api.jup.ag/swap/v1` key | yes | optional here | `JUPITER_API_KEY` (quotes only; keyless falls back to the deprecated lite-api) | optional | optional | `swap.py:20-21` | IMPLEMENTED — AWAITING CREDENTIAL VERIFICATION |
| `EXCHANGE`, `BINANCE_API_KEY/SECRET`, `BYBIT_API_KEY/SECRET` | goldvsbtc-binance-future | as Meta Muse | yes | live | as Meta Muse; strategy `gold_btc_trend` | yes | yes | `bot.py:51-52` | IMPLEMENTED — AWAITING CREDENTIAL VERIFICATION |
| `CONTROL_API_TOKEN`, `CONTROL_API_PORT` | goldvsbtc-binance-future | the bot's control API | token yes | optional | `GOLDVSBTC_CONTROL_URL` / `GOLDVSBTC_TOKEN` | optional | optional | `control_api.py` | IMPLEMENTED — AWAITING CREDENTIAL VERIFICATION |
| `HELIUS_RPC_URL`, `HELIUS_WS_URL` | Meme-bot, reference bot | Helius endpoints | yes (key in URL) | yes | aliases of `SOLANA_RPC_URL` / `SOLANA_WS_URL` | yes | yes | `config.py` | REPLACED (aliases accepted) |
| `HELIUS_API_KEY` | Meme-bot, reference bot | listed in `.env.example` | yes | – | `HELIUS_API_KEY` derives the URLs in YonixAlpha | – | – | `.env.example` only; the code parses the key out of the URL (`main.py:532`) | REPLACED |
| `PUMPPORTAL_API_KEY` | Meme-bot, reference bot | custodial Lightning `/api/trade` and data WS | yes | live (there) | `PUMPPORTAL_API_KEY`, only for the metered data subscription of held mints; trading uses the keyless Local API | optional | optional | `config.py:31-32` | IMPLEMENTED — AWAITING CREDENTIAL VERIFICATION (Lightning: NOT USED) |
| `WALLET_PUBLIC_KEY`, `WALLET_PRIVATE_KEY` | Meme-bot, reference bot | wallet (there: the PumpPortal-generated custodial wallet) | private yes | live | same names; a self-custodied wallet, signed locally | no | yes | `.env.example` | IMPLEMENTED — AWAITING CREDENTIAL VERIFICATION |
| `CONTROL_API_TOKEN`, `CONTROL_API_PORT` | Meme-bot | the bot's control API | token yes | optional | `MEME_BOT_CONTROL_URL` / `MEME_BOT_TOKEN` | optional | optional | control API module | IMPLEMENTED — AWAITING CREDENTIAL VERIFICATION |
| `DASHBOARD_TOKEN` | trading-command-center | dashboard bearer token | yes | yes | not carried over: YonixAlpha login (JWT, argon2) protects every route | – | – | `app.py` | NOT USED |
| `META_MUSE_TOKEN`, `GOLDVSBTC_TOKEN`, `MEME_BOT_TOKEN`, `HYPERLIQUID_GRID_TOKEN` | trading-command-center | per-bot control tokens | yes | per bot | same names in YonixAlpha, plus a `*_CONTROL_URL` for each | optional | optional | `.env.example`, `bots.yaml` | IMPLEMENTED — AWAITING CREDENTIAL VERIFICATION |
| `BOTS_YAML` | trading-command-center | registry path | no | – | fixed registry `external_bots.BOTS` | – | – | `app.py` | REPLACED |
| — (Android settings, no env) | trade-alpha | Kotlin Android app | – | – | not integrated (different platform) | – | – | `app/` | NOT USED |
| `MT5_BRIDGE_URL`, `MT5_BRIDGE_TOKEN` | yonix-alpha (new) | the bridge to MT5 | token yes | venue mt5 | `execution/mt5_bridge.py`, `venues/mt5.py` | yes | yes | `config.py` | IMPLEMENTED — AWAITING CREDENTIAL VERIFICATION |

## Security-relevant findings in the source repositories

1. **Reference bot** (`solana_pumpswap_migration_bot/trade.py:111-129`).
   - A hard-coded "routing" key `83X5Evf8…` with `_ROUTE_BPS = 50` on the Jupiter referral program `REFER4Zg…`. It is a hidden 0.5% fee on every swap, paid to a third party.
   - Not ported.
2. **Meme-bot and the reference bot**.
   - Both use PumpPortal's custodial Lightning API (`https://pumpportal.fun/api/trade`) with a PumpPortal-generated wallet, so PumpPortal holds the key.
   - YonixAlpha uses the Local Transaction API and signs locally.
3. **Meta Muse and goldvsbtc**.
   - Both place stop-loss/take-profit with ccxt `stopLossPrice` / `takeProfitPrice` on Binance USDⓈ-M.
   - Since 2025-12-09, Binance only accepts conditional orders through the Algo service (`POST /fapi/v1/algoOrder`), and the old endpoint answers `-4120 STOP_ORDER_SWITCH_ALGO`. Depending on the ccxt version installed, those bots may run without any stop.
   - Their `requirements.txt` only asks for `ccxt>=4.4.0`. When placement fails, goldvsbtc keeps the position open without protection and retries on the next check (`bot.py:282-283`).
   - YonixAlpha's `BinanceProvider` uses the Algo service.
4. **hyperliquid-grid-trading-bot**.
   - `src/bot.py:212` treats any order that is no longer open as filled ("assume filled"), so a cancelled or rejected order is booked and replaced as a fill.
   - YonixAlpha's live grid books fills only from Hyperliquid's order status.
   - Its `.env.example` and `config.yaml` also carry a referral link. That is harmless for the code, but noted.
5. **confluence-matrix-forex**: `config.py` has non-empty placeholder defaults for `MT5_LOGIN` / `MT5_PASSWORD`. If they are not overridden, the bot silently tries to log in with them. The bridge instead requires real values and refuses to start without its token.
