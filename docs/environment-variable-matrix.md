# Environment variable matrix (YonixAlpha)

Every variable that YonixAlpha's code reads. Each one comes from a search
for every `Settings` field in `packages/core-py/yonixalpha_core/config.py`
and every raw `os.getenv` / `os.environ` read. `.env.example` contains
exactly these variables and no others.

How to read the columns:

- **Secret**: yes means it must never be logged, returned by the API,
  shown in the dashboard, or stored in the database. Secrets are held as
  `SecretStr` where the code allows.
- **Required**:
  - R: always required.
  - P: required for a module's paper operation.
  - L: required only for that module's live trading.
  - O: optional.
- **Paper / Live**: whether the variable is needed in that mode.
- **Configured**: whether it is set on the deployment. This is not
  knowable from the repository, so it reads *per deployment*. The API
  reports it without values under System Health → Configuration.
- **Validated**: which check enforces it (Settings type/validator, or the
  per-module start-up validator `config_validation.py`).

| Variable | Module | Purpose | Secret | Required | Default | Where used | Paper | Live | Configured | Validated | Notes |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `APP_ENV` | application | `production` restricts CORS to PUBLIC_DOMAIN / API URL | no | O | development | `config.py`, `apps/api/app/main.py` | – | – | per deployment | Settings | |
| `APP_NAME` | application | name in logs / OpenAPI / System page | no | O | YonixAlpha | API | – | – | per deployment | Settings | |
| `APP_HOST`, `APP_PORT` | application | API bind address | no | O | 0.0.0.0 / 8000 | `apps/api/Dockerfile` CMD | – | – | per deployment | shell default | |
| `LOG_LEVEL` | application | log verbosity | no | O | INFO | all services | – | – | per deployment | Settings | |
| `NEXT_PUBLIC_API_URL` | frontend | API base URL baked into the web build; the realtime WS URL is derived from it | no | R | http://localhost:8000 | `apps/web/lib/api.ts`, `events.tsx`, CORS | yes | yes | per deployment | build | set before `docker compose build` |
| `PUBLIC_DOMAIN` | application | CORS origin | no | R (prod) | http://localhost:3000 | `config.py` | yes | yes | per deployment | Settings | |
| `LETSENCRYPT_EMAIL` | deployment | certbot command in the runbook | no | O | – | `docs/DEPLOYMENT.md` only | – | – | per deployment | – | not read by code |
| `POSTGRES_HOST/PORT/DB/USER` | database | database location | no | R | postgres / 5432 / yonixalpha / yonixalpha | `config.py`, compose | yes | yes | per deployment | Settings | |
| `POSTGRES_PASSWORD` | database | database password | **yes** | R | empty (the container refuses to start) | `config.py`, compose | yes | yes | per deployment | Settings | |
| `DATABASE_URL` | database | overrides POSTGRES_* | yes (contains the password) | O | – | `config.py` | – | – | per deployment | Settings | |
| `REDIS_HOST/PORT` | redis | Redis location | no | R | redis / 6379 | `config.py` | yes | yes | per deployment | Settings | |
| `REDIS_PASSWORD` | redis | Redis auth (`--requirepass`) | **yes** | R (prod) | – | `config.py`, compose | yes | yes | per deployment | Settings | |
| `REDIS_URL` | redis | overrides the above | yes | O | – | `config.py` | – | – | per deployment | Settings | |
| `SOLANA_RPC_URL` (alias `HELIUS_RPC_URL`) | solana_* | chain reads, PumpSwap pools, live send/confirm, reconciliation | yes (URL embeds the key) | P (Pump.fun) | derived from HELIUS_API_KEY | discovery, decision-engine, paper-trading, data-solana | yes | yes | per deployment | config_validation (solana_*) | |
| `SOLANA_WS_URL` (alias `HELIUS_WS_URL`) | solana_* | `logsSubscribe` on the Pump.fun program | yes | P (Pump.fun) | derived | engine-solana-discovery, data-solana | yes | yes | per deployment | config_validation | |
| `SOLANA_RPC_BACKUP_URL`, `SOLANA_WS_BACKUP_URL` | solana_* | failover endpoints | yes | O | – | `RpcManager`, `SolanaWsClient` | – | recommended | per deployment | Settings | |
| `HELIUS_API_KEY` | solana_* | derives the Helius mainnet RPC/WS URLs | **yes** | O | – | `config.py` validator | – | – | per deployment | Settings | health: `helius` |
| `SOLANA_WATCHED_ADDRESSES` | data-solana | extra addresses for the generic event table | no | O | empty | `services/data-solana/app/main.py` | – | – | per deployment | – | raw getenv |
| `MIGRATION_AMM_PROGRAM_IDS` | engine-solana-migration | generic AMM detector (ships without parsers) | no | O | empty | `services/engine-solana-migration/app/detect.py` | – | – | per deployment | – | raw getenv; not part of the Pump.fun path |
| `WALLET_PRIVATE_KEY` (alias `SOLANA_WALLET_PRIVATE_KEY`) | solana_* live | signs Pump.fun transactions locally | **yes** | L | – | `solana/wallet.py`, live worker | – | yes | per deployment | wallet loader + config_validation | `SecretStr`; never logged or returned |
| `WALLET_PUBLIC_KEY` | solana_* live | cross-check; must match the private key | no | O | – | `solana/wallet.py` | – | recommended | per deployment | wallet loader | |
| `PUMPPORTAL_API_KEY` | pumpportal feed | only for metered `subscribeTokenTrade` of held mints | **yes** | O | – | `solana/pumpportal_ws.py` via discovery | optional | optional | per deployment | Settings (`SecretStr`) | never logged (URL-borne); trading needs no key |
| `JUPITER_API_KEY` | jupiter | `api.jup.ag` quotes (keyless lite-api is deprecated) | **yes** | O | – | `solana/market_data.py` | optional | optional | per deployment | – | health: `jupiter` |
| `BINANCE_API_KEY`, `BINANCE_API_SECRET` | binance venue | signed futures calls: orders, Algo stops, fills, balance; account sync | **yes** | L (venue binance) | – | `execution/binance.py`, engine-binance-futures | – | yes | per deployment | config_validation | no withdrawal permission |
| `BINANCE_TESTNET` | binance venue | send signed calls to testnet | no | O | true | same | – | yes | per deployment | Settings; warning when live | |
| `BINANCE_SYMBOLS`, `BINANCE_STREAM_TYPES` | data-binance | ingestion opt-in | no | O | empty / kline_1m,aggTrade | `services/data-binance/app/main.py` | – | – | per deployment | – | raw getenv |
| `BYBIT_API_KEY`, `BYBIT_API_SECRET` | bybit venue | account views; live orders, position stop, executions | **yes** | L (venue bybit) | – | `execution/bybit.py`, `venues/bybit.py` | – | yes | per deployment | config_validation | |
| `BYBIT_TESTNET` | bybit venue | testnet endpoints | no | O | false | same | – | yes | per deployment | Settings; warning when live | |
| `HYPERLIQUID_ACCOUNT_ADDRESS` | hyperliquid venue / grid | the main account (read-only views, order owner) | no | L | – | `venues/hyperliquid.py`, `execution/hyperliquid.py` | – | yes | per deployment | config_validation (0x + 40 hex) | |
| `HYPERLIQUID_API_WALLET_PRIVATE_KEY` | hyperliquid venue / grid | API (agent) wallet that signs orders; cannot withdraw | **yes** | L | – | `execution/hyperliquid.py` | – | yes | per deployment | config_validation (64 hex) | `SecretStr`; never the main wallet key |
| `HYPERLIQUID_TESTNET` | hyperliquid | testnet endpoints | no | O | false | same | – | yes | per deployment | Settings; warning when live | |
| `MT5_BRIDGE_URL` | mt5 venue | private URL of services/mt5-bridge | no | P+L (venue mt5) | – | `execution/mt5_bridge.py`, `venues/mt5.py` | yes | yes | per deployment | config_validation (http/https) | |
| `MT5_BRIDGE_TOKEN` | mt5 venue | bearer token of the bridge | **yes** | P+L (venue mt5) | – | same | yes | yes | per deployment | config_validation (≥ 32 chars) | MT5 login/password stay on the bridge host |
| `META_MUSE_CONTROL_URL`, `META_MUSE_TOKEN` | control APIs | standalone meta-muse bot: status/close/config, LIVE conflict guard | token **yes** | O | – | `external_bots.py` | optional | optional | per deployment | config_validation (both or neither) | token = that bot's CONTROL_API_TOKEN |
| `GOLDVSBTC_CONTROL_URL`, `GOLDVSBTC_TOKEN` | control APIs | standalone goldvsbtc bot | token **yes** | O | – | same | optional | optional | per deployment | same | |
| `MEME_BOT_CONTROL_URL`, `MEME_BOT_TOKEN` | control APIs | standalone Meme-bot | token **yes** | O | – | same | optional | optional | per deployment | same | |
| `HYPERLIQUID_GRID_CONTROL_URL`, `HYPERLIQUID_GRID_TOKEN` | control APIs | standalone grid bot | token **yes** | O | – | same | optional | optional | per deployment | same | |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | alerts | Telegram alerts | token **yes** | O | – | `notify.py` | optional | recommended | per deployment | config_validation (both or neither) | |
| `PAPER_TRADING_PER_LEG_COST_BPS` | legacy paper | cost per leg for the pre-gate paper path | no | O | 0 | paper-trading legacy loop | – | – | per deployment | Settings | |
| `JWT_SECRET` | security | signs dashboard sessions | **yes** | R | – | `security.py` | yes | yes | per deployment | Settings (≥ 32 chars) | |
| `JWT_ALGORITHM`, `ACCESS_TOKEN_TTL_MINUTES`, `REFRESH_TOKEN_TTL_DAYS` | security | session tokens | no | O | HS256 / 15 / 7 | API auth | – | – | per deployment | Settings | |
| `ADMIN_USERNAME` | security | operator login | no | R | admin | API auth | yes | yes | per deployment | Settings | |
| `ADMIN_PASSWORD_HASH` | security | argon2 hash (never plaintext) | **yes** | R | – | API auth | yes | yes | per deployment | Settings | escape `$` as `$$` |
| `TRADING_ENABLED` | locks | lock 1 | no | R | false | `safety/store.live_trading_permitted` and all live paths | false | **true** | per deployment | Settings | |
| `LIVE_TRADING_ENABLED` | locks | lock 2 | no | R | false | same | false | **true** | per deployment | Settings | |
| `PAPER_TRADING` | locks | lock 3 (true forces paper) | no | R | true | same | true | **false** | per deployment | Settings | |
| `MAX_DAILY_LOSS`, `MAX_POSITION_SIZE`, `MAX_OPEN_POSITIONS` | legacy risk | pre-gate Phase 5 risk engine | no | O | empty | `services/decision-engine/app/evaluate.py` | – | – | per deployment | Settings | the safety gate uses DB risk settings |

