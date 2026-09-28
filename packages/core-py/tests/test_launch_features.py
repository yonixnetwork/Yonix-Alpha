"""Causal launch features and the manipulation score, on synthetic but
curve-exact trade sequences (standard pump.fun constant product)."""

from datetime import datetime, timedelta, timezone

from yonixalpha_core.solana import launch_features as lf
from yonixalpha_core.solana import manipulation as mp
from yonixalpha_core.solana.flow import Trade

T0 = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
SOL = 1_000_000_000


class Curve:
    def __init__(self, vsol=lf.STANDARD_VSOL0, vtok=lf.STANDARD_VTOK0):
        self.vsol, self.vtok = vsol, vtok

    def trade(self, wallet: str, at: datetime, sol: float, buy: bool = True) -> Trade:
        lam = int(sol * SOL)
        k = self.vsol * self.vtok
        if buy:
            self.vsol += lam
            new_tok = k // self.vsol
            tokens = self.vtok - new_tok
            self.vtok = new_tok
        else:
            self.vsol -= lam
            new_tok = k // self.vsol
            tokens = new_tok - self.vtok
            self.vtok = new_tok
        return Trade(at, wallet, buy, lam, tokens, self.vsol, self.vtok)


def launch(pattern, curve=None):
    """pattern: list of (seconds, wallet, sol, buy)."""
    c = curve or Curve()
    return [c.trade(w, T0 + timedelta(seconds=s), sol, buy) for s, w, sol, buy in pattern]


def organic(n=40, every=1.5, size=0.3):
    return launch([(i * every, f"w{i}", size + (i % 7) * 0.05, i % 5 != 4) for i in range(n)])


# --- regime ---------------------------------------------------------------------------

def test_curve_math_applies_only_to_standard_non_mayhem_curves():
    trades = organic(10)
    assert lf.curve_math(trades, False)["valid"] is True
    assert lf.curve_math(trades, True)["valid"] is False  # Mayhem: never computed
    assert lf.curve_math(trades, None)["valid"] is None  # unknown flag is not "safe"
    odd = launch([(1, "a", 1, True)], Curve(vsol=3_860_000_000, vtok=1_163_000_000_000_000))  # observed Mayhem-like state
    r = lf.curve_math(odd, False)
    assert r["valid"] is False and "constant product" in r["reason"]


def test_instant_bond_and_regime_tags():
    assert lf.instant_bond(1000, 1000) is True and lf.instant_bond(1000, 1600) is False and lf.instant_bond(None, 5) is None
    assert lf.data_regime(datetime(2026, 7, 20, tzinfo=timezone.utc)) == "pre_boost" and lf.data_regime(T0) == "post_boost"
    assert lf.boost_window(T0, T0 + timedelta(seconds=60)) is True and lf.boost_window(T0, T0 + timedelta(seconds=400)) is False


# --- snapshots ------------------------------------------------------------------------

def test_snapshots_are_causal_and_measure_the_curve():
    trades = organic(40)
    series = lf.snapshot_series(trades, T0, T0 + timedelta(seconds=25), supply_raw=10**15, curve_valid=True)
    assert [s["offset_seconds"] for s in series] == [0, 5, 10, 20]  # 30 and 60 are in the future
    s20 = series[-1]
    assert s20["trades"] == len([t for t in trades if t.at <= T0 + timedelta(seconds=20)])
    # Trades after t never change what was recorded for t.
    future = launch([(30 + i, f"f{i}", 5.0, False) for i in range(5)])
    at20 = lf.snapshot(trades, T0, T0 + timedelta(seconds=20), supply_raw=10**15, curve_valid=True)
    assert lf.snapshot(trades + future, T0, T0 + timedelta(seconds=20), supply_raw=10**15, curve_valid=True) == at20
    assert 0 < s20["curve_progress"] < 1 and s20["sol_accumulated"] > 0 and s20["distance_to_graduation_sol"] > 0
    assert s20["market_cap_sol"] > 0 and s20["unique_buyers"] > 5 and "price_velocity" in s20
    assert s20["buyer_growth"] >= 0 and s20["top3_buy_share"] < 1


