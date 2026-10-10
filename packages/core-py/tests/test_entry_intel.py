"""Early-entry intelligence (2026-10-10), pure functions only: the feature
engine, the momentum phase, the three entry strategies, the outcome labels,
the chronological evaluation / readiness, the latency split, the event
trigger and the migrated variants. Deterministic replays of synthetic
pump.fun curve trades (constant product from the standard opening
reserves); no network, no database."""

from datetime import datetime, timedelta

from yonixalpha_core import entry_eval
from yonixalpha_core import entry_intel as ei
from yonixalpha_core import entry_outcomes as eo
from yonixalpha_core import entry_timing as et
from yonixalpha_core import gate_events as ge

from yonixalpha_core.testing.curve_sim import T0, Curve  # noqa: E402

CREATED = int(T0.timestamp())


def feats(c: Curve, at: float, **kw):
    return ei.early_features(c.trades, T0 + timedelta(seconds=at), created_ts=CREATED, creator=kw.pop("creator", "creator"),
                             complete_history=True, fee_bps=kw.pop("fee_bps", 125), **kw)


def accelerating_launch() -> Curve:
    c = Curve()
    for i in range(6):  # a slow start: 6 distinct buyers over 12 s
        c.buy(1 + 2 * i, f"a{i}", 0.12)
    for i in range(10):  # then demand accelerates: more buyers, bigger buys
        c.buy(14 + i * 0.6, f"b{i}", 0.25 + 0.02 * i)
    return c


# --- feature engine ---------------------------------------------------------------------

def test_features_use_only_trades_at_or_before_the_decision():
    c = accelerating_launch()
    c.buy(40, "late", 5.0)  # after the decision time: must not count
    f = feats(c, 20)
    assert f["trades_total"] == 16
    assert all(t.at <= T0 + timedelta(seconds=20) for t in c.trades[:16])
    assert f["net_inflow_sol_total"] < 5.0
    assert f["age_seconds"] == 20.0 and f["feature_version"] == ei.FEATURE_VERSION
    assert f["w10"]["net_sol"] > f["w10_prev"]["net_sol"] > 0
    assert f["round_trip_cost_pct"] is not None and f["fee_assumed"] is False


def test_no_trade_yet_is_unknown_not_zero():
    f = ei.early_features([], T0 + timedelta(seconds=5), created_ts=CREATED)
    assert f["trades_total"] == 0 and "price" in f["unknown"] and "w10" not in f
    res = ei.evaluate(f, ei.EntryConfig())
    assert res["phase"] == ei.UNCLEAR
    assert res["strategies"][ei.EARLY_ACCELERATION]["decision"] == ei.WAIT


# --- strategy A: early acceleration ---------------------------------------------------------

def test_early_acceleration_is_a_candidate_with_limited_evidence_sizing():
    res = ei.evaluate(feats(accelerating_launch(), 20), ei.EntryConfig())
    a = res["strategies"][ei.EARLY_ACCELERATION]
    assert res["phase"] == ei.EARLY_ACCEL_PHASE
    assert a["decision"] == ei.CANDIDATE, a["reasons"]
    assert a["evidence_level"] == ei.MODERATE and a["size_factor"] == 0.5
    assert 0 < a["score"] <= 1


def test_false_acceleration_from_tiny_repeated_trades_waits():
    c = Curve()
    for i in range(4):
        c.buy(1 + i, f"real{i}", 0.3)
    for i in range(40):  # three bots churning 0.001 SOL buys
        c.buy(6 + i * 0.3, f"bot{i % 3}", 0.001)
    a = ei.evaluate(feats(c, 19), ei.EntryConfig())["strategies"][ei.EARLY_ACCELERATION]
    assert a["decision"] in (ei.WAIT, ei.NO_TRADE)
    assert any("tiny repeats" in r for r in a["reasons"])


def test_large_initial_buy_followed_by_selling_is_no_trade():
    c = accelerating_launch()
    c.buy(20.5, "whale", 2.0)
    c.sell(25, "whale", 0.8)
    a = ei.evaluate(feats(c, 27), ei.EntryConfig())["strategies"][ei.EARLY_ACCELERATION]
    assert a["decision"] == ei.NO_TRADE
    assert any("large buyer sold" in r for r in a["reasons"])


