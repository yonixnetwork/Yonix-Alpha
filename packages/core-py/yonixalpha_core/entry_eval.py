"""Chronological evaluation of the entry strategies against the existing
pipeline, and their readiness states.

Splits are by time, never shuffled. Once enough labelled signals exist,
the last 20% of the labelled time range is FROZEN as the unseen test period
(stored as the platform setting `entry_eval_frozen`); it never moves
afterwards, so later tuning cannot look at it. Signals after the frozen
period are the FORWARD (shadow) period, used for drift.

  train       first 60% of the pre-freeze range (what thresholds and the
              entry-timing model may be fitted on)
  validation  next 20%
  test        the frozen 20%
  forward     everything after the frozen period

The champion is the existing pipeline: CURRENT_GATE_ENTRY (the safety gate
approved an entry), measured by the same labeller. A strategy is only
called better when, on the frozen test period, its median executable
return AND its profit factor beat the champion's, its bad-entry rate is
not higher, and its drawdown is not more than PROMOTION["max_drawdown_ratio"]
times the champion's. "Training until correct" is not a criterion: nothing
here retrains anything or moves the test period.

Readiness states (per strategy):
  INSUFFICIENT_DATA      fewer than MIN_LABELLED labelled signals
  LEARNING               labelled, but no frozen test period yet / too few
                         test signals
  VALIDATING             test signals exist; it does not beat the champion
                         (or the champion has too few test signals)
  SHADOW                 beats the champion on the frozen test period;
                         waiting for forward confirmation
  PAPER_VALIDATED        test and forward periods both pass, and its paper
                         trades (mode PAPER) are not losing
  PRODUCTION_CONTRIBUTOR not assigned by this release: live use of these
                         strategies is not enabled
  DRIFT_DETECTED         passed before, but the forward period degraded
  PAUSED                 the operator paused it
"""

from __future__ import annotations

import statistics
from datetime import datetime
from typing import Any, Iterable

from yonixalpha_core import entry_intel as ei

MIN_LABELLED = 30
MIN_TEST = 30
MIN_FORWARD = 30
MIN_FREEZE_TOTAL = 200  # labelled signals (all strategies) before the test period is frozen
FREEZE_SETTING = "entry_eval_frozen"
CHAMPION = ei.CURRENT_GATE_ENTRY
PROMOTION = {
    "median_return": "strategy median executable return > champion median",
    "profit_factor": "strategy profit factor > champion profit factor",
    "bad_entry_rate": "strategy bad-entry rate <= champion bad-entry rate",
    "max_drawdown_ratio": 1.5,
    "forward_min_median_return": 0.0,
}
STATES = ("INSUFFICIENT_DATA", "LEARNING", "VALIDATING", "SHADOW", "PAPER_VALIDATED", "PRODUCTION_CONTRIBUTOR",
          "DRIFT_DETECTED", "PAUSED")


def _ex(r: dict) -> float | None:
    v = (r.get("outcome") or {}).get("executable_return_pct")
    return None if v is None else float(v)


def metrics(rows: Iterable[dict]) -> dict[str, Any]:
    rows = sorted(rows, key=lambda r: r["decided_at"])
    rets = [x for r in rows if (x := _ex(r)) is not None]
    out: dict[str, Any] = {"signals": len(rows), "with_executable_return": len(rets)}
    if not rets:
        out["note"] = "no executable return yet (labels pending or curve math did not apply)"
        return out
    wins = [x for x in rets if x > 0]
    losses = [x for x in rets if x <= 0]
    eq, peak, mdd = 0.0, 0.0, 0.0
    for x in rets:
        eq += x
        peak = max(peak, eq)
        mdd = min(mdd, eq - peak)
    outs = [r.get("outcome") or {} for r in rows]
    bad = [o.get("bad_entry") for o in outs if o.get("bad_entry") is not None]
    late = [o.get("late_entry") for o in outs if o.get("late_entry") is not None]
    pos = [o["entry_position"] for o in outs if o.get("entry_position") is not None]
    disp = [o["latency_displacement_pct"] for o in outs if o.get("latency_displacement_pct") is not None]
    exits: dict[str, int] = {}
    for o in outs:
        if o.get("rule_exit"):
            exits[o["rule_exit"]] = exits.get(o["rule_exit"], 0) + 1
    out.update({
        "win_rate": round(len(wins) / len(rets), 4),
        "mean_return_pct": round(statistics.fmean(rets), 3),
        "median_return_pct": round(statistics.median(rets), 3),
        "profit_factor": round(sum(wins) / -sum(losses), 3) if losses and sum(losses) < 0 else None,
        "profit_factor_note": None if losses and sum(losses) < 0 else "no losing signal: profit factor undefined",
        "max_drawdown_pct_points": round(mdd, 3),
        "false_positive_rate": round(len(losses) / len(rets), 4),
        "bad_entry_rate": round(sum(1 for b in bad if b) / len(bad), 4) if bad else None,
        "late_entry_rate": round(sum(1 for x in late if x) / len(late), 4) if late else None,
        "mean_entry_position": round(statistics.fmean(pos), 4) if pos else None,
        "median_latency_displacement_pct": round(statistics.median(disp), 3) if disp else None,
        "rule_exits": exits,
        "unit": "percent of a reference-size trade, after fees, impact, latency and fixed costs",
    })
    return out


