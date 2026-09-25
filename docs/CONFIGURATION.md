# Configuration

Two kinds of configuration, kept strictly apart:

- **Secrets and deployment wiring** live in `.env` on the server (never in Git,
  never in the database, never shown by the API). They are read once at start-up
  by `yonixalpha_core.config.Settings` (plus four raw `os.getenv` reads noted below).
- **Trading behaviour** (risk limits, modes, word filters, custom rules, strategy
  parameters, operator exit plans, live-execution slippage/fees/reserve) lives in
  the database, is edited on the dashboard, is validated by the same code the
  engines use, and every change is written to the audit log.

This inventory was produced by searching the code for every `Settings` field and
every `os.getenv`/`os.environ` read (not from the README). "Tested" means an
automated test exercises the variable's effect in this repository; no test here
talks to a real external service.

## Environment variables

Req. column: **R** required, **O** optional, **L** required for live Pump.fun
execution only. Dev/Paper/Live: whether the variable is needed in that mode.

| Variable | Purpose | Req. | Dev | Paper | Live | Where used | Tested |
|---|---|---|---|---|---|---|---|
| `APP_ENV` | `production` restricts CORS to PUBLIC_DOMAIN/API URL; reported on the System page | O | – | – | – | `config.py`, `apps/api/app/main.py` | yes (API tests) |
| `APP_NAME` | service name in logs/OpenAPI | O | – | – | – | `config.py`, API | yes |
| `APP_HOST`, `APP_PORT` | API bind address | O | – | – | – | `apps/api/Dockerfile` CMD | build only |
| `JWT_SECRET` | signs dashboard sessions | R | yes | yes | yes | `yonixalpha_core/security.py` | yes |
| `JWT_ALGORITHM`, `ACCESS_TOKEN_TTL_MINUTES`, `REFRESH_TOKEN_TTL_DAYS` | session token settings (defaults HS256 / 15 / 7) | O | – | – | – | API auth | yes |
| `ADMIN_USERNAME`, `ADMIN_PASSWORD_HASH` | the single operator login (argon2 hash, never plaintext) | R | yes | yes | yes | API auth | yes |
| `POSTGRES_HOST/PORT/DB/USER/PASSWORD` | database (or set `DATABASE_URL`) | R | yes | yes | yes | `config.py`, compose | yes |
| `DATABASE_URL` | overrides the POSTGRES_* parts | O | – | – | – | `config.py` | yes |
| `REDIS_HOST/PORT/PASSWORD`, `REDIS_URL` | Redis (streams, events, readiness, caches) | R | yes | yes | yes | `config.py` | yes |
| `SOLANA_RPC_URL` (alias `HELIUS_RPC_URL`) | JSON-RPC: token/holder/authority checks, PumpSwap pool reads, funding links, live send/confirm, reconciliation | R for Solana | yes | yes | yes | discovery, decision-engine, paper-trading (pool pricing + live worker), data-solana | yes (fake RPC) |
| `SOLANA_WS_URL` (alias `HELIUS_WS_URL`) | `logsSubscribe` to the Pump.fun program (token discovery, trades, migrations) | R for Solana | yes | yes | yes | engine-solana-discovery, data-solana | yes (decoder tests) |
| `SOLANA_RPC_BACKUP_URL`, `SOLANA_WS_BACKUP_URL` | failover endpoints | O | – | – | recommended | `RpcManager`, `SolanaWsClient` | yes |
| `HELIUS_API_KEY` | if the URLs above are empty, `https://mainnet.helius-rpc.com/?api-key=…` and the `wss://` equivalent are derived | O | – | – | – | `config.py` validator | yes |
| `SOLANA_WATCHED_ADDRESSES` | extra addresses for data-solana's generic event table | O | – | – | – | `services/data-solana/app/main.py` | yes |
| `JUPITER_API_KEY` | Jupiter quote API (cross-check for migrated tokens, fallback exit quote) | O | – | – | – | assembler, paper-trading | yes (fake) |
| `WALLET_PRIVATE_KEY` (alias `SOLANA_WALLET_PRIVATE_KEY`) | signs live transactions locally; base58 64-byte or JSON array; `SecretStr`, never logged or returned | L | – | – | **yes** | `solana/wallet.py`, live worker | yes (repr/redaction, mismatch) |
| `WALLET_PUBLIC_KEY` | optional cross-check: must match the private key | O | – | – | recommended | `solana/wallet.py` | yes |
| `TRADING_ENABLED` | lock 1 | R | false | false | **true** | `safety/store.live_trading_permitted` + everywhere | yes |
| `LIVE_TRADING_ENABLED` | lock 2 | R | false | false | **true** | same | yes |
| `PAPER_TRADING` | lock 3 (true forces paper) | R | true | true | **false** | same | yes |
| `MAX_DAILY_LOSS`, `MAX_POSITION_SIZE`, `MAX_SLIPPAGE`, `MAX_OPEN_POSITIONS` | legacy Phase 5 risk engine (pre-gate path). The gate uses DB risk settings instead | O | – | – | – | `services/decision-engine/app/evaluate.py` | yes |
| `PAPER_TRADING_PER_LEG_COST_BPS` | legacy Phase 7 paper cost per leg | O | – | – | – | paper-trading legacy manage loop | yes |
| `BINANCE_API_KEY/SECRET`, `BINANCE_TESTNET` | Binance futures account sync (live futures orders are not enabled) | O | – | – | – | engine-binance-futures | yes (signed-request tests) |
| `BINANCE_SYMBOLS`, `BINANCE_STREAM_TYPES` | data-binance ingestion opt-in | O | – | – | – | `services/data-binance/app/main.py` | yes |
| `BYBIT_API_KEY/SECRET`, `BYBIT_TESTNET` | read-only Bybit account views | O | – | – | – | venues | yes |
| `HYPERLIQUID_ACCOUNT_ADDRESS`, `HYPERLIQUID_TESTNET` | read-only Hyperliquid account views (public address only) | O | – | – | – | venues | yes |
| `MIGRATION_AMM_PROGRAM_IDS` | generic AMM detector (not part of the Pump.fun sniper; ships without parsers) | O | – | – | – | engine-solana-migration | yes |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | alerts (both needed) | O | – | – | recommended | `notify.py` | yes |
| `NEXT_PUBLIC_API_URL` | API base URL baked into the web build (realtime WS URL derived from it) | R | yes | yes | yes | `apps/web/lib/api.ts`, `events.tsx` | build |
| `PUBLIC_DOMAIN` | CORS origin | R (prod) | – | – | – | `config.py` | yes |
| `LOG_LEVEL` | log verbosity | O | – | – | – | all services | yes |
| `LETSENCRYPT_EMAIL` | used only in the documented certbot command | O | – | – | – | `docs/DEPLOYMENT.md` | n/a |

