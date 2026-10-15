"""YonixAlpha Crypto Trading engine: regular crypto strategies on Bybit and
Binance spot market data, with simulated (DEMO) execution only.

Isolated from the Memecoin engine by design:
- own package (not packages/core-py, which every Memecoin image copies);
- own Postgres schema `crypto` with its own Alembic history;
- own worker (services/crypto-trading) and Redis keys (`yx:crypto:*`);
- no exchange credentials and no order-sending code: market data comes
  from public GET endpoints only (publichttp.PublicHttp).
"""

SCHEMA = "crypto"
REDIS_PREFIX = "yx:crypto:"
