"""Wallet validation (§26): every check reported with value / requirement;
not enough history is INSUFFICIENT_DATA (never a verdict); one lucky trade
or one good day does not validate a wallet; settings are validated."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from yonixalpha_core import wallet_validation as wv
from yonixalpha_core.wallet_pnl import ClosedTrade

T0 = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)


def ct(i: int, cost: str, proceeds: str, day: int) -> ClosedTrade:
    at = T0 + timedelta(days=day, minutes=i)
    return ClosedTrade(f"tok{i}", Decimal(cost), Decimal(proceeds), at - timedelta(minutes=10), at, Decimal(1))


def run(closed, trades=None, days=None, cfg=wv.ValidationConfig()):
    times = [c.opened_at for c in closed] + [c.closed_at for c in closed]
    return wv.validate(closed, trades=trades if trades is not None else 2 * len(closed),
                       unique_tokens=len({c.token for c in closed}), trade_times=times, cfg=cfg)


def test_consistent_wallet_over_several_days_is_validated():
    closed = [ct(i, "1", "1.3" if i % 3 else "0.9", day=i % 5) for i in range(15)]
    r = run(closed)
    assert r["status"] == wv.VALIDATED, r["reason"]
    assert all(c["pass"] for c in r["checks"]) and r["active_days"] == 5
    names = {c["check"] for c in r["checks"]}
    assert set(wv.HISTORY_CHECKS) <= names and "max_single_trade_share" in names


def test_too_little_history_is_insufficient_data_with_the_missing_checks():
    r = run([ct(i, "1", "2", day=0) for i in range(3)])
    assert r["status"] == wv.INSUFFICIENT and "min_closed" in r["reason"] and "min_active_days" in r["reason"]


def test_one_lucky_trade_and_one_good_day_do_not_validate():
    closed = [ct(i, "1", "0.95", day=i % 5) for i in range(14)] + [ct(99, "1", "40", day=0)]
    r = run(closed)
    by = {c["check"]: c for c in r["checks"]}
    assert r["status"] == wv.NOT_VALIDATED
    assert by["max_single_trade_share"]["pass"] is False and by["min_median_return_pct"]["pass"] is False
    assert by["min_profitable_period_share"]["value"] == 0.2  # 1 of 5 active days profitable


def test_settings_are_parsed_and_bad_values_reported():
    cfg, errors = wv.parse_config({"min_closed": "25", "max_single_trade_share": "0.4"})
    assert cfg.min_closed == 25 and cfg.max_single_trade_share == 0.4 and not errors
    _, errors = wv.parse_config({"nope": 1, "min_trades": "x", "max_single_trade_share": 2, "min_closed": -1})
    assert len(errors) == 4


def test_discovery_never_copies_and_follows_validated_wallets_on_paper():
    assert wv.discovery_status({"status": wv.VALIDATED}, {"evaluated": 4})["stage"] == "PAPER_FOLLOWED"
    assert wv.discovery_status({"status": wv.VALIDATED}, None)["stage"] == "VALIDATED"
    assert wv.discovery_status({"status": wv.NOT_VALIDATED}, None)["stage"] == "REJECTED"
    s = wv.discovery_status({"status": wv.INSUFFICIENT}, None)
    assert s["stage"] == "COLLECTING_HISTORY" and "never copied automatically" in s["note"]