def test_rising_buyers_with_declining_net_inflow_waits():
    c = Curve()
    for i in range(8):
        c.buy(1 + i * 1.2, f"big{i}", 0.4)
    for i in range(12):  # more buyers in the last 10 s, but tiny buys and sells
        c.buy(11 + i * 0.7, f"small{i}", 0.02)
    c.sell(18, "big0", 1.0)
    c.sell(19, "big1", 1.0)
    a = ei.evaluate(feats(c, 20), ei.EntryConfig())["strategies"][ei.EARLY_ACCELERATION]
    assert a["decision"] in (ei.WAIT, ei.NO_TRADE)
    assert any("inflow" in r for r in a["reasons"])


def test_price_increase_from_one_wallet_is_weak_organic_demand():
    c = Curve()
    for i in range(5):
        c.buy(1 + i, f"x{i}", 0.15)
    c.buy(12, "pumper", 3.0)  # one wallet moves the price
    f = feats(c, 15)
    a = ei.evaluate(f, ei.EntryConfig())["strategies"][ei.EARLY_ACCELERATION]
    assert f["w10"]["top_buyer_share"] >= 0.7
    assert a["decision"] != ei.CANDIDATE
    assert any("one wallet" in r or "meaningful" in r for r in a["reasons"])


def test_slippage_that_removes_the_expected_profit_refuses():
    a = ei.evaluate(feats(accelerating_launch(), 20), ei.EntryConfig(max_round_trip_cost_pct=0.5))
    s = a["strategies"][ei.EARLY_ACCELERATION]
    assert s["decision"] == ei.NO_TRADE and any("round-trip cost" in r for r in s["reasons"])


def test_stale_stream_data_refuses():
    f = feats(accelerating_launch(), 20, stream_heartbeat=T0 - timedelta(seconds=30))
    s = ei.evaluate(f, ei.EntryConfig())["strategies"][ei.EARLY_ACCELERATION]
    assert s["decision"] == ei.NO_TRADE and any("stale" in r for r in s["reasons"])


def test_creator_sell_refuses_every_strategy():
    c = accelerating_launch()
    c.buy(0.5, "creator", 0.5)
    c.sell(19, "creator", 1.0)
    res = ei.evaluate(feats(c, 20), ei.EntryConfig())
    for name in (ei.EARLY_ACCELERATION, ei.MOMENTUM_CONTINUATION):
        assert res["strategies"][name]["decision"] == ei.NO_TRADE


# --- strategy C: momentum continuation and exhaustion -------------------------------------------

def continuation() -> Curve:
    c = Curve()
    for i in range(20):
        c.buy(1 + i * 2, f"e{i}", 0.2)
    for i in range(7):  # a real pullback (about 9% off the high on the curve)
        c.sell(44 + i, f"e{i}", 1.0)
    for i in range(14):  # renewed broad demand
        c.buy(70 + i * 3, f"r{i}", 0.25)
    return c


def test_momentum_continuation_candidate():
    res = ei.evaluate(feats(continuation(), 112), ei.EntryConfig())
    m = res["strategies"][ei.MOMENTUM_CONTINUATION]
    assert res["phase"] == ei.HEALTHY_CONTINUATION, res["phase_evidence"]
    assert m["decision"] == ei.CANDIDATE, m["reasons"]


def test_exhausted_momentum_is_not_a_continuation():
    c = Curve()
    for i in range(30):
        c.buy(1 + i * 1.5, f"p{i}", 0.8)  # a big run-up
    for i in range(3):
        c.buy(60 + i * 4, f"q{i}", 0.05)  # demand fading at the top
    res = ei.evaluate(feats(c, 75), ei.EntryConfig())
    assert res["phase"] in (ei.EXHAUSTED, ei.UNCLEAR, ei.DISTRIBUTION)
    assert res["strategies"][ei.MOMENTUM_CONTINUATION]["decision"] != ei.CANDIDATE
    assert res["strategies"][ei.EARLY_ACCELERATION]["decision"] == ei.NO_TRADE


def test_distribution_phase():
    c = continuation()
    for i in range(7, 19):
        c.sell(115 + i, f"e{i}", 1.0)
    phase, ev = ei.momentum_phase(feats(c, 127))
    assert phase == ei.DISTRIBUTION and ev


# --- strategy B: smart wallets -------------------------------------------------------------------

