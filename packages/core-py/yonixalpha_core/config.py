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
    SOLANA_WS_BACKUP_URL: Optional[str] = None
    HELIUS_API_KEY: Optional[str] = None
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

    # Binance USDⓈ-M futures. Read + trade permission when live futures
    # execution is used; never enable withdrawals. BINANCE_TESTNET=true
    # points every signed call at testnet.binancefuture.com.
    BINANCE_API_KEY: Optional[str] = None
    BINANCE_API_SECRET: Optional[str] = None
    BINANCE_TESTNET: bool = True

    # Bybit V5 linear perpetuals. A READ-ONLY key is enough for account
    # views; live execution needs Contract "Orders" + "Positions" trade
    # permission. Never enable withdrawals.
    BYBIT_API_KEY: Optional[str] = None
    BYBIT_API_SECRET: Optional[str] = None
    BYBIT_TESTNET: bool = False

    # Hyperliquid. The public account address is enough for read-only
    # views. Live execution signs with an API ("agent") wallet approved for
    # that account at app.hyperliquid.xyz/API — an agent wallet can trade
    # but cannot withdraw. Never put the main wallet's key here.
    HYPERLIQUID_ACCOUNT_ADDRESS: Optional[str] = None
    HYPERLIQUID_API_WALLET_PRIVATE_KEY: Optional[SecretStr] = None
    HYPERLIQUID_TESTNET: bool = False

    # MetaTrader 5, through services/mt5-bridge on the Windows host that
    # runs the MT5 terminal. MT5_LOGIN / MT5_PASSWORD / MT5_SERVER live in
    # the BRIDGE's environment only; this server knows just the bridge URL
    # and its bearer token.
    MT5_BRIDGE_URL: Optional[str] = None
    MT5_BRIDGE_TOKEN: Optional[SecretStr] = None

    # External bots' control APIs (CONTROL_API_CONTRACT v1 of
    # trading-command-center): status / close / config of the standalone
    # bots, if they still run. Each token equals THAT bot's own
    # CONTROL_API_TOKEN. Leave unset when the bots are not deployed —
    # YonixAlpha runs these strategies natively.
    META_MUSE_CONTROL_URL: Optional[str] = None
    META_MUSE_TOKEN: Optional[SecretStr] = None
    GOLDVSBTC_CONTROL_URL: Optional[str] = None
    GOLDVSBTC_TOKEN: Optional[SecretStr] = None
    MEME_BOT_CONTROL_URL: Optional[str] = None
    MEME_BOT_TOKEN: Optional[SecretStr] = None
    HYPERLIQUID_GRID_CONTROL_URL: Optional[str] = None
    HYPERLIQUID_GRID_TOKEN: Optional[SecretStr] = None

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
