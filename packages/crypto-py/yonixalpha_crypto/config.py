"""Connection settings for the Crypto Trading engine, read from the same
environment the other services use (.env). Only the database and Redis are
read here; there are no exchange keys anywhere in this package."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class CryptoSettings:
    database_url: str
    redis_url: str
    loop_seconds: float = 30.0  # config re-read while idle
    health_every_seconds: float = 600.0  # public exchange health check
    base_urls: tuple[tuple[str, str], ...] = ()  # (exchange id, base url) overrides


def _flt(env: Mapping[str, str], key: str, default: float, lo: float, hi: float) -> float:
    try:
        v = float(env.get(key, default))
    except ValueError:
        return default
    return min(hi, max(lo, v))


def load_settings(env: Mapping[str, str] | None = None) -> CryptoSettings:
    env = os.environ if env is None else env
    db = env.get("DATABASE_URL") or (
        f"postgresql+asyncpg://{env.get('POSTGRES_USER', 'yonixalpha')}:{env.get('POSTGRES_PASSWORD', '')}"
        f"@{env.get('POSTGRES_HOST', 'postgres')}:{env.get('POSTGRES_PORT', '5432')}/{env.get('POSTGRES_DB', 'yonixalpha')}")
    if env.get("REDIS_URL"):
        redis = env["REDIS_URL"]
    else:
        pw = env.get("REDIS_PASSWORD")
        redis = f"redis://{':' + pw + '@' if pw else ''}{env.get('REDIS_HOST', 'redis')}:{env.get('REDIS_PORT', '6379')}/0"
    overrides = tuple((ex, env[f"CRYPTO_{ex.upper()}_BASE_URL"]) for ex in ("bybit", "binance", "binance_us")
                      if env.get(f"CRYPTO_{ex.upper()}_BASE_URL"))
    return CryptoSettings(
        database_url=db,
        redis_url=redis,
        loop_seconds=_flt(env, "CRYPTO_LOOP_SECONDS", 30.0, 5.0, 300.0),
        health_every_seconds=_flt(env, "CRYPTO_HEALTH_EVERY_SECONDS", 600.0, 60.0, 86400.0),
        base_urls=overrides,
    )