def test_smart_wallets_never_trigger_alone():
    f = feats(Curve(), 5)
    wallets = {"status": "MEASURED", "proven_entries": [{"wallet": "w1", "seconds_ago": 3}], "proven_exits": []}
    s = ei.evaluate(f, ei.EntryConfig(), wallets)["strategies"][ei.SMART_WALLET_CONFIRMATION]
    assert s["decision"] == ei.WAIT and "never a trigger alone" in s["reasons"][0]


def test_smart_wallet_confirms_a_candidate_but_missing_history_waits():
    f = feats(accelerating_launch(), 20)
    ok = ei.evaluate(f, ei.EntryConfig(), {"status": "MEASURED", "proven_entries": [{"wallet": "w1aaaaaa", "seconds_ago": 5}],
                                            "proven_exits": [], "coordinated": False})
    assert ok["strategies"][ei.SMART_WALLET_CONFIRMATION]["decision"] == ei.CANDIDATE
    missing = ei.evaluate(f, ei.EntryConfig(), {"status": "UNKNOWN", "reason": "no history"})
    s = missing["strategies"][ei.SMART_WALLET_CONFIRMATION]
    assert s["decision"] == ei.WAIT and "insufficient" in s["reasons"][0]


def test_smart_wallet_entry_followed_by_immediate_dump_refuses():
    f = feats(accelerating_launch(), 20)
    res = ei.evaluate(f, ei.EntryConfig(), {"status": "MEASURED", "proven_entries": [{"wallet": "w1aaaaaa", "seconds_ago": 5}],
                                            "proven_exits": [{"wallet": "w1aaaaaa"}]})
    assert res["strategies"][ei.SMART_WALLET_CONFIRMATION]["decision"] == ei.NO_TRADE


def test_coordinated_wallets_refuse():
    f = feats(accelerating_launch(), 20)
    res = ei.evaluate(f, ei.EntryConfig(), {"status": "MEASURED", "proven_entries": [{"wallet": "w1aaaaaa", "seconds_ago": 5}],
                                            "proven_exits": [], "coordinated": True})
    assert res["strategies"][ei.SMART_WALLET_CONFIRMATION]["decision"] == ei.NO_TRADE


# --- config ------------------------------------------------------------------------------------------

def test_config_validation_and_bool_parsing():
    assert ei.validate_config({"ea_max_age_seconds": 5, "nope": 1})
    assert not ei.validate_config({"ea_max_age_seconds": 120, "event_min_interval_seconds": 10})
    assert ei.EntryConfig.from_dict({"event_reevaluation": "false"}).event_reevaluation is False
    assert ei.EntryConfig.from_dict({"ea_min_trades": "x"}).ea_min_trades == ei.EntryConfig().ea_min_trades
    assert ei.naive_sampled("abc", 1) is True


# --- labels (no leakage: decision data before, outcome after) -------------------------------------------

def test_label_enters_after_latency_and_measures_timing_against_the_peak():
    c = accelerating_launch()
    for i in range(10):
        c.buy(22 + i, f"pump{i}", 1.0)  # the move after the decision
    for i in range(10):
        c.sell(60 + i, f"pump{i}", 1.0)
    out = eo.label_fresh(c.trades, T0 + timedelta(seconds=20), T0 + timedelta(seconds=1200), latency_s=3.0,
                         latency_source="test", fee_bps=125, mayhem=False, migrated_at=None, phase_at_decision=ei.EARLY_ACCEL_PHASE)
    assert out["entry_price_raw"] >= out["decision_price_raw"]  # entry happens after the decision
    assert out["latency_displacement_pct"] >= 0
    assert out["mfe_pct"] > 0 and out["rule_exit"] in ("TAKE_PROFIT", "STOP_LOSS", "TIME_EXIT")
    assert 0 <= out["entry_position"] < 0.7 and out["late_entry"] is False
    assert out["decelerating_at_entry"] is False
    assert out["executable_return_pct"] is not None and out["win"] == (out["executable_return_pct"] > 0)


def test_late_entry_near_the_peak_is_labelled_late():
    c = Curve()
    for i in range(25):
        c.buy(1 + i, f"p{i}", 0.6)
    for i in range(10):
        c.sell(40 + i, f"p{i}", 1.0)
    out = eo.label_fresh(c.trades, T0 + timedelta(seconds=25), T0 + timedelta(seconds=1200), latency_s=1.0,
                         latency_source="test", fee_bps=125, mayhem=False, migrated_at=None, phase_at_decision=ei.EXHAUSTED)
    assert out["late_entry"] is True and out["decelerating_at_entry"] is True
    assert out["executable_return_pct"] < 0 and out["win"] is False


