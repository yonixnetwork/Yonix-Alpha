from types import SimpleNamespace

from yonixalpha_core.ml.gate_features import FUTURES_FEATURES, MODEL_FOR_ENGINE, explain, vector


def test_vector_never_defaults_missing_features():
    f = {"volatility": "0.01", "liquidity_quote": "1000", "spread_bps": "2", "signal_strength": 0.8, "side": "SHORT"}
    v = vector("gate_futures", f)
    assert v == {"volatility": 0.01, "liquidity_quote": 1000.0, "spread_bps": 2.0, "signal_strength": 0.8, "side_long": 0.0}
    assert vector("gate_futures", {**f, "spread_bps": None}) is None
    assert vector("gate_futures", {**f, "volatility": "nan"}) is None
    assert vector("gate_futures", {k: v for k, v in f.items() if k != "signal_strength"}) is None


def test_every_engine_maps_to_a_feature_set():
    for engine, model in MODEL_FOR_ENGINE.items():
        assert model.startswith("gate_"), engine


def test_explain_only_for_linear_models():
    lin = SimpleNamespace(coef_=[[0, 0, 0, 2.0, -1.0]])
    values = dict(zip(FUTURES_FEATURES, [0.01, 1000, 2, 0.8, 1.0]))
    top = explain(lin, FUTURES_FEATURES, values, top=2)
    assert [t["feature"] for t in top] == ["signal_strength", "side_long"]
    assert top[0]["contribution"] == 1.6
    assert explain(SimpleNamespace(), FUTURES_FEATURES, values) is None
