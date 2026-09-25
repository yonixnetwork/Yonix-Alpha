from datetime import datetime, timedelta, timezone
from decimal import Decimal

from yonixalpha_core.analytics import ClosedTrade, performance
from yonixalpha_core.strategies.catalog import CATALOG, MODE_KEYS, validate_config

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def trade(pnl: str, i: int) -> ClosedTrade:
    return ClosedTrade(Decimal(pnl), None, Decimal("0.1"), T0 + timedelta(hours=i), T0 + timedelta(hours=i, minutes=30))


def test_performance_statistics():
    s = performance([trade("10", 0), trade("-4", 1), trade("-6", 2), trade("0", 3), trade("5", 4)], Decimal(100))
    assert (s["trades"], s["wins"], s["losses"], s["breakeven"]) == (5, 2, 2, 1)
    assert s["win_rate"] == 0.4 and s["loss_rate"] == 0.4
    assert Decimal(s["profit_factor"]) == Decimal("1.5") and Decimal(s["expectancy"]) == 1
    assert Decimal(s["avg_win"]) == Decimal("7.5") and Decimal(s["avg_loss"]) == -5
    # Equity 100 -> 110 (peak) -> 106 -> 100: drawdown 10 from a 110 peak.
    assert Decimal(s["max_drawdown"]) == 10 and Decimal(s["max_drawdown_pct"]) == Decimal(10) / Decimal(110)
    assert s["avg_duration_seconds"] == 1800 and len(s["equity_curve"]) == 5


def test_performance_edge_cases_are_null_not_invented():
    empty = performance([])
    assert empty["trades"] == 0 and empty["win_rate"] is None and empty["profit_factor"] is None
    only_wins = performance([trade("3", 0)])
    assert only_wins["profit_factor"] is None and only_wins["avg_loss"] is None


def test_catalog_covers_every_strategy_and_venue():
    assert {"solana_fresh", "solana_migration", "solana_momentum", "meta_muse", "confluence_matrix", "hyperliquid_grid",
            "gold_vs_btc", "binance_futures", "bybit_futures", "hyperliquid_perps"} == set(CATALOG)
    assert "gold_vs_btc" not in MODE_KEYS


def test_config_validation_rejects_bad_input_without_coercion():
    clean, errors = validate_config("meta_muse", {"fast": 9, "slow": 21, "stop_pct": "0.02"})
    assert errors == [] and clean["stop_pct"] == "0.02"
    for bad in ({"fast": "9"}, {"fast": True}, {"stop_pct": "abc"}, {"stop_pct": "NaN"}, {"interval": "7m"},
                {"asset1": "eth usdt"}, {"api_key": "x"}, {"fast": 30}):
        assert validate_config("meta_muse", bad)[1], bad
    assert validate_config("hyperliquid_grid", {"leverage": "10"})[1]
    assert validate_config("hyperliquid_grid", {"range_mode": "manual"})[1]
    assert validate_config("hyperliquid_grid", {"range_mode": "manual", "range_lower": "90", "range_upper": "110"})[1] == []
    assert validate_config("confluence_matrix", {"killzones": [[7, 10], [12, 15]]})[1] == []
    assert validate_config("confluence_matrix", {"killzones": [[10, 7]]})[1]
    # Pump.fun strategies: optional operator exit plan, empty = automatic.
    assert validate_config("solana_fresh", {})[1] == []
    clean, errors = validate_config("solana_fresh", {"manual_stop_loss_pct": "0.2", "manual_tp1_pct": "0.5",
                                                     "manual_tp2_pct": "1", "manual_position_size_sol": None})
    assert errors == [] and clean["manual_stop_loss_pct"] == "0.2" and clean["manual_position_size_sol"] is None
    for bad in ({"manual_stop_loss_pct": "0.95"}, {"manual_stop_loss_pct": "abc"}, {"manual_stop_loss_pct": True},
                {"manual_tp2_pct": "0.5"}, {"manual_tp1_pct": "1", "manual_tp2_pct": "0.5"}, {"stop_pct": "0.1"}):
        assert validate_config("solana_migration", bad)[1], bad
