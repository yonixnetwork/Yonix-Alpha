"""Momentum v2: the unchanged v1 signal (strategies.solana.momentum_signal)
plus operator-toggleable checks, each one evaluated and recorded on every
decision and enforced only when it is switched on in the strategy config
(Strategies page). With every check off the decision is exactly v1.

The checks are pure functions of a feature dict, so the same code runs live
(features from the decision's own launch intelligence) and in replay
(features from the stored decision snapshot, tools/momentum_research).
Features are only what was known at the decision: nothing here looks at
the price after it.

A check is PASS, FAIL or UNKNOWN (input missing). An enabled check that is
UNKNOWN blocks the entry (no trade on missing critical inputs). The score is
the share of known checks that pass: an explanation, never a decision; it
cannot override a safety finding, which the gate applies separately.

Concepts (adapted, not copied; see docs/MOMENTUM_STRATEGY_RESEARCH.md):
- exhaustion cap: mako's DexScreener momentum rejects a 5 m change > 50 %;
- buyer breadth / concentration: memecoin-trading-bot's largest-buyer share,
  mako's buyer uniqueness;
- anti-dump: creator sells (memecoin-trading-bot exits on a dev sell; the
  gem finder retires the token), flow distribution / deterioration.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

PASS, FAIL, UNKNOWN = "PASS", "FAIL", "UNKNOWN"
WATCH, WAIT, QUALIFIED, REJECTED, STALE = "WATCH", "WAIT", "QUALIFIED", "REJECTED", "STALE"
CONTINUATION, PULLBACK, UNCLASSIFIED = "CONTINUATION", "PULLBACK", "UNCLASSIFIED"

# Checks whose failure means the opportunity is gone or unsafe (state
# REJECTED); the others are confirmations the candidate can still meet on a
# later evaluation (state WAIT). Either way an enabled failing check only
# refuses the entry; the gate's candidate lifecycle is unchanged.
HARD_CHECKS = ("exhaustion", "anti_dump")
CHECKS = ("exhaustion", "decay", "independent_buyers", "anti_dump", "liquidity", "qualified_wallets")
SHORT = {"exhaustion": "exh", "decay": "dec", "independent_buyers": "brd", "anti_dump": "dmp", "liquidity": "liq",
         "qualified_wallets": "qw"}

# Defaults: research starting points, not fitted values. Every one is
# editable on the Strategies page and is recorded with each decision.
DEFAULTS: dict[str, Any] = {
    "mv2_exhaustion": False, "mv2_max_return_5m": "0.5",
    "mv2_decay": False,
    "mv2_independent_buyers": False, "mv2_min_breadth": "0.5", "mv2_max_top3_share": "0.6",
    "mv2_anti_dump": False,
    "mv2_liquidity": False, "mv2_min_liquidity_sol": "5",
    "mv2_qualified_wallets": False, "mv2_min_qualified_wallets": 1,
    "monitor_when_off": True,
}
# Seconds without a trade after which the stream's view of the token is stale.
STALE_AFTER_SECONDS = 120


@dataclass
class Check:
    name: str
    status: str
    detail: str
    value: Any = None
    threshold: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status, "detail": self.detail,
                "value": self.value, "threshold": self.threshold}


@dataclass
class Evaluation:
    state: str
    qualified: bool
    version: str
    enabled: list[str]
    checks: list[Check]
    score: float | None
    entry_type: str
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"state": self.state, "qualified": self.qualified, "version": self.version, "enabled": self.enabled,
                "checks": [c.to_dict() for c in self.checks], "score": self.score, "entry_type": self.entry_type,
                "reasons": self.reasons}


def _num(v: Any) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        d = Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None
    return float(d) if d.is_finite() else None


def config_of(saved: dict[str, Any] | None) -> dict[str, Any]:
    return {**DEFAULTS, **{k: v for k, v in (saved or {}).items() if k in DEFAULTS}}


def enabled_checks(cfg: dict[str, Any]) -> list[str]:
    return [c for c in CHECKS if cfg.get(f"mv2_{c}") is True]


def version_of(cfg: dict[str, Any]) -> str:
    """'1' with every check off (the v1 decision); else '2:' + the enabled checks."""
    on = enabled_checks(cfg)
    return "1" if not on else "2:" + "+".join(SHORT[c] for c in on)


def features_from_intel(intel: dict[str, Any] | None, snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    """The v2 features from a decision's launch intelligence (solana.intel
    curve_intel) and, when given, the gate snapshot (opportunities.
    gate_snapshot). Missing inputs stay None."""
    intel = intel or {}
    snap = snapshot or {}
    mom = intel.get("momentum") or {}
    flow = intel.get("flow_state") or {}
    breadth = intel.get("buyer_breadth") or {}
    now = intel.get("now") or {}
    manip = intel.get("manipulation") or {}
    mp = intel.get("manufactured_pump") or {}
    price, high = _num(now.get("price")), _num(now.get("high"))
    # Top-3 share of the last minute's buy volume (buyer_breadth's diversity
    # = 1 - that share), not of the token's whole history.
    diversity = _num((breadth.get("components") or {}).get("diversity"))
    wallets = intel.get("wallets") if isinstance(intel.get("wallets"), dict) else {}
    qualified = wallets.get("qualified_wallets_buying") if wallets else None
    return {
        "return_1m": _num(mom.get("return_1m")), "return_5m": _num(mom.get("return_5m")),
        "volume_acceleration": _num(mom.get("volume_acceleration")),
        "buyer_acceleration": _num(mom.get("buyer_acceleration")),
        "seller_acceleration": _num(mom.get("seller_acceleration")),
        "trade_rate_acceleration": _num(mom.get("trade_rate_acceleration")),
        "new_wallet_acceleration": _num(mom.get("new_wallet_acceleration")),
        "trades_1m": _num(mom.get("trades_1m")),
        "flow_state": flow.get("state"), "sell_share": _num(flow.get("sell_share")), "new_buyers": _num(flow.get("new_buyers")),
        "breadth_score": _num(breadth.get("score")), "breadth_buyers": _num(breadth.get("unique_buyers")),
        "top3_buy_share": round(1 - diversity, 4) if diversity is not None else None,
        "price": price, "high": high,
        "distance_from_high": round(price / high - 1, 6) if price and high else None,
        "curve_progress": _num(snap.get("curve_progress")) if snap.get("curve_progress") is not None
        else _num(now.get("curve_progress")),
        "market_cap_sol": _num(snap.get("market_cap_sol")) if snap.get("market_cap_sol") is not None
        else _num(now.get("market_cap_sol")),
        "age_seconds": _num(snap.get("age_seconds")) if snap.get("age_seconds") is not None else _num(now.get("age_seconds")),
        "liquidity_sol": _num(snap.get("liquidity_sol")),
        "creator_sold": snap.get("creator_sold") if isinstance(snap.get("creator_sold"), bool) else None,
        "manipulation_level": manip.get("level"), "manufactured_pump_risk": mp.get("risk"),
        "qualified_wallets_buying": _num(qualified),
        "seconds_since_last_trade": _num(now.get("seconds_since_last_trade")),
    }


def entry_type(f: dict[str, Any]) -> str:
    """CONTINUATION: rising over 1 m and 5 m. PULLBACK: up over 5 m but the
    last minute dipped. Descriptive only (results are split by it)."""
    r1, r5 = f.get("return_1m"), f.get("return_5m")
    if r1 is None or r5 is None:
        return UNCLASSIFIED
    if r5 > 0 and r1 > 0:
        return CONTINUATION
    if r5 > 0 and r1 <= 0:
        return PULLBACK
    return UNCLASSIFIED


def _check_exhaustion(f, cfg) -> Check:
    r5, cap = f.get("return_5m"), _num(cfg["mv2_max_return_5m"])
    if r5 is None:
        return Check("exhaustion", UNKNOWN, "5 m return unknown (fewer than 5 minutes of trades)", None, cap)
    ok = r5 <= cap
    return Check("exhaustion", PASS if ok else FAIL,
                 f"5 m return {r5:+.0%} {'within' if ok else 'above'} the {cap:.0%} cap"
                 + ("" if ok else ": most of the move has already happened (too late to enter)"), r5, cap)


def _check_decay(f, cfg) -> Check:
    r1, ba, na = f.get("return_1m"), f.get("buyer_acceleration"), f.get("new_wallet_acceleration")
    if r1 is None or (ba is None and na is None):
        return Check("decay", UNKNOWN, "1 m return or buyer acceleration unknown")
    weakening = r1 > 0 and ((ba is not None and ba < 1) or (na is not None and na < 1))
    return Check("decay", FAIL if weakening else PASS,
                 (f"price rising ({r1:+.1%} in 1 m) on fewer buyers (buyer accel {ba}, new-wallet accel {na}): "
                  "momentum exhausting") if weakening else f"buyers keeping pace (buyer accel {ba}, new-wallet accel {na})",
                 {"return_1m": r1, "buyer_acceleration": ba, "new_wallet_acceleration": na})


def _check_breadth(f, cfg) -> Check:
    score, top3 = f.get("breadth_score"), f.get("top3_buy_share")
    lo, hi = _num(cfg["mv2_min_breadth"]), _num(cfg["mv2_max_top3_share"])
    if score is None or top3 is None:
        return Check("independent_buyers", UNKNOWN, "buyer breadth or top-3 buyer share unknown (too few buys)",
                     {"breadth": score, "top3_share": top3}, {"min_breadth": lo, "max_top3_share": hi})
    ok = score >= lo and top3 <= hi
    return Check("independent_buyers", PASS if ok else FAIL,
                 f"buyer breadth {score:.2f} (min {lo}), top-3 buyers {top3:.0%} of buy volume (max {hi:.0%})"
                 + ("" if ok else ": buying is concentrated in a few wallets"),
                 {"breadth": score, "top3_share": top3}, {"min_breadth": lo, "max_top3_share": hi})


def _check_anti_dump(f, cfg) -> Check:
    problems, unknown = [], []
    state = f.get("flow_state")
    if state in ("DISTRIBUTION", "DETERIORATION"):
        problems.append(f"flow {state}")
    elif state in (None, "UNKNOWN"):
        unknown.append("flow state")
    if f.get("creator_sold") is True:
        problems.append("creator sold")
    elif f.get("creator_sold") is None:
        unknown.append("creator sells")
    ba, sa = f.get("buyer_acceleration"), f.get("seller_acceleration")
    if ba is not None and sa is not None and sa > ba:
        problems.append(f"sellers accelerating faster than buyers ({sa} vs {ba})")
    if f.get("manufactured_pump_risk") == "HIGH":
        problems.append("manufactured-pump pattern HIGH")
    if problems:
        return Check("anti_dump", FAIL, "; ".join(problems), problems)
    if unknown:
        return Check("anti_dump", UNKNOWN, "unknown: " + ", ".join(unknown), unknown)
    return Check("anti_dump", PASS, "no distribution, creator sell or seller surge")


def _check_liquidity(f, cfg) -> Check:
    liq, lo = f.get("liquidity_sol"), _num(cfg["mv2_min_liquidity_sol"])
    if liq is None:
        return Check("liquidity", UNKNOWN, "usable liquidity unknown", None, lo)
    ok = liq >= lo
    return Check("liquidity", PASS if ok else FAIL, f"usable liquidity {liq:.2f} SOL (min {lo})", liq, lo)


def _check_wallets(f, cfg) -> Check:
    n, lo = f.get("qualified_wallets_buying"), int(cfg["mv2_min_qualified_wallets"])
    if n is None:
        return Check("qualified_wallets", UNKNOWN, "qualified-wallet participation not measured for this token", None, lo)
    ok = n >= lo
    return Check("qualified_wallets", PASS if ok else FAIL, f"{int(n)} qualified wallets buying (min {lo})", n, lo)


RUNNERS = {"exhaustion": _check_exhaustion, "decay": _check_decay, "independent_buyers": _check_breadth,
           "anti_dump": _check_anti_dump, "liquidity": _check_liquidity, "qualified_wallets": _check_wallets}


def evaluate(features: dict[str, Any] | None, v1_qualified: bool | None, cfg: dict[str, Any]) -> Evaluation:
    """`v1_qualified`: the unchanged v1 signal's verdict (None: it could not
    run). Every check runs; only enabled ones decide."""
    cfg = config_of(cfg)
    on = enabled_checks(cfg)
    ver = version_of(cfg)
    if not features or all(v is None for v in features.values()):
        return Evaluation(STALE, False, ver, on, [], None, UNCLASSIFIED, ["no decision-time features (stream data missing)"])
    checks = [RUNNERS[c](features, cfg) for c in CHECKS]
    known = [c for c in checks if c.status != UNKNOWN]
    score = round(sum(c.status == PASS for c in known) / len(known), 3) if known else None
    et = entry_type(features)
    stale = features.get("seconds_since_last_trade")
    if stale is not None and stale > STALE_AFTER_SECONDS:
        return Evaluation(STALE, False, ver, on, checks, score, et,
                          [f"last trade {stale:.0f}s ago (more than {STALE_AFTER_SECONDS}s): price not current"])
    by = {c.name: c for c in checks}
    failed_hard = [by[n] for n in on if n in HARD_CHECKS and by[n].status == FAIL]
    failed_soft = [by[n] for n in on if n not in HARD_CHECKS and by[n].status == FAIL]
    missing = [by[n] for n in on if by[n].status == UNKNOWN]
    if v1_qualified is None:
        return Evaluation(STALE, False, ver, on, checks, score, et, ["v1 signal could not run (token data missing)"])
    if failed_hard:
        return Evaluation(REJECTED, False, ver, on, checks, score, et, [f"{c.name}: {c.detail}" for c in failed_hard])
    if not v1_qualified:
        return Evaluation(WATCH, False, ver, on, checks, score, et, ["v1 momentum conditions not all met yet"])
    if failed_soft or missing:
        return Evaluation(WAIT, False, ver, on, checks, score, et,
                          [f"{c.name}: {c.detail}" for c in failed_soft] + [f"{c.name}: {c.detail} (required input)" for c in missing])
    return Evaluation(QUALIFIED, True, ver, on, checks, score, et,
                      ["v1 conditions met" + (f"; enabled checks passed: {', '.join(on)}" if on else " (no v2 check enabled)")])