def test_migration_during_observation_is_recorded_in_the_label():
    c = accelerating_launch()
    out = eo.label_fresh(c.trades, T0 + timedelta(seconds=10), T0 + timedelta(seconds=1200), latency_s=1.0, latency_source="t",
                         fee_bps=125, mayhem=False, migrated_at=T0 + timedelta(seconds=15))
    assert out["migrated_within_horizon"] is True
    assert out["executable_return_pct"] is None and "migrated" in out["executable_unknown"]


def test_unknown_fee_is_never_assumed_in_the_label():
    out = eo.label_fresh(accelerating_launch().trades, T0 + timedelta(seconds=10), T0 + timedelta(seconds=1200), latency_s=1.0,
                         latency_source="t", fee_bps=None, mayhem=False, migrated_at=None)
    assert out["executable_return_pct"] is None and "fee" in out["executable_unknown"]


# --- migrated variants -----------------------------------------------------------------------------------

def test_migrated_variants_trigger_on_their_own_conditions():
    mig = T0.timestamp()
    samples = [(mig + 0, 1000, 100), (mig + 30, 900, 112), (mig + 60, 950, 106), (mig + 90, 1000, 101),
               (mig + 120, 980, 104), (mig + 150, 900, 115), (mig + 180, 850, 125)]
    trig = eo.migrated_triggers(samples, mig, mig + 200)
    assert trig[ei.MIGRATED_IMMEDIATE] == mig and trig[ei.MIGRATED_NO_TRADE] == mig
    assert trig[ei.MIGRATED_DELAYED_CONFIRMATION] >= mig + 120
    assert trig[ei.MIGRATED_PULLBACK] > mig + 60
    assert ei.MIGRATED_CONTINUATION in trig
    again = eo.migrated_triggers(samples, mig, mig + 200, done=set(trig))
    assert again == {}


def test_migrated_label_uses_the_pool_fee_or_says_why_not():
    mig = T0.timestamp()
    samples = [(mig + i * 30, 1_000_000 - i * 20_000, 100_000_000_000 + i * 3_000_000_000) for i in range(20)]
    lab = eo.label_migrated(samples, mig + 30, mig + 2000, latency_s=3, latency_source="t", fee_bps=30,
                            strategy=ei.MIGRATED_IMMEDIATE)
    assert lab["executable_return_pct"] is not None and lab["win"] is True
    nofee = eo.label_migrated(samples, mig + 30, mig + 2000, latency_s=3, latency_source="t", fee_bps=None,
                              strategy=ei.MIGRATED_IMMEDIATE)
    assert nofee["executable_return_pct"] is None and nofee["theoretical_return_pct"] > 0
    base = eo.label_migrated(samples, mig, mig + 2000, latency_s=3, latency_source="t", fee_bps=30, strategy=ei.MIGRATED_NO_TRADE)
    assert base["executable_return_pct"] == 0.0


# --- evaluation and readiness ---------------------------------------------------------------------------

def _rows(strategy: str, rets: list[float], start: datetime, step: int = 60) -> list[dict]:
    return [{"strategy": strategy, "decided_at": start + timedelta(seconds=i * step), "score": 0.5,
             "outcome": {"executable_return_pct": r, "bad_entry": r < -20, "late_entry": False, "entry_position": 0.3,
                         "win": r > 0, "rule_exit": "TIME_EXIT"}} for i, r in enumerate(rets)]


def test_metrics_profit_factor_drawdown_and_insufficient_samples():
    m = entry_eval.metrics(_rows("X", [10, -5, 20, -5, 3], T0))
    assert m["win_rate"] == 0.6 and m["profit_factor"] == round(33 / 10, 3)
    assert m["max_drawdown_pct_points"] == -5.0
    assert entry_eval.metrics([])["with_executable_return"] == 0
    st = entry_eval.readiness("X", {"unsplit": _rows("X", [1] * 5, T0)}, {}, "SHADOW")
    assert st["state"] == "INSUFFICIENT_DATA"


