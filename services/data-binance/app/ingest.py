from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from yonixalpha_core.schemas.market import NormalizedMarketEvent

# Field-name mappings below follow Binance's long-stable USDT-M Futures
# combined-stream payload shapes (kline/aggTrade/markPriceUpdate/depthUpdate)
# — not live-verified in this environment (no outbound network access to
# Binance from this sandbox; see ARCHITECTURE_AUDIT.md). Confirm against
# current Binance Futures WebSocket API docs before relying on this in
# production, the same caveat as app/rest/client.py.


def _to_decimal(value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(value)
    except InvalidOperation:
        return None


def normalize_stream_message(message: dict) -> NormalizedMarketEvent | None:
    """Combined-stream envelope: {"stream": "<symbol>@<type>", "data": {...}}.
    Dispatches on data["e"] (Binance's own event-type discriminator) rather
    than parsing the stream name, since "e" is the authoritative field.
    """
    data = message.get("data")
    if not data or "e" not in data:
        return None

    event_type = data["e"]
    if event_type == "kline":
        return _normalize_kline(data)
    if event_type == "aggTrade":
        return _normalize_agg_trade(data)
    if event_type == "markPriceUpdate":
        return _normalize_mark_price(data)
    if event_type == "depthUpdate":
        return _normalize_depth(data)
    return None


def _normalize_kline(data: dict) -> NormalizedMarketEvent | None:
    k = data.get("k")
    if not k:
        return None
    return NormalizedMarketEvent(
        source="binance",
        symbol=data["s"],
        snapshot_type=f"kline_{k['i']}",
        occurred_at=datetime.fromtimestamp(data["E"] / 1000, tz=timezone.utc),
        price=_to_decimal(k.get("c")),
        volume=_to_decimal(k.get("v")),
        sequence=k["t"],  # kline open time: stable, monotonic per (symbol, interval)
        payload=data,
    )


def _normalize_agg_trade(data: dict) -> NormalizedMarketEvent:
    return NormalizedMarketEvent(
        source="binance",
        symbol=data["s"],
        snapshot_type="trade",
        occurred_at=datetime.fromtimestamp(data["E"] / 1000, tz=timezone.utc),
        price=_to_decimal(data.get("p")),
        volume=_to_decimal(data.get("q")),
        sequence=data["a"],  # aggregate trade id: unique per symbol
        payload=data,
    )


def _normalize_mark_price(data: dict) -> NormalizedMarketEvent:
    return NormalizedMarketEvent(
        source="binance",
        symbol=data["s"],
        snapshot_type="mark_price",
        occurred_at=datetime.fromtimestamp(data["E"] / 1000, tz=timezone.utc),
        price=_to_decimal(data.get("p")),
        sequence=data["E"],  # event time in ms: not a guaranteed-unique id, best-effort dedup
        payload=data,
    )


def _normalize_depth(data: dict) -> NormalizedMarketEvent:
    return NormalizedMarketEvent(
        source="binance",
        symbol=data["s"],
        snapshot_type="depth",
        occurred_at=datetime.fromtimestamp(data["E"] / 1000, tz=timezone.utc),
        sequence=data["u"],  # final update ID: monotonic per symbol
        payload=data,
    )