## Variables that live only on other hosts

These are never put in YonixAlpha's `.env`:

| Variable | Host | Purpose | Secret |
|---|---|---|---|
| `MT5_LOGIN`, `MT5_PASSWORD`, `MT5_SERVER`, `MT5_TERMINAL_PATH` | Windows MT5 host (services/mt5-bridge) | terminal login | password **yes** |
| `MT5_BRIDGE_TOKEN`, `MT5_BRIDGE_HOST`, `MT5_BRIDGE_PORT`, `MT5_BRIDGE_MAGIC`, `MT5_BRIDGE_STATE_PATH` | same | bridge settings (the token is shared with YonixAlpha) | token **yes** |
| `CONTROL_API_TOKEN`, `CONTROL_API_PORT` | each standalone bot | that bot's control API | token **yes** |

## Changes in this audit

- **Added**, each consumed by code:
  - `PUMPPORTAL_API_KEY` (PumpPortal data WebSocket, metered trade subscriptions only);
  - `HYPERLIQUID_API_WALLET_PRIVATE_KEY`;
  - `MT5_BRIDGE_URL` and `MT5_BRIDGE_TOKEN`;
  - the four `*_CONTROL_URL` / `*_TOKEN` pairs.
- **Removed**: `MAX_SLIPPAGE`. It was declared in Settings but never consumed; slippage limits are database risk settings.
- **Not added**:
  - `JUPITER_API_SECRET`: Jupiter uses one `x-api-key`;
  - `EXCHANGE` (the user's bots' venue switch): the venue is a per-strategy database setting here;
  - `PRIVATE_KEY` / `RPC_URL` (solana-sniper-jupiter-swap-api): covered by `WALLET_PRIVATE_KEY` / `SOLANA_RPC_URL`;
  - `DASHBOARD_TOKEN` (trading-command-center): YonixAlpha's own login replaces it.