def calibration(rows: Iterable[dict], key: str = "score", bins: int = 4) -> dict[str, Any]:
    """Win rate per score (or ML probability) bucket, and the Brier score of
    the probability when key is a probability."""
    pts = [(float(r[key]) if key in r and r[key] is not None else None, (r.get("outcome") or {}).get("win")) for r in rows]
    pts = [(s, w) for s, w in pts if s is not None and w is not None]
    if not pts:
        return {"samples": 0}
    out: dict[str, Any] = {"samples": len(pts), "buckets": []}
    for i in range(bins):
        lo, hi = i / bins, (i + 1) / bins
        b = [w for s, w in pts if lo <= s < hi or (i == bins - 1 and s == 1.0)]
        out["buckets"].append({"range": [lo, hi], "n": len(b), "win_rate": round(sum(b) / len(b), 4) if b else None})
    if key.startswith("ml"):
        out["brier"] = round(statistics.fmean((s - (1.0 if w else 0.0)) ** 2 for s, w in pts), 5)
    return out


def freeze_window(labelled_times: list[datetime]) -> dict[str, str] | None:
    """The frozen test period: the last 20% of the labelled time range, once
    MIN_FREEZE_TOTAL labelled signals exist."""
    if len(labelled_times) < MIN_FREEZE_TOTAL:
        return None
    ts = sorted(labelled_times)
    start, end = ts[0], ts[-1]
    span = (end - start).total_seconds()
    if span <= 0:
        return None
    from datetime import timedelta

    return {"train_start": start.isoformat(),
            "validation_start": (start + timedelta(seconds=span * 0.6)).isoformat(),
            "test_start": (start + timedelta(seconds=span * 0.8)).isoformat(),
            "test_end": end.isoformat()}


def split(rows: list[dict], frozen: dict | None) -> dict[str, list[dict]]:
    if not frozen:
        return {"train": [], "validation": [], "test": [], "forward": [], "unsplit": rows}
    v0, t0, t1 = (datetime.fromisoformat(frozen[k]) for k in ("validation_start", "test_start", "test_end"))
    out: dict[str, list[dict]] = {"train": [], "validation": [], "test": [], "forward": []}
    for r in rows:
        at = r["decided_at"]
        key = "train" if at < v0 else "validation" if at < t0 else "test" if at <= t1 else "forward"
        out[key].append(r)
    return out


def beats(s: dict, c: dict) -> tuple[bool, list[str]]:
    """Does strategy metrics `s` beat champion metrics `c` (same test period)?"""
    why: list[str] = []
    if (s.get("with_executable_return") or 0) < MIN_TEST:
        return False, [f"{s.get('with_executable_return') or 0} test signals with a return (need {MIN_TEST})"]
    if (c.get("with_executable_return") or 0) < MIN_TEST:
        return False, [f"champion has {c.get('with_executable_return') or 0} test signals (need {MIN_TEST}): no comparison"]
    ok = True
    if not s["median_return_pct"] > c["median_return_pct"]:
        ok = False
        why.append(f"median {s['median_return_pct']}% not above champion {c['median_return_pct']}%")
    spf, cpf = s.get("profit_factor"), c.get("profit_factor")
    if spf is None or (cpf is not None and not spf > cpf):
        ok = False
        why.append(f"profit factor {spf} not above champion {cpf}")
    if s.get("bad_entry_rate") is not None and c.get("bad_entry_rate") is not None and s["bad_entry_rate"] > c["bad_entry_rate"]:
        ok = False
        why.append(f"bad-entry rate {s['bad_entry_rate']} above champion {c['bad_entry_rate']}")
    if c.get("max_drawdown_pct_points") and s["max_drawdown_pct_points"] < c["max_drawdown_pct_points"] * PROMOTION["max_drawdown_ratio"]:
        ok = False
        why.append(f"drawdown {s['max_drawdown_pct_points']} worse than {PROMOTION['max_drawdown_ratio']}x champion "
                   f"{c['max_drawdown_pct_points']}")
    if ok:
        why.append("beats the champion on median return, profit factor, bad-entry rate and drawdown")
    return ok, why


