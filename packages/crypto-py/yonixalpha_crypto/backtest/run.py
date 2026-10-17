"""One backtest run, computed the same way everywhere: by the local runner
(backtest.local) and by the database queue worker (disabled on the server
by backtest.policy). Given the loaded candles and the run's configuration it
returns the study, its summary and the effective costs and risk, so a stored
result always says exactly which assumptions produced it."""

from __future__ import annotations

from typing import Any

from yonixalpha_crypto.backtest import data, validation
from yonixalpha_crypto.backtest.engine import Costs, RiskConfig
from yonixalpha_crypto.strategies.library import BY_ID, Series

# Allowed cost assumptions (basis points unless stated) and their ranges.
COST_RULES = {"taker_fee_bps": (0, 100), "maker_fee_bps": (0, 100), "spread_bps": (0, 200), "slippage_bps": (0, 200),
              "max_participation": (0.001, 1), "funding_bps_per_day": (0, 100), "min_notional": (0, 1000)}
RISK_KEYS = {"mode", "value", "max_position_quote", "starting_equity"}

SAVED_PARAMS_NOTE = ("non-default parameters: if they were chosen after looking at earlier out-of-sample results, "
                     "these out-of-sample figures are no longer independent evidence")


class RunConfigError(ValueError):
    pass


def costs_from(cfg: dict[str, Any] | None) -> Costs:
    cfg = cfg or {}
    for k, v in cfg.items():
        if k not in COST_RULES or not COST_RULES[k][0] <= float(v) <= COST_RULES[k][1]:
            raise RunConfigError(f"costs.{k}: unknown or outside {COST_RULES.get(k)}")
    return Costs(**{k: float(v) for k, v in cfg.items()})


def risk_from(cfg: dict[str, Any] | None) -> RiskConfig:
    cfg = cfg or {}
    if set(cfg) - RISK_KEYS:
        raise RunConfigError(f"risk: allowed keys are {sorted(RISK_KEYS)}")
    if cfg.get("mode", "PERCENT") not in ("PERCENT", "FIXED"):
        raise RunConfigError("risk.mode: PERCENT or FIXED")
    return RiskConfig(**{k: (v if k == "mode" else float(v)) for k, v in cfg.items()})


def check_params(strategy_id: str, params: dict[str, Any] | None) -> dict[str, Any]:
    """Only known parameters, inside their published bounds."""
    m = BY_ID[strategy_id]
    from yonixalpha_crypto.strategies.library import COMMON_PARAMS
    rules = {**COMMON_PARAMS, **m.params}
    out = {}
    for k, v in (params or {}).items():
        if k not in rules:
            raise RunConfigError(f"{strategy_id}: unknown parameter {k!r}")
        _default, kind, lo, hi = rules[k]
        if kind in ("int", "dec"):
            x = float(v)
            if not float(lo) <= x <= float(hi):
                raise RunConfigError(f"{strategy_id}.{k}: {v} outside {lo}..{hi}")
        out[k] = v
    return out


def execute(series: Series, prov: dict[str, Any], strategy_id: str, interval: str, run_config: dict[str, Any],
            expected_bars: int) -> dict[str, Any]:
    """Study one strategy on one (aligned) series. Never raises for a bad
    market outcome; returns status DONE or FAILED with the reason."""
    m = BY_ID[strategy_id]
    costs, risk = costs_from(run_config.get("costs")), risk_from(run_config.get("risk"))
    result = validation.study(series, m, run_config.get("params") or {}, run_config.get("exits"), costs, risk,
                              interval, optimize=bool(run_config.get("optimize", True)))
    status = "FAILED" if "error" in result else "DONE"
    if status == "DONE":
        data.apply_data_quality(result, prov, expected_bars)
        result["param_source"] = run_config.get("param_source", "defaults")
        if result["param_source"] != "defaults" and run_config.get("params"):
            result["verdict"]["reasons"].append(SAVED_PARAMS_NOTE)
    from yonixalpha_crypto.backtest.jobs import summary  # late: jobs imports the db layer
    return {"status": status, "result": result, "summary": summary(result), "error": result.get("error"),
            "costs_effective": dict(costs.__dict__), "risk_effective": dict(risk.__dict__)}
