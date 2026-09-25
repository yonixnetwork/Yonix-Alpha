"""Feature sets for models trained on safety-gate decisions, shared by the
trainer (services/ml) and inference (decision-engine) so the two can never
disagree about columns.

Feature snapshots are written at decision time (safety.pipeline.
record_ml_sample) from inputs observed up to that moment; labels are added
only when the resulting paper position closes. Nothing observed after the
decision can be in a feature vector (spec §89).
"""

import math
from typing import Any

FEATURE_VERSION = "gate-features-v2"
DRIFT_FLAG_PREFIX = "yx:ml:drift:"

SOLANA_FEATURES = [
    "liquidity_quote", "age_seconds", "volatility", "top1_share", "top10_share", "creator_share", "unique_buyers",
    "trade_count", "buy_sell_volume_ratio", "top3_wallet_volume_share", "sync_buy_cluster", "round_trip_share",
    "window_volume",
]
FUTURES_FEATURES = ["volatility", "liquidity_quote", "spread_bps", "signal_strength", "side_long"]

MODEL_FOR_ENGINE = {
    "solana_fresh": "gate_solana_fresh",
    "solana_momentum": "gate_solana_momentum",
    "solana_migration": "gate_solana_migration",
    "binance_futures": "gate_futures",
    "bybit_futures": "gate_futures",
    "hyperliquid_perps": "gate_futures",
}
FEATURES_FOR_MODEL = {
    "gate_solana_fresh": SOLANA_FEATURES,
    "gate_solana_momentum": SOLANA_FEATURES,
    "gate_solana_migration": ["liquidity_quote", "volatility", "top1_share", "top10_share", "trade_count"],
    "gate_futures": FUTURES_FEATURES,
}
ENGINES_FOR_MODEL: dict[str, list[str]] = {}
for _engine, _model in MODEL_FOR_ENGINE.items():
    ENGINES_FOR_MODEL.setdefault(_model, []).append(_engine)


def _num(v: Any) -> float | None:
    if v is None or v == "" or v == "None":
        return None
    if v in ("LONG", "SHORT"):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def vector(model_name: str, features: dict[str, Any]) -> dict[str, float] | None:
    """Named numeric features for `model_name`, or None if any is missing
    or non-numeric — never defaulted (a fabricated 0 would be a lie the
    model then learns from)."""
    f = dict(features or {})
    if "side" in f and "side_long" not in f:
        f["side_long"] = 1.0 if f["side"] == "LONG" else 0.0
    out: dict[str, float] = {}
    for name in FEATURES_FOR_MODEL[model_name]:
        x = _num(f.get(name))
        if x is None:
            return None
        out[name] = x
    return out


def explain(estimator: Any, feature_names: list[str], values: dict[str, float], top: int = 3) -> list[dict] | None:
    """Per-feature log-odds contributions (coefficient × value) for linear
    models, the only kind where this decomposition is exact. Returns None
    for anything else rather than inventing an explanation."""
    coef = getattr(estimator, "coef_", None)
    if coef is None or len(coef) != 1 or len(coef[0]) != len(feature_names):
        return None
    contribs = [{"feature": n, "value": values[n], "contribution": float(c) * values[n]} for n, c in zip(feature_names, coef[0])]
    contribs.sort(key=lambda d: abs(d["contribution"]), reverse=True)
    return contribs[:top]
