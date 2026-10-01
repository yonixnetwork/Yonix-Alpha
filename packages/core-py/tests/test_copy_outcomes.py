"""Copy position link (§32) and paper copy outcomes (§35): pure logic."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

from yonixalpha_core import copy_outcomes as co

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
P = co.Point


def test_outcome_enters_at_the_first_price_after_we_saw_the_trade_and_exits_at_the_horizon():
    path = [P(T0 - timedelta(seconds=5), Decimal("0.5")),  # before we saw it: never our entry
            P(T0 + timedelta(minutes=1), Decimal("1.0")), P(T0 + timedelta(minutes=20), Decimal("1.8")),
            P(T0 + timedelta(minutes=59), Decimal("1.2")), P(T0 + timedelta(minutes=61), Decimal("9"))]  # after the horizon
    o = co.evaluate(T0, path)
    assert o["status"] == "EVALUATED" and o["label"] == "WOULD_HAVE_WON" and o["exit_by"] == "HORIZON"
    assert (o["simulated_entry"], o["simulated_exit"]) == ("1.0", "1.2")
    assert (o["result_pct"], o["max_return_pct"], o["min_return_pct"]) == (20.0, 80.0, 0.0)
    assert o["trades_in_window"] == 3 and "fees" in o["basis"]


def test_a_mirror_exit_uses_the_targets_sell_and_a_loss_is_a_loss():
    path = [P(T0 + timedelta(minutes=1), Decimal("1.0")), P(T0 + timedelta(minutes=30), Decimal("3.0"))]
    o = co.evaluate(T0, path, target_exit=P(T0 + timedelta(minutes=5), Decimal("0.9")))
    assert (o["exit_by"], o["label"], o["result_pct"], o["horizon_return_pct"]) == ("TARGET_SELL", "WOULD_HAVE_LOST", -10.0, 200.0)
    # a flat result is not a win
    assert co.evaluate(T0, [P(T0, Decimal(1)), P(T0 + timedelta(minutes=9), Decimal(1))])["label"] == "WOULD_HAVE_LOST"


def test_no_trade_after_we_saw_it_is_no_price_data_never_zero():
    o = co.evaluate(T0, [P(T0 - timedelta(minutes=1), Decimal(1))])
    assert o["status"] == "NO_PRICE_DATA" and "result_pct" not in o


def test_skip_classes_separate_missed_from_safety_and_settings():
    assert co.skip_class("COPIED", "paper entry") == "COPIED"
    assert co.skip_class("NOTIFIED", "notify-only target") == "NOTIFY_ONLY"
    assert co.skip_class("SKIPPED", "TOO_LATE: seen 40s after") == "MISSED"
    assert co.skip_class("FAILED", "RuntimeError: x") == "MISSED"
    assert co.skip_class("SKIPPED", "SAFETY_NOT_PASSED: safety verdict FAIL") == "BLOCKED_BY_SAFETY"
    assert co.skip_class("SKIPPED", "CHASE_GUARD: 30% above the target") == "BLOCKED_BY_SAFETY"
    assert co.skip_class("SKIPPED", "LAUNCHPAD_FILTERED: flap") == "FILTERED_BY_SETTINGS"
    assert co.skip_class("SKIPPED", "TOKEN_NOT_DISCOVERED: x") == "NOT_COPYABLE"


def test_units_and_source_transaction():
    assert co.unit_price("bsc", 10 ** 18, 10 ** 24) == Decimal("0.000001")
    assert co.unit_price("solana", 10 ** 9, 10 ** 12) == Decimal("0.000001")  # 1 SOL for 1M pump tokens
    assert co.unit_price("bsc", 0, 10) is None
    assert co.source_transaction("bsc", "bsc:0xabc:3") == "0xabc"
    assert co.source_transaction("solana", "solana:" + "f" * 64) is None


def test_link_fields_of_a_partly_exited_copy():
    entry = SimpleNamespace(target_token_amount=Decimal(10 ** 24), target_quote_amount=Decimal(10 ** 18),
                            source_event_id="bsc:0xfeed:1", latency_ms={"total": 2500})
    sells = [SimpleNamespace(target_token_amount=Decimal(5 * 10 ** 23), target_quote_amount=Decimal(6 * 10 ** 17))]
    pos = SimpleNamespace(id="p1", initial_quantity=Decimal(20000), quantity=Decimal(20000), remaining_quantity=Decimal(10000),
                          proceeds_quote=Decimal("0.024"), exit_price=None, status="open", entry_price=Decimal("0.0000011"),
                          entry_cost_quote=Decimal("0.022"), last_price=Decimal("0.000002"), realized_pnl=None)
    lk = co.link(chain="bsc", wallet="0xw", mode="MIRROR", entry_event=entry, sells=sells, position=pos,
                 target_tokens_held=Decimal(5 * 10 ** 23))
    assert lk["source_transaction"] == "0xfeed" and lk["source_position"] == {"bought": Decimal(10 ** 6),
                                                                              "still_held": Decimal(5 * 10 ** 5)}
    assert lk["copy_ratio"] == Decimal("0.02") and lk["target_entry"] == Decimal("0.000001")
    assert lk["target_exit"] == Decimal("0.0000012") and lk["target_sold_fraction"] == Decimal("0.5")
    assert lk["our_exit"] == Decimal("0.0000024") and lk["price_displacement_pct"] == 10.0
    assert lk["slippage"] is None and "live" in lk["slippage_note"] and lk["copy_latency"] == {"total": 2500}
    assert lk["pnl"] == Decimal("0.022")  # 10000 * 0.000002 + 0.024 - 0.022