def test_frozen_test_period_and_promotion_against_the_champion():
    champion = _rows(ei.CURRENT_GATE_ENTRY, [-4, 2, -6, 1] * 60, T0)
    better = _rows(ei.EARLY_ACCELERATION, [6, -3, 8, 2] * 60, T0 + timedelta(seconds=7))
    frozen = entry_eval.freeze_window([r["decided_at"] for r in champion + better])
    assert frozen is not None and frozen["test_start"] > frozen["validation_start"]
    out = entry_eval.comparison(champion + better, frozen, {ei.EARLY_ACCELERATION: "SHADOW"})
    st = out["strategies"][ei.EARLY_ACCELERATION]["readiness"]
    assert st["state"] in ("SHADOW", "DRIFT_DETECTED", "PAPER_VALIDATED") or "forward" in st["reason"]
    worse = _rows(ei.MOMENTUM_CONTINUATION, [-8, 1, -9, 1] * 60, T0 + timedelta(seconds=11))
    out2 = entry_eval.comparison(champion + worse, frozen, {})
    assert out2["strategies"][ei.MOMENTUM_CONTINUATION]["readiness"]["state"] in ("VALIDATING", "LEARNING")
    assert out["strategies"][ei.CURRENT_GATE_ENTRY]["readiness"]["state"] == "BASELINE"


def test_paused_strategy_and_no_freeze_with_few_signals():
    assert entry_eval.readiness("X", {"unsplit": []}, {}, "PAUSED")["state"] == "PAUSED"
    assert entry_eval.freeze_window([T0] * 10) is None


# --- latency split and events ------------------------------------------------------------------------------

def test_waiting_split_attributes_time_to_the_blocking_cause():
    evals = [{"at": 0, "executable": False, "codes": ["VOLATILITY_UNAVAILABLE"]},
             {"at": 30, "executable": False, "codes": ["SIGNAL_NOT_QUALIFIED"]},
             {"at": 60, "executable": False, "codes": ["MAX_OPEN_POSITIONS"]},
             {"at": 75, "executable": True, "codes": []}]
    w = et.waiting_split(evals)
    assert w["seconds"] == {"strategy": 30.0, "data": 30.0, "risk": 15.0, "execution": 0.0, "safety": 0.0}
    assert et.wait_category("FREEZE_AUTHORITY") == "safety" and et.wait_category("ENTRY_IMPACT") == "execution"


def test_derived_latencies_never_invent_missing_points():
    d = et.derive({"launch_observed_at": 100.0, "event_received_at": 101.5, "signal_created_at": 113.0})
    assert d["detection_latency_s"] == 1.5 and d["promotion_latency_s"] == 11.5
    assert d["quote_latency_s"] is None and d["launch_to_entry_s"] is None


def test_meaningful_events_and_route_change():
    prev = {"trades_total": 10, "net_inflow_sol_total": 2.0, "unique_sellers": 1, "creator_sold": False,
            "sol_accumulated": 3.0, "phase": ei.EARLY_ACCEL_PHASE, "max_buy_sol_10s": 0.1, "migrated": False}
    same = dict(prev)
    assert ge.meaningful(prev, same) == []
    cur = dict(prev, trades_total=14, net_inflow_sol_total=3.5, max_buy_sol_10s=0.8, unique_sellers=7, creator_sold=True,
               migrated=True, phase=ei.DISTRIBUTION)
    why = " ".join(ge.meaningful(prev, cur))
    for word in ("significant buy", "net inflow", "phase", "seller surge", "creator sold", "migration"):
        assert word in why
    assert ge.meaningful(None, cur) == []
    fp = ge.fingerprint({"features": {"trades_total": 3, "w10": {"max_buy_sol": 0.6}}, "phase": "UNCLEAR"})
    assert fp["trades_total"] == 3 and fp["max_buy_sol_10s"] == 0.6


# --- the entry-timing model (pure-Python inference) -------------------------------------------------------

def test_logistic_inference_matches_the_exported_coefficients_and_rejects_others():
    n = len(ei.MODEL_FEATURES)
    coef = {"features": list(ei.MODEL_FEATURES), "medians": [0.0] * n, "means": [0.0] * n, "scales": [1.0] * n,
            "weights": [0.0] * n, "intercept": 0.0}
    assert ei.logistic_predict(coef, [None] * n) == 0.5
    coef["weights"][0] = 1.0
    assert ei.logistic_predict(coef, [2.0] + [None] * (n - 1)) > 0.8
    assert ei.logistic_predict({**coef, "features": ["other"]}, [1.0]) is None
    vec = ei.model_vector({"age_seconds": 12, "creator_sold": True}, 0.4)
    assert vec[0] == 12.0 and vec[-1] == 0.4 and len(vec) == n
