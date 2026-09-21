from functools import lru_cache
from typing import Optional

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=True)

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
    SOLANA_RPC_URL: Optional[str] = None
    SOLANA_WS_URL: Optional[str] = None
    SOLANA_RPC_BACKUP_URL: Optional[str] = None
    SOLANA_WS_BACKUP_URL: Optional[str] = None
    HELIUS_API_KEY: Optional[str] = None
    HELIUS_WEBHOOK_SECRET: Optional[str] = None
    JUPITER_API_KEY: Optional[str] = None
    SOLANA_WALLET_PRIVATE_KEY: Optional[str] = None

    # Binance (Phase 4+)
    BINANCE_API_KEY: Optional[str] = None
    BINANCE_API_SECRET: Optional[str] = None
    BINANCE_TESTNET: bool = True

    # Telegram (Phase 4/8+)
    TELEGRAM_BOT_TOKEN: Optional[str] = None
    TELEGRAM_CHAT_ID: Optional[str] = None

    # Frontend / domain
    NEXT_PUBLIC_API_URL: str = "http://localhost:8000"
    NEXT_PUBLIC_WS_URL: str = "ws://localhost:8000"
    PUBLIC_DOMAIN: str = "http://localhost:3000"

    # Logging
    LOG_LEVEL: str = "INFO"

    # Trading safety — both must be true, independently, before any live order
    # can be placed. See docs/SECURITY.md. No engine exists yet in Phase 1, so
    # these are inert here, but the flags and their fail-safe defaults are
    # established from day one so nothing downstream has to invent them later.
    TRADING_ENABLED: bool = False
    LIVE_TRADING_ENABLED: bool = False
    MAX_DAILY_LOSS: Optional[float] = None
    MAX_POSITION_SIZE: Optional[float] = None
    MAX_SLIPPAGE: Optional[float] = None
    MAX_OPEN_POSITIONS: Optional[int] = None

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
