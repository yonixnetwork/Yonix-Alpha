import hashlib
import hmac

from app.auth import build_signed_query


def test_signature_matches_independent_hmac_computation():
    params = {"symbol": "BTCUSDT", "side": "BUY", "timestamp": 1700000000000}
    secret = "test-secret-key"

    result = build_signed_query(params, secret)
    query_part, _, signature_part = result.rpartition("&signature=")

    expected_signature = hmac.new(secret.encode(), query_part.encode(), hashlib.sha256).hexdigest()
    assert signature_part == expected_signature


def test_signature_changes_with_different_secret():
    params = {"symbol": "BTCUSDT", "timestamp": 1700000000000}
    sig1 = build_signed_query(params, "secret-one")
    sig2 = build_signed_query(params, "secret-two")
    assert sig1 != sig2


def test_signature_changes_with_different_params():
    secret = "test-secret-key"
    sig1 = build_signed_query({"symbol": "BTCUSDT"}, secret)
    sig2 = build_signed_query({"symbol": "ETHUSDT"}, secret)
    assert sig1 != sig2


def test_query_string_preserves_param_order():
    params = {"symbol": "BTCUSDT", "side": "BUY", "quantity": 1.5}
    result = build_signed_query(params, "secret")
    query_part = result.split("&signature=")[0]
    assert query_part == "symbol=BTCUSDT&side=BUY&quantity=1.5"


def test_signature_is_deterministic():
    params = {"symbol": "BTCUSDT", "timestamp": 1700000000000}
    secret = "test-secret-key"
    assert build_signed_query(params, secret) == build_signed_query(params, secret)
