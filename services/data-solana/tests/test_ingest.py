from app.ingest import normalize_logs_notification, normalize_slot_notification


def test_normalize_slot_notification():
    message = {"method": "slotNotification", "params": {"result": {"parent": 41, "root": 39, "slot": 42}}}
    event = normalize_slot_notification(message)
    assert event is not None
    assert event.source == "solana"
    assert event.snapshot_type == "slot"
    assert event.sequence == 42
    assert event.payload == {"parent": 41, "root": 39, "slot": 42}


def test_normalize_slot_notification_missing_result_returns_none():
    assert normalize_slot_notification({"method": "slotNotification", "params": {}}) is None


def test_normalize_logs_notification():
    message = {
        "method": "logsNotification",
        "params": {
            "result": {
                "context": {"slot": 100},
                "value": {"signature": "abc123", "err": None, "logs": ["Program log: hello"]},
            }
        },
    }
    event = normalize_logs_notification(message)
    assert event is not None
    assert event.source == "solana"
    assert event.snapshot_type == "log_notification"
    assert event.symbol == "abc123"
    assert event.sequence is None
    assert event.payload["value"]["signature"] == "abc123"


def test_normalize_logs_notification_missing_result_returns_none():
    assert normalize_logs_notification({"method": "logsNotification", "params": {}}) is None