def test_curve_values_are_unknown_not_zero_when_curve_math_does_not_apply():
    s = lf.snapshot(organic(10), T0, T0 + timedelta(seconds=20), curve_valid=False)
    assert "curve_progress" not in s and "curve_progress" in s["unknown"] and "market_cap_sol" in s["unknown"]


# --- trade efficiency -----------------------------------------------------------------

def test_few_meaningful_trades_reach_a_level_in_fewer_trades_than_many_tiny_ones():
    big = launch([(i, f"b{i}", 0.5, True) for i in range(12)])  # 6 SOL in 12 trades
    tiny = launch([(i * 0.2, f"t{i % 9}", 0.05, True) for i in range(130)])  # 6.5 SOL in 130 trades, 9 wallets
    end = T0 + timedelta(seconds=60)
    a = lf.trades_to_reach(big, end, complete_history=True, curve_valid=True, checkpoints=(5,))["checkpoints"]["5"]
    b = lf.trades_to_reach(tiny, end, complete_history=True, curve_valid=True, checkpoints=(5,))["checkpoints"]["5"]
    assert a["trades"] == 10 and b["trades"] == 100
    assert a["meaningful_buy_ratio"] == 1.0 and b["unique_buyers"] == 9
    none = lf.trades_to_reach(big, end, complete_history=False, curve_valid=True)
    assert none["unknown"] == "trade history from creation not held"


def test_coverage_detects_a_history_that_does_not_start_at_the_opening_reserve():
    trades = organic(20)
    assert lf.coverage(trades, int(T0.timestamp()), int(T0.timestamp()) - 5)["complete"] is True
    assert lf.coverage(trades[5:], int(T0.timestamp()), None)["complete"] is False
    assert lf.coverage(trades, int(T0.timestamp()), int(T0.timestamp()) + 60)["complete"] is False


# --- breadth and flow -----------------------------------------------------------------

def test_buyer_breadth_rewards_many_distinct_meaningful_buyers_not_transaction_count():
    broad = organic(40)
    churn = launch([(i * 0.5, f"bot{i % 3}", 0.01, True) for i in range(80)])
    t = T0 + timedelta(seconds=40)
    b = lf.buyer_breadth(broad, t)
    c = lf.buyer_breadth(churn, t)
    assert b["score"] > c["score"] and c["components"]["non_repeat"] == 0
    assert lf.buyer_breadth(churn, t, recycled_wallets={"bot0"})["components"]["non_recycled"] < 1


def test_falling_volume_with_new_buyers_and_a_holding_price_is_consolidation_not_deterioration():
    c = Curve()
    first = [c.trade(f"a{i}", T0 + timedelta(seconds=i), 1.0, True) for i in range(30)]
    calm = [c.trade(f"n{i}", T0 + timedelta(seconds=62 + i * 6), 0.2, True) for i in range(9)]
    r = lf.flow_state(first + calm, T0 + timedelta(seconds=120))
    assert r["state"] == lf.HEALTHY_CONSOLIDATION, r

    c = Curve()
    up = [c.trade(f"a{i}", T0 + timedelta(seconds=i * 2), 1.0, True) for i in range(30)]
    dump = [c.trade(f"s{i}", T0 + timedelta(seconds=62 + i * 3), 1.5, False) for i in range(15)]
    r = lf.flow_state(up + dump, T0 + timedelta(seconds=120))
    assert r["state"] == lf.DETERIORATION, r


def test_momentum_acceleration_compares_the_last_minute_with_the_one_before():
    c = Curve()
    slow = [c.trade(f"a{i}", T0 + timedelta(seconds=i * 10), 0.2, True) for i in range(6)]
    fast = [c.trade(f"b{i}", T0 + timedelta(seconds=61 + i * 2), 0.4, True) for i in range(25)]
    m = lf.momentum(slow + fast, T0 + timedelta(seconds=120))
    assert m["trade_rate_acceleration"] > 3 and m["buyer_acceleration"] > 3 and m["return_1m"] > 0


