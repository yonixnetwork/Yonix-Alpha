from decimal import Decimal
from functools import lru_cache
from typing import Optional

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # env_parse_none_str: an env var present but set to "" (as .env.example's
    # optional fields document leaving them) is treated as unset (None)
    # rather than failed-to-parse for non-str Optional fields like
    # MAX_DAILY_LOSS/MAX_OPEN_POSITIONS.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=True, env_parse_none_str="")

    # Application
    APP_ENV: str = "development"
    APP_NAME: str = "YonixAlpha"
    APP_PORT: int = 8000
    APP_HOST: str = "0.0.0.0"

    # Security
    JWT_SECRET: str
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_TTL_MINUTES: int = 15
    REFRESH_TOKEN_TTL_DAYS: int = 7
    ADMIN_USERNAME: str = "admin"
    ADMIN_PASSWORD_HASH: str

    # Database
    POSTGRES_HOST: str = "postgres"
    POSTGRES_PORT: int = 5432
    POSTGRES_DB: str = "yonixalpha"
    POSTGRES_USER: str = "yonixalpha"
    POSTGRES_PASSWORD: str = ""
    DATABASE_URL: Optional[str] = None

    # Redis
    REDIS_HOST: str = "redis"
    REDIS_PORT: int = 6379
    REDIS_PASSWORD: Optional[str] = None
    REDIS_URL: Optional[str] = None

    # Solana (Phase 2/3+)
    # HELIUS_RPC_URL / HELIUS_WS_URL are accepted as aliases (the names the
    # reference migration bot uses); with only HELIUS_API_KEY set, the
    # Helius mainnet endpoints are derived from it.
    SOLANA_RPC_URL: Optional[str] = Field(None, validation_alias=AliasChoices("SOLANA_RPC_URL", "HELIUS_RPC_URL"))
    SOLANA_WS_URL: Optional[str] = Field(None, validation_alias=AliasChoices("SOLANA_WS_URL", "HELIUS_WS_URL"))
    SOLANA_RPC_BACKUP_URL: Optional[str] = None
    # Further backups, tried in order after SOLANA_RPC_BACKUP_URL (use a
    # different provider for each, so one rate limit can't stop them all).
    SOLANA_RPC_BACKUP_URL_2: Optional[str] = None
    SOLANA_RPC_BACKUP_URL_3: Optional[str] = None
    SOLANA_WS_BACKUP_URL: Optional[str] = None
    # Optional: key for encrypting credentials entered in the dashboard
    # (provider URLs). Derived from JWT_SECRET when unset.
    CONFIG_ENCRYPTION_KEY: Optional[str] = None
    HELIUS_API_KEY: Optional[str] = None

    # EVM chains (multi-chain phase): comma-separated RPC URLs tried in order
    # before the chain's public endpoints. URLs may embed API keys; they are
    # only ever shown as scheme://host.
    BSC_RPC_URLS: Optional[str] = None
    ROBINHOOD_RPC_URLS: Optional[str] = None
    # One EVM account for both BSC and Robinhood Chain. The address alone is
    # enough (watch-only: balances); the private key is optional, server-only,
    # never logged or returned, and only checked against the address — EVM
    # LIVE execution is not implemented.
    # Optional Etherscan API V2 key: first-funder lookups of BSC wallets for the
    # launch-coordination check (Robinhood Chain uses its public Blockscout).
    ETHERSCAN_API_KEY: Optional[str] = None
    # Optional GitHub token (read-only, no scopes needed) for the update monitor:
    # 5,000 API requests per hour instead of 60. Never logged or returned.
    GITHUB_TOKEN: Optional[SecretStr] = None
    # Optional paid wallet-intelligence APIs (master §24-25): enrichment only,
    # switched on in the dashboard with a daily call budget. Never logged or returned.
    NANSEN_API_KEY: Optional[SecretStr] = None
    MADEONSOL_API_KEY: Optional[SecretStr] = None
    EVM_WALLET_ADDRESS: Optional[str] = None
    EVM_WALLET_PRIVATE_KEY: Optional[SecretStr] = None
    JUPITER_API_KEY: Optional[str] = None

    # Live Solana execution (only used when every live lock is open).
    # The private key signs transactions locally and never leaves the
    # process; it is a SecretStr so it can't be printed by accident.
    # SOLANA_WALLET_PRIVATE_KEY is the older name, still accepted.
    WALLET_PUBLIC_KEY: Optional[str] = None
    WALLET_PRIVATE_KEY: Optional[SecretStr] = Field(
        None, validation_alias=AliasChoices("WALLET_PRIVATE_KEY", "SOLANA_WALLET_PRIVATE_KEY"))

    # PumpPortal data WebSocket (wss://pumpportal.fun/api/data). New-token
    # and migration subscriptions are free and need no key; the key is only
    # sent for the metered token-trade subscriptions (PumpPortal charges
    # per message from the key's linked wallet). Trading uses PumpPortal's
    # Local Transaction API, which needs no key: YonixAlpha signs locally.
    PUMPPORTAL_API_KEY: Optional[SecretStr] = None

    # Telegram (wired up Phase 11 — yonixalpha_core.notify.send_telegram_alert)
    TELEGRAM_BOT_TOKEN: Optional[str] = None
    TELEGRAM_CHAT_ID: Optional[str] = None

    # Simulated trading cost charged to EACH leg of a paper position, in
    # basis points of that leg's notional (entry and exit are charged
    # separately). Defaults to 0, which means paper PnL is GROSS — no fee,
    # spread, or price impact.
    #
    # 0 is deliberately not a claim that trading is free. It is a refusal
    # to invent a number: a realistic Solana/Jupiter round-trip cost cannot
    # be verified from this codebase's environment, and a fabricated
    # constant would quietly propagate into every ML label. It matters
    # because a label is literally `realized_pnl > 0`, so at 0 bps a trade
    # that only cleared costs on paper still trains the model as a winner.
    # Set this to a measured value before trusting paper results.
    PAPER_TRADING_PER_LEG_COST_BPS: Decimal = Decimal(0)

    # Frontend / domain
    NEXT_PUBLIC_API_URL: str = "http://localhost:8000"
    PUBLIC_DOMAIN: str = "http://localhost:3000"

    # Logging
    LOG_LEVEL: str = "INFO"

    # Dashboard API: a single database statement is stopped after this long
    # (Postgres statement_timeout on the api's own connections only) so a slow
    # page answers with a clear error before the reverse proxy's 60 s limit,
    # and its connection is freed instead of piling up behind it. Workers keep
    # no limit. 0 = no limit.
    API_STATEMENT_TIMEOUT_MS: int = 25_000
    # Longest wait for a free pooled connection before the request fails with
    # DB_POOL_EXHAUSTED instead of queueing until the proxy gives up.
    API_POOL_TIMEOUT_S: int = 10
    # The review pages' aggregates (ML Review, opportunity comparison, EVM ML)
    # are computed in the background on their own small pool with this longer
    # limit; a request is answered from the last result meanwhile
    # (apps/api review_cache). Server 2026-10-07: they took over 120 s under load.
    API_REVIEW_STATEMENT_TIMEOUT_MS: int = 240_000

    # Low-resource operation (2026-10-08: 2 GB / 2 vCPU droplet with a 16 GB
    # database). yonixalpha_core.operating_mode. These are the defaults; the
    # dashboard can change the mode and the copy-trading status at runtime
    # (stored in platform_settings), never past the resume thresholds below.
    SYSTEM_RESOURCE_MODE: str = "LOW_RESOURCE"  # NORMAL | LOW_RESOURCE | EMERGENCY
    COPY_TRADING_STATUS: str = "SUSPENDED"  # ACTIVE | THROTTLED | SUSPENDED
    # Resource level (yonixalpha_core.resources): WARNING / CRITICAL when any
    # of these is crossed. CRITICAL pauses background (priority 3) work only.
    RESOURCE_WARN_AVAILABLE_MB: int = 400
    RESOURCE_CRITICAL_AVAILABLE_MB: int = 200
    RESOURCE_WARN_LOAD_PER_CPU: float = 1.5
    RESOURCE_CRITICAL_LOAD_PER_CPU: float = 3.0
    RESOURCE_WARN_MEMORY_PRESSURE_PCT: float = 5.0  # Linux PSI memory "some" avg60
    RESOURCE_CRITICAL_MEMORY_PRESSURE_PCT: float = 20.0
    # Copy trading may only be resumed while all of these hold.
    COPY_RESUME_MIN_FREE_RAM_MB: int = 1024
    COPY_MAX_CPU_LOAD: float = 1.0  # load average (1 min) per CPU
    COPY_MAX_SWAP_USAGE_MB: int = 256
    COPY_MAX_DB_LATENCY_MS: int = 250
    # ML training (not inference) in LOW_RESOURCE mode. Memecoin ML (Solana,
    # BSC, Robinhood) keeps its normal schedule and only waits while the
    # resource level is CRITICAL (checked again within the hour); copy-trading
    # wallet ML pauses with copy trading. >0: train at most once per this many
    # hours instead (operator, 2026-10-08: memecoin ML must keep working).
    ML_TRAINING_INTERVAL_LOW_RESOURCE_H: int = 0
    # Database connections per worker process (the API sets its own): kept
    # open / extra allowed in bursts. Each Postgres connection is a process.
    DB_POOL_SIZE: int = 5
    DB_MAX_OVERFLOW: int = 10

    # Trading safety — both must be true, independently, before any live order
    # can be placed. See docs/SECURITY.md. No engine exists yet in Phase 1, so
    # these are inert here, but the flags and their fail-safe defaults are
    # established from day one so nothing downstream has to invent them later.
    TRADING_ENABLED: bool = False
    LIVE_TRADING_ENABLED: bool = False
    # A third, independent lock: while true, every executable decision is
    # routed to the paper engine whatever the stored global/strategy modes
    # say. Live execution needs TRADING_ENABLED and LIVE_TRADING_ENABLED true
    # AND this false (yonixalpha_core.safety.store.live_trading_permitted).
    PAPER_TRADING: bool = True
    # LIVE_EXECUTION_SMOKE_TEST (yonixalpha_core.live_smoke): an execution
    # verification tool, not a strategy. Off unless this is true in .env AND
    # an admin arms a run on the Live page with the admin password and a typed
    # confirmation. It never switches the global mode, spends at most
    # LIVE_SMOKE_TEST_MAX_SOL per buy (no default: unset = cannot arm) and at
    # most LIVE_SMOKE_TEST_MAX_TRADES buys in total, and every buy passes the
    # full safety gate against the live wallet. Not editable from the dashboard.
    LIVE_SMOKE_TEST_ENABLED: bool = False
    LIVE_SMOKE_TEST_MAX_SOL: Optional[Decimal] = None
    LIVE_SMOKE_TEST_MAX_TRADES: int = 1
    MAX_DAILY_LOSS: Optional[float] = None
    MAX_POSITION_SIZE: Optional[float] = None
    MAX_OPEN_POSITIONS: Optional[int] = None

    @model_validator(mode="after")
    def derive_helius_endpoints(self) -> "Settings":
        if self.HELIUS_API_KEY:
            if not self.SOLANA_RPC_URL:
                self.SOLANA_RPC_URL = f"https://mainnet.helius-rpc.com/?api-key={self.HELIUS_API_KEY}"
            if not self.SOLANA_WS_URL:
                self.SOLANA_WS_URL = f"wss://mainnet.helius-rpc.com/?api-key={self.HELIUS_API_KEY}"
        return self

    @field_validator("JWT_SECRET")
    @classmethod
    def jwt_secret_must_be_strong(cls, v: str) -> str:
        if len(v) < 32:
            raise ValueError("JWT_SECRET must be at least 32 characters — generate with `openssl rand -hex 32`")
        return v

    @property
    def database_url(self) -> str:
        if self.DATABASE_URL:
            return self.DATABASE_URL
        return (
            f"postgresql+asyncpg://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    @property
    def redis_url(self) -> str:
        if self.REDIS_URL:
            return self.REDIS_URL
        auth = f":{self.REDIS_PASSWORD}@" if self.REDIS_PASSWORD else ""
        return f"redis://{auth}{self.REDIS_HOST}:{self.REDIS_PORT}/0"

    @property
    def is_production(self) -> bool:
        return self.APP_ENV == "production"

    @property
    def cors_origins(self) -> list[str]:
        origins = {self.PUBLIC_DOMAIN, self.NEXT_PUBLIC_API_URL}
        if not self.is_production:
            origins.update({"http://localhost:3000", "http://127.0.0.1:3000"})
        return sorted(o for o in origins if o)


@lru_cache
def get_settings() -> Settings:
    return Settings()
