# Configuration

Two kinds of configuration, kept strictly apart:

- **Secrets and infrastructure** live in `.env` on the server. They never go
  in Git, never go in the database, and the API never shows them. They are
  read once at start-up by `yonixalpha_core.config.Settings`, plus four raw
  `os.getenv` reads listed in the matrix. Examples: API keys, private keys,
  URLs, database/Redis wiring, and the three trading locks.
- **Trading behaviour** lives in the database. It is edited on the dashboard,
  validated by the same code the engines use, and every change goes to the
  audit log. Examples: modes, risk limits, token-tax limits, slippage, word
  filters, custom rules, strategy parameters, operator exit plans, and
  live-execution settings.

The per-variable inventory is in
[environment-variable-matrix.md](environment-variable-matrix.md). What each of
the user's repositories needs, and how it maps onto YonixAlpha, is in
[integration-config-inventory.md](integration-config-inventory.md).

## Start-up validation

At start-up, the API validates each module's `.env` configuration
(`yonixalpha_core.config_validation`). The results are cached in Redis and
shown under System Health → Configuration and at
`GET /api/system/config-validation`.

| Status | Meaning |
|---|---|
| DISABLED | module mode is OFF; it never blocks anything |
| READY | what the module needs for its current mode is present and well-formed |
| CONFIGURATION_ERROR | an enabled module lacks what it needs. This covers paper requirements (such as Solana RPC for the Pump.fun engines, or the bridge for an MT5 strategy), and live requirements when it is asked to trade live |

A module in CONFIGURATION_ERROR cannot be switched to AUTO or MANUAL while
the global mode is LIVE. The global mode cannot go LIVE while an AUTO or
MANUAL module lacks its live configuration: the API answers 409 and lists
the missing variable names. Messages name variables, never values.
Switching a module to OFF or PAPER is never refused.

## External providers

| Provider | Purpose | Key | Endpoint | Paper | Live |
|---|---|---|---|---|---|
| Solana RPC/WS (Helius or any) | chain reads, program-log stream, simulate/send/confirm | in URL, or `HELIUS_API_KEY` | `SOLANA_RPC_URL` / `SOLANA_WS_URL` | required for Pump.fun | required for Pump.fun |
| PumpPortal Local Transaction API | builds the unsigned buy/sell (curve `pump`, PumpSwap `pump-amm`); signed locally | none | `POST https://pumpportal.fun/api/trade-local` | not used | required |
| PumpPortal data WebSocket | coverage and migration cross-check; trades of held mints only with a key (metered) | `PUMPPORTAL_API_KEY` (optional) | `wss://pumpportal.fun/api/data` | optional | optional |
| Jupiter | cross-check and fallback exit quotes | `JUPITER_API_KEY` (optional; keyless lite-api is deprecated) | `api.jup.ag/swap/v1` or `lite-api.jup.ag/swap/v1` | optional | optional |
| Binance USDⓈ-M | market data (public); orders, Algo-service stops, fills, balances | `BINANCE_API_KEY/SECRET` | `fapi.binance.com` / testnet | public data | required for venue `binance` |
| Bybit V5 | market data; orders, position stop, executions, balance | `BYBIT_API_KEY/SECRET` | `api.bybit.com` / testnet | public data | required for venue `bybit` |
| Hyperliquid | info API; signed orders via an API (agent) wallet | `HYPERLIQUID_ACCOUNT_ADDRESS` + `HYPERLIQUID_API_WALLET_PRIVATE_KEY` | `api.hyperliquid.xyz` / testnet | public data | required for venue `hyperliquid` and the live grid |
| MT5 bridge | rates, depth of market, orders on a MetaTrader 5 account | `MT5_BRIDGE_URL` + `MT5_BRIDGE_TOKEN` (MT5 login stays on the bridge host) | private network only | required for venue `mt5` | required for venue `mt5` |
| Standalone bots' control APIs | status / close / config; LIVE conflict guard | `*_CONTROL_URL` + `*_TOKEN` | private network only | optional | optional |
| Telegram | alerts | `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` | Bot API | optional | recommended |

PumpPortal charges 0.5% per trade (per its docs). The transaction guard
refuses any fee transfer above `max_platform_fee_bps` (default 100 bps).

## Runtime settings (database, dashboard)

| Where on the dashboard | What | Validation |
|---|---|---|
| Risk Settings | per-engine risk limits: stop range, risk per trade, max position, slippage, token tax limits (buy 5% / sell 5%), holder/flow thresholds, funding-check width, max leverage, … | `safety/settings.py` hard limits |
| Settings → modes | global mode (PAPER / MANUAL / LIVE) and per-strategy/venue mode (OFF / PAPER / MANUAL / AUTO). LIVE is refused while the locks are closed or a needed module is in CONFIGURATION_ERROR | `safety/store.py`, API, `config_validation.py` |
| Word Filters & Rules | BLOCK/ALLOW filters, scopes, fields, match types; custom threshold rules | `safety/rules.py` |
| Strategy pages | Meta Muse, Gold vs BTC, Confluence (including venue `binance`/`bybit`/`hyperliquid`/`mt5`), Hyperliquid grid parameters; Pump.fun operator exit plans | `strategies/catalog.py` |
| Paper Trading → Simulated execution failures | entry/exit failure % | `paper_execution.parse_settings` |
| Live Execution | Pump.fun: slippage, fees, SOL reserve, wallet-sync age. Futures (`futures_live_execution`): max leverage, free-balance reserve, balance max age, max fill deviation | `live_trading.LIMITS`, `futures_live.LIMITS` |

The built-in safety checks cannot be configured away: authorities, tax limits
(hard maximum 25%), sellability, liquidity, defined maximum loss, the
exchange-side stop on every live futures position, and the environment
locks.
