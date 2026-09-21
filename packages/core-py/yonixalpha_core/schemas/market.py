from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel

Source = Literal["binance", "solana"]


class NormalizedMarketEvent(BaseModel):
    """The common shape every data-ingestion service (data-solana,
    data-binance, and whatever follows) normalizes its source-specific
    messages into before writing to `market_snapshots`. Keeping this in
    packages/core-py rather than duplicated per service is what makes
    "normalized" mean something — two services producing the same shape
    independently would drift the first time either one changes it.

    `price`/`volume` are populated only when the source event unambiguously
    carries them (a Binance kline close price, a trade price) — never
    computed or inferred here. `sequence` is a source-native monotonic
    identifier used for de-duplication (a Binance kline's open_time, a
    Solana slot number); leave it None when the source has no such id
    rather than inventing one, since a wrong one would silently break dedup.
    """

    source: Source
    symbol: str
    snapshot_type: str
    occurred_at: datetime
    price: Decimal | None = None
    volume: Decimal | None = None
    sequence: int | None = None
    payload: dict[str, Any]
