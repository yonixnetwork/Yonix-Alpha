from decimal import Decimal

from app.ingest import normalize_stream_message


def test_normalize_kline():
    message = {
        "stream": "btcusdt@kline_1m",
        "data": {
            "e": "kline",
            "E": 1700000000000,
            "s": "BTCUSDT",
            "k": {
                "t": 1699999940000,
                "T": 1700000000000,
                "s": "BTCUSDT",
                "i": "1m",
                "o": "50000.0",
                "c": "50050.0",
                "h": "50100.0",
                "l": "49900.0",
                "v": "10.5",
                "x": True,
            },
        },
    }
    event = normalize_stream_message(message)
    assert event is not None
    assert event.source == "binance"
    assert event.symbol == "BTCUSDT"
    assert event.snapshot_type == "kline_1m"
    assert event.price == Decimal("50050.0")
    assert event.volume == Decimal("10.5")
    assert event.sequence == 1699999940000


def test_normalize_agg_trade():
    message = {
        "stream": "btcusdt@aggTrade",
        "data": {"e": "aggTrade", "E": 1700000000000, "s": "BTCUSDT", "a": 5933014, "p": "50000.5", "q": "0.01"},
    }
    event = normalize_stream_message(message)
    assert event is not None
    assert event.snapshot_type == "trade"
    assert event.price == Decimal("50000.5")
    assert event.sequence == 5933014


def test_normalize_mark_price():
    message = {
        "stream": "btcusdt@markPrice",
        "data": {"e": "markPriceUpdate", "E": 1700000000000, "s": "BTCUSDT", "p": "50010.0", "r": "0.0001"},
    }
    event = normalize_stream_message(message)
    assert event is not None
    assert event.snapshot_type == "mark_price"
    assert event.price == Decimal("50010.0")


def test_normalize_depth():
    message = {
        "stream": "btcusdt@depth",
        "data": {
            "e": "depthUpdate",
            "E": 1700000000000,
            "s": "BTCUSDT",
            "u": 160,
            "b": [["50000.0", "1.0"]],
            "a": [["50010.0", "2.0"]],
        },
    }
    event = normalize_stream_message(message)
    assert event is not None
    assert event.snapshot_type == "depth"
    assert event.price is None
    assert event.sequence == 160


def test_normalize_unknown_event_type_returns_none():
    assert normalize_stream_message({"stream": "x", "data": {"e": "somethingNew"}}) is None


def test_normalize_missing_data_returns_none():
    assert normalize_stream_message({"stream": "x"}) is None