Removed in this audit because nothing read them: `HELIUS_WEBHOOK_SECRET`,
`NEXT_PUBLIC_WS_URL`, and `PUMPPORTAL_API_KEY`. PumpPortal's local-transaction
API needs no key. Its data WebSocket is not used, because discovery decodes the
Pump.fun program's own logs.

## External providers

| Provider | Purpose | Key | Endpoint | WebSocket | Rate limits | Paper | Live |
|---|---|---|---|---|---|---|---|
| Solana RPC (Helius or any) | chain reads, simulate/send/confirm | in URL / `HELIUS_API_KEY` | `SOLANA_RPC_URL` | `SOLANA_WS_URL` | depends on the plan; the funding check is bounded to ≤ 2 calls per wallet + 1 per funder (cached 7 days), pool trades cached per signature (1 h) | required | required |
| PumpPortal Local Transaction API | builds the unsigned buy/sell transaction (curve `pump`, PumpSwap `pump-amm`) | none | `POST https://pumpportal.fun/api/trade-local` | – | no numeric limit found in the docs consulted; the worker sends at most one request per order attempt | not used | required |
| Jupiter | cross-check quote, fallback exit quote | `JUPITER_API_KEY` (optional) | Jupiter quote API | – | 20 req/min budget enforced client-side in paper-trading | optional | optional |
| Telegram | alerts | bot token | Bot API | – | – | optional | recommended |

PumpPortal charges 0.5% per trade (per its docs). The transaction guard refuses
any fee transfer above `max_platform_fee_bps` (default 100 bps).

## Runtime settings (database, dashboard)

| Where on the dashboard | What | Validation |
|---|---|---|
| Risk Settings | per-engine risk limits (stop range, risk per trade, max position, slippage, tax limits 5%/5%, holder/flow thresholds, funding-check width, …) | `safety/settings.py` hard limits |
| Settings → modes | global mode (OFF/PAPER/MANUAL/LIVE) and per-strategy mode (OFF/MANUAL/PAPER/AUTO); LIVE refused while locks are closed | `safety/store.py`, API |
| Word Filters & Rules | BLOCK/ALLOW word filters, scope GLOBAL/FRESH/MIGRATED, fields name/symbol/metadata/any/mint, match exact/word/substring/pattern/regex; custom threshold rules | `safety/rules.py` (regex safety, min lengths) |
| Fresh / Migrated / Momentum pages | optional operator exit plan: `manual_stop_loss_pct`, `manual_tp1..3_pct`, `manual_trailing_pct`, `manual_position_size_sol`, `manual_max_risk_sol` (empty = automatic) | `strategies/catalog.py`, then re-validated by the planner against risk settings |
| Live Execution | entry/exit slippage, per-failure exit slippage step, max exit slippage, priority fee and guard maximum, max provider fee, SOL reserve, wallet-sync max age | `live_trading.LIMITS` |

The built-in safety checks cannot be configured away: authorities, tax limits
(hard maximum 25%), sellability, liquidity, defined maximum loss, and the
environment locks.