def readiness(name: str, parts: dict[str, list[dict]], champion_test: dict, mode: str,
              paper_results: dict | None = None) -> dict[str, Any]:
    if mode == "PAUSED":
        return {"state": "PAUSED", "reason": "paused by the operator"}
    labelled = sum(1 for k in parts for r in parts[k] if _ex(r) is not None)
    if labelled < MIN_LABELLED:
        return {"state": "INSUFFICIENT_DATA", "reason": f"{labelled} labelled signals (need {MIN_LABELLED})"}
    if "unsplit" in parts:
        return {"state": "LEARNING", "reason": f"the test period is frozen once {MIN_FREEZE_TOTAL} signals (all strategies) "
                                               "are labelled"}
    test = metrics(parts["test"])
    if (test.get("with_executable_return") or 0) < MIN_TEST:
        return {"state": "LEARNING", "reason": f"{test.get('with_executable_return') or 0} signals in the frozen test period "
                                               f"(need {MIN_TEST})", "test": test}
    ok, why = beats(test, champion_test)
    if not ok:
        return {"state": "VALIDATING", "reason": "; ".join(why), "test": test}
    fwd = metrics(parts["forward"])
    if (fwd.get("with_executable_return") or 0) < MIN_FORWARD:
        return {"state": "SHADOW", "reason": f"passed the frozen test; {fwd.get('with_executable_return') or 0} forward "
                                             f"signals (need {MIN_FORWARD}) before it can be validated", "test": test,
                "forward": fwd}
    if fwd["median_return_pct"] <= PROMOTION["forward_min_median_return"] or (
            (test.get("profit_factor") or 0) > 1 and (fwd.get("profit_factor") or 0) < 1):
        return {"state": "DRIFT_DETECTED", "reason": f"forward median {fwd['median_return_pct']}% / profit factor "
                                                     f"{fwd.get('profit_factor')} after passing the test", "test": test,
                "forward": fwd}
    if mode != "PAPER":
        return {"state": "SHADOW", "reason": "test and forward periods pass; switch it to PAPER to validate with paper trades",
                "test": test, "forward": fwd}
    pr = paper_results or {}
    if (pr.get("closed") or 0) < 10:
        return {"state": "SHADOW", "reason": f"{pr.get('closed') or 0} closed paper trades from this strategy (need 10)",
                "test": test, "forward": fwd, "paper": pr}
    if (pr.get("median_pnl_pct") or 0) <= 0:
        return {"state": "VALIDATING", "reason": f"paper trades median {pr.get('median_pnl_pct')}%: not validated",
                "test": test, "forward": fwd, "paper": pr}
    return {"state": "PAPER_VALIDATED", "reason": "test, forward and paper results pass. Live use is not enabled by this "
                                                  "release (PRODUCTION_CONTRIBUTOR needs a separate, explicit decision)",
            "test": test, "forward": fwd, "paper": pr}


def comparison(rows: list[dict], frozen: dict | None, modes: dict[str, str],
               paper_results: dict[str, dict] | None = None) -> dict[str, Any]:
    """Per strategy and baseline: metrics per split and readiness."""
    by: dict[str, list[dict]] = {}
    for r in rows:
        by.setdefault(r["strategy"], []).append(r)
    champ_parts = split(by.get(CHAMPION, []), frozen)
    champ_test = metrics(champ_parts.get("test", []))
    out: dict[str, Any] = {"frozen": frozen, "champion": CHAMPION, "promotion_rule": PROMOTION, "strategies": {}}
    for name in ei.ALL_RECORDED:
        parts = split(by.get(name, []), frozen)
        entry: dict[str, Any] = {"all": metrics(by.get(name, [])),
                                 "by_split": {k: metrics(v) for k, v in parts.items()},
                                 "calibration_score": calibration(by.get(name, []), "score"),
                                 "calibration_ml": calibration(by.get(name, []), "ml_probability")}
        if name in ei.STRATEGIES or name in ei.MIGRATED_VARIANTS:
            entry["readiness"] = readiness(name, parts, champ_test, modes.get(name, "SHADOW"),
                                           (paper_results or {}).get(name))
        else:
            entry["readiness"] = {"state": "BASELINE", "reason": "reference for comparison"}
        out["strategies"][name] = entry
    return out