def test_post_migration_states():
    mig = T0
    c = Curve()
    dump = [c.trade(f"s{i}", mig + timedelta(seconds=10 + i * 5), 0.5, i % 4 == 0) for i in range(20)]
    assert lf.post_migration_state(dump, mig, mig + timedelta(seconds=110))["state"] == lf.DUMPING
    c = Curve()
    down = [c.trade(f"s{i}", mig + timedelta(seconds=5 + i * 3), 0.6, False) for i in range(15)]
    up = [c.trade(f"b{i}", mig + timedelta(seconds=120 + i * 4), 0.5, True) for i in range(15)]
    r = lf.post_migration_state(down + up, mig, mig + timedelta(seconds=180))
    assert r["state"] == lf.RECOVERING and r["off_low"] > 0.1 and r["boost_window"] is True
    assert lf.post_migration_state([], mig, mig + timedelta(seconds=30))["state"] == lf.UNKNOWN


# --- manipulation ---------------------------------------------------------------------

def test_one_indicator_is_never_high_manipulation():
    c = Curve()
    sync = [c.trade(f"s{i}", T0 + timedelta(seconds=5), 0.2, True) for i in range(6)]  # 6 wallets, same second, same size
    rest = [c.trade(f"o{i}", T0 + timedelta(seconds=6 + i * 3), 0.1 + i * 0.05, True) for i in range(10)]
    r = mp.score(sync + rest, T0 + timedelta(seconds=40))
    assert r["level"] == "LOW" and list(r["families"]) == ["synchronized_buys"]


def test_several_independent_families_are_high_with_evidence():
    c = Curve()
    trades = []
    for i in range(20):  # the same 4 wallets buy and sell identical sizes, rising in a straight line
        trades.append(c.trade(f"w{i % 4}", T0 + timedelta(seconds=i * 2), 0.1, True))
    for i in range(3):
        trades.append(c.trade(f"w{i}", T0 + timedelta(seconds=41), 0.05, False))
    for i in range(3):  # and back out a second time: repeated in-and-out
        trades.append(c.trade(f"w{i}", T0 + timedelta(seconds=43 + i), 0.05, False))
    r = mp.score(trades, T0 + timedelta(seconds=45), duplicate_of="EARLIERmint")
    assert r["level"] == "HIGH", r
    assert {"wash_trading", "regular_trade_sizes", "synchronized_sells", "copycat_name"} <= set(r["families"])


def test_too_few_trades_is_unknown_and_funding_needs_checked_wallets():
    r = mp.score(organic(4), T0 + timedelta(seconds=10))
    assert r["level"] == "UNKNOWN"
    r = mp.score(organic(4), T0 + timedelta(seconds=10), funding={"checked": 0, "creator_linked": 3})
    assert "creator_linked_funding" not in r["families"]
    r = mp.score(organic(4), T0 + timedelta(seconds=10), funding={"checked": 5, "creator_linked": 2, "largest_group": 0})
    assert r["level"] == "LOW" and "creator_linked_funding" in r["families"]


def test_a_single_flip_per_wallet_is_not_wash_trading():
    c = Curve()
    ins = [c.trade(f"s{i}", T0 + timedelta(seconds=i), 0.3 + i * 0.01, True) for i in range(12)]
    outs = [c.trade(f"s{i}", T0 + timedelta(seconds=20 + i), 0.2, False) for i in range(12)]  # every buyer flips once
    assert mp.repeated_round_trip_share(ins + outs) == 0
    r = mp.score(ins + outs, T0 + timedelta(seconds=40))
    assert "wash_trading" not in r["families"]
    again = [c.trade(f"s{i}", T0 + timedelta(seconds=35 + i), 0.3, True) for i in range(4)]
    out2 = [c.trade(f"s{i}", T0 + timedelta(seconds=39 + i), 0.2, False) for i in range(4)]
    assert mp.repeated_round_trip_share(ins + outs + again + out2) > 0.3
