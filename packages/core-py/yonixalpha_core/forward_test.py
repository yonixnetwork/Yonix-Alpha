"""Forward test: a timestamped evaluation period with frozen rules, compared
with the baseline before it (seven-day performance investigation,
2026-10-24).

A period is started by the operator after a verified deployment
(tools.forward_test start, or the dashboard). It stores, at the start:

  started_at / label / started_by
  deployment     the revision scripts/deploy.sh recorded (record_deployment)
  fingerprint    one short hash per configuration component: global mode,
                 each strategy's mode and config, the latest risk-settings
                 version per scope, the paper / live / exit / ML-contribution
                 settings, the active model versions
  configuration  the readable values behind the fingerprint (modes, versions)
  data_sources   the scanner health at the start (scanner_health)
  rules          RULES, frozen: a report always judges a period by the rules
                 it started with, never by today's defaults

Earlier periods are kept (history), never deleted or overwritten; starting a
new period ends the previous one at that moment.

The report (report()) measures every Solana strategy, PAPER and LIVE apart,
with solana_performance on closed trades only:

  forward     since the start (daily rows and the rolling last 7 days)
  baseline    the BASELINE_DAYS before the start, the same strategy and mode
  evidence    closed trades, distinct tokens, elapsed days against the rules;
              the share of the net result that came from the single best trade
  uncertainty a 95 % bootstrap interval of the net PnL per trade, and of the
              difference to the baseline (deterministic seed)
  verdict     INSUFFICIENT EVIDENCE   a rule is not met yet (the reasons say which)
              IMPROVING               the interval of the difference is above 0
              DETERIORATING           the interval of the difference is below 0
              INCONCLUSIVE            enough trades, but the interval includes 0
              (no baseline trades: NO BASELINE, the forward numbers only)
  changes     components whose fingerprint differs from the start: results
              after that point mix two configurations, and the report says so
  signals     every ledger decision in the period per strategy and decision
              (accepted, refused, watched / skipped) with its first reason and
              the recorded outcome (executable return at the primary horizon)

Seven days is an initial observation window, not proof of an edge. Nothing
here trades, changes a mode or a setting other than its own record.
"""

from __future__ import annotations

import hashlib
import json
import random
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core import solana_performance
from yonixalpha_core.db.models import AuditLog, ModelVersion, PlatformSetting, RiskSettingsVersion, StrategyConfig

KEY = "forward_test"
DEPLOY_KEY = "deployment"
ENGINES = ("solana_fresh", "solana_migration", "solana_momentum")
MODES = ("PAPER", "LIVE")
BASELINE_DAYS = 14
DEPLOY_HISTORY = 50
RULES = {
    "min_closed_trades": 30,
    "min_days": 7,
    "min_distinct_tokens": 20,
    "max_best_trade_share": 0.5,  # more than half of a positive net from one trade: not evidence of an edge
    "confidence": 0.95,
    "baseline_days": BASELINE_DAYS,
    "unit": "net realized PnL per closed trade in SOL, after every recorded cost",
}
# Platform settings that change how trades are decided, sized or simulated.
SETTINGS_KEYS = ("global_mode", "live_execution", "paper_execution", "exit_protection_settings", "ml_contribution",
                 "entry_intelligence", "launch_coordination", "wallet_validation")
BOOT_SAMPLES = 2000
SEED = 20261024

PNL_SQL = """
SELECT coalesce(execution_mode, 'PAPER') AS mode, engine, realized_pnl AS pnl, asset_id
FROM paper_positions
WHERE engine = ANY(:engines) AND status = 'closed' AND exit_at IS NOT NULL AND exit_at >= :since AND exit_at < :until
ORDER BY exit_at LIMIT 20000
"""
SIGNALS_SQL = """
SELECT engine, stage, decision, traded,
       -- numbers become N so "liquidity 0.0031 below minimum 20" and "liquidity 0.0051 ..." are one group
       left(regexp_replace(coalesce(nullif(split_part(split_part(reasons->>0, ':', 1), ' (', 1), ''), '-'),
                           '[0-9]+([.][0-9]+)?', 'N', 'g'), 60) AS reason,
       count(*) AS n, count(*) FILTER (WHERE status = 'COMPLETE') AS complete,
       count(*) FILTER (WHERE executable_return_pct IS NOT NULL) AS measured,
       count(*) FILTER (WHERE executable_return_pct > 0) AS positive,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY executable_return_pct) AS median_exec_pct,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY peak_pct) AS median_peak_pct
FROM opportunity_outcomes
WHERE decided_at >= :since AND decided_at < :until AND engine = ANY(:engines)
GROUP BY 1, 2, 3, 4, 5 ORDER BY 1, 6 DESC LIMIT 300
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _h(v: Any) -> str:
    return hashlib.sha256(json.dumps(v, sort_keys=True, default=str).encode()).hexdigest()[:12]


async def _get(session: AsyncSession, key: str) -> dict | None:
    row = await session.get(PlatformSetting, key)
    return dict(row.value) if row else None


async def _put(session: AsyncSession, key: str, value: dict) -> None:
    row = await session.get(PlatformSetting, key)
    if row is None:
        session.add(PlatformSetting(key=key, value=value))
    else:
        row.value = value


async def record_deployment(session: AsyncSession, revision: str, now: datetime | None = None) -> dict:
    """Called by scripts/deploy.sh after the stack is up: which revision runs
    since when. History kept (newest first, DEPLOY_HISTORY entries)."""
    now = now or _now()
    rev = "".join(c for c in str(revision) if c.isalnum())[:40] or "unknown"
    cur = await _get(session, DEPLOY_KEY) or {"history": []}
    entry = {"revision": rev, "at": now.isoformat()}
    value = {"current": entry, "history": ([entry] + list(cur.get("history") or []))[:DEPLOY_HISTORY]}
    await _put(session, DEPLOY_KEY, value)
    session.add(AuditLog(user_id=None, event_type="deployment.recorded", ip_address=None, detail=entry))
    return entry


async def configuration(session: AsyncSession) -> dict[str, Any]:
    """The readable configuration a period is judged under."""
    strategies = {r.strategy: {"mode": r.mode, "config_hash": _h(r.config)}
                  for r in (await session.execute(select(StrategyConfig))).scalars().all()}
    risk = {scope: int(v) for scope, v in (await session.execute(
        select(RiskSettingsVersion.scope, func.max(RiskSettingsVersion.version)).group_by(RiskSettingsVersion.scope))).all()}
    settings = {}
    for k in SETTINGS_KEYS:
        row = await session.get(PlatformSetting, k)
        settings[k] = _h(row.value) if row else None
    gm = await session.get(PlatformSetting, "global_mode")
    models = {f"{n}": f"v{v} {st}" for n, v, st in (await session.execute(
        select(ModelVersion.name, ModelVersion.version, ModelVersion.status)
        .where(ModelVersion.status.in_(("active", "challenger"))).order_by(ModelVersion.name, ModelVersion.version))).all()}
    return {"global_mode": (gm.value or {}).get("mode") if gm else "PAPER", "strategies": strategies,
            "risk_settings_versions": risk, "settings": settings, "models_active_or_challenger": models}


def _models(conf: dict[str, Any], status: str) -> dict[str, str]:
    return {k: v for k, v in (conf.get("models_active_or_challenger") or {}).items() if str(v).endswith(" " + status)}


def fingerprint(conf: dict[str, Any]) -> dict[str, str]:
    """What decides trades. Only ACTIVE models are loaded for decisions (ml.registry.get_active_model), so a new
    challenger from routine retraining is not a configuration change (2026-10-25: it was flagged as one)."""
    out = {"global_mode": _h(conf.get("global_mode")), "risk_settings": _h(conf.get("risk_settings_versions")),
           "models": _h(_models(conf, "active"))}
    for name, s in (conf.get("strategies") or {}).items():
        out[f"strategy:{name}"] = _h(s)
    for k, v in (conf.get("settings") or {}).items():
        out[f"setting:{k}"] = v or "-"
    return out


async def current(session: AsyncSession) -> dict | None:
    return ((await _get(session, KEY)) or {}).get("current")


async def start(session: AsyncSession, redis: Any, *, label: str, user: str | None, user_id: Any = None,
                now: datetime | None = None) -> dict[str, Any]:
    """Opens a new period; the previous one (if any) is ended now and kept."""
    from yonixalpha_core import scanner_health

    now = now or _now()
    label = " ".join(str(label or "").split())[:120] or "forward test"
    conf = await configuration(session)
    deploy = ((await _get(session, DEPLOY_KEY)) or {}).get("current")
    sources = None
    if redis is not None:
        try:
            h = await scanner_health.report(session, redis, now)
            sources = {"overall": h.get("overall"),
                       "stages": {s["stage"]: {"state": s["state"], "age_seconds": s["age_seconds"]} for s in h["stages"]}}
        except Exception as exc:  # noqa: BLE001 - a health read failure is recorded, it never blocks the start
            sources = {"overall": "UNKNOWN", "error": f"{type(exc).__name__}: {exc}"[:200]}
    period = {"id": now.strftime("%Y%m%dT%H%M%SZ"), "label": label, "started_at": now.isoformat(), "started_by": user,
              "deployment": deploy, "configuration": conf, "fingerprint": fingerprint(conf), "data_sources": sources,
              "rules": dict(RULES)}
    cur = await _get(session, KEY) or {"history": []}
    history = list(cur.get("history") or [])
    if cur.get("current"):
        history.insert(0, {**cur["current"], "ended_at": now.isoformat()})
    await _put(session, KEY, {"current": period, "history": history[:50]})
    session.add(AuditLog(user_id=user_id, event_type="forward_test.started", ip_address=None,
                         detail={"id": period["id"], "label": label, "by": user,
                                 "deployment": deploy, "previous": (cur.get("current") or {}).get("id")}))
    return period


def bootstrap_mean_ci(values: list[float], confidence: float = 0.95, seed: int = SEED) -> tuple[float, float] | None:
    if len(values) < 2:
        return None
    rnd = random.Random(seed)
    n = len(values)
    means = sorted(statistics.fmean(rnd.choices(values, k=n)) for _ in range(BOOT_SAMPLES))
    lo = means[int((1 - confidence) / 2 * BOOT_SAMPLES)]
    hi = means[min(BOOT_SAMPLES - 1, int((1 + confidence) / 2 * BOOT_SAMPLES))]
    return lo, hi


def bootstrap_diff_ci(a: list[float], b: list[float], confidence: float = 0.95,
                      seed: int = SEED) -> tuple[float, float] | None:
    """Interval of mean(a) - mean(b), resampling each group independently."""
    if len(a) < 2 or len(b) < 2:
        return None
    rnd = random.Random(seed)
    diffs = sorted(statistics.fmean(rnd.choices(a, k=len(a))) - statistics.fmean(rnd.choices(b, k=len(b)))
                   for _ in range(BOOT_SAMPLES))
    return (diffs[int((1 - confidence) / 2 * BOOT_SAMPLES)],
            diffs[min(BOOT_SAMPLES - 1, int((1 + confidence) / 2 * BOOT_SAMPLES))])


def judge(fwd: list[float], base: list[float], *, tokens: int, elapsed_days: float, rules: dict) -> dict[str, Any]:
    """Evidence, uncertainty and the verdict for one strategy and mode."""
    reasons = []
    if len(fwd) < rules["min_closed_trades"]:
        reasons.append(f"{len(fwd)} closed trades (need {rules['min_closed_trades']})")
    if elapsed_days < rules["min_days"]:
        reasons.append(f"{elapsed_days:.1f} days elapsed (need {rules['min_days']})")
    if tokens < rules["min_distinct_tokens"]:
        reasons.append(f"{tokens} distinct tokens (need {rules['min_distinct_tokens']})")
    net = sum(fwd)
    best = max(fwd) if fwd else None
    best_share = (best / net) if fwd and net > 0 and best and best > 0 else None
    if best_share is not None and best_share > rules["max_best_trade_share"]:
        reasons.append(f"the best trade is {best_share * 100:.0f} % of the net result "
                       f"(more than {rules['max_best_trade_share'] * 100:.0f} %): one trade carries it")
    ci = bootstrap_mean_ci(fwd, rules["confidence"])
    diff = bootstrap_diff_ci(fwd, base, rules["confidence"])
    out: dict[str, Any] = {
        "closed_trades": len(fwd), "distinct_tokens": tokens, "net_sol": round(net, 6),
        "expectancy_sol": round(statistics.fmean(fwd), 6) if fwd else None,
        "expectancy_ci_sol": [round(ci[0], 6), round(ci[1], 6)] if ci else None,
        "best_trade_share_of_net": round(best_share, 3) if best_share is not None else None,
        "baseline_trades": len(base), "baseline_expectancy_sol": round(statistics.fmean(base), 6) if base else None,
        "difference_ci_sol": [round(diff[0], 6), round(diff[1], 6)] if diff else None,
        "evidence_gaps": reasons,
    }
    if reasons:
        out["verdict"] = "INSUFFICIENT EVIDENCE"
    elif not base:
        out["verdict"] = "NO BASELINE"
    elif diff and diff[0] > 0:
        out["verdict"] = "IMPROVING"
    elif diff and diff[1] < 0:
        out["verdict"] = "DETERIORATING"
    else:
        out["verdict"] = "INCONCLUSIVE"
    out["edge"] = ("not established: the interval of the net per trade includes 0 or lies below it"
                   if not ci or ci[0] <= 0 else
                   "the interval of the net per trade is above 0 in this period; still not proof of a lasting edge")
    return out


async def _pnls(session: AsyncSession, since: datetime, until: datetime) -> dict[tuple[str, str], list[tuple[float, str]]]:
    out: dict[tuple[str, str], list[tuple[float, str]]] = {}
    for r in (await session.execute(text(PNL_SQL), {"engines": list(ENGINES), "since": since, "until": until})).all():
        out.setdefault((r.engine, r.mode), []).append((float(r.pnl or 0), r.asset_id or "?"))
    return out


async def report(session: AsyncSession, redis: Any = None, now: datetime | None = None,
                 period: dict | None = None) -> dict[str, Any]:
    now = now or _now()
    period = period or await current(session)
    if not period:
        return {"state": "NOT STARTED", "message": "no forward test has been started: run tools.forward_test start "
                                                     "after a verified deployment"}
    rules = {**RULES, **(period.get("rules") or {})}
    started = datetime.fromisoformat(period["started_at"])
    until = datetime.fromisoformat(period["ended_at"]) if period.get("ended_at") else now
    elapsed = max((until - started).total_seconds(), 0) / 86400
    base_since = started - timedelta(days=int(rules["baseline_days"]))
    fwd, base = await _pnls(session, started, until), await _pnls(session, base_since, started)
    conf = await configuration(session)
    fp_now = fingerprint(conf)
    # recomputed from the configuration stored at the start, so every period is compared by the same rule
    fp_start = fingerprint(period["configuration"]) if period.get("configuration") else (period.get("fingerprint") or {})
    changed = sorted(k for k in set(fp_now) | set(fp_start) if fp_now.get(k) != fp_start.get(k))
    challengers = {"at_start": _models(period.get("configuration") or {}, "challenger"),
                   "now": _models(conf, "challenger")}
    strategies: dict[str, Any] = {}
    roll_since = max(started, until - timedelta(days=7))
    for engine in ENGINES:
        perf = await solana_performance.report(session, since=started, until=until, engine=engine, outcomes=False)
        rolling = await solana_performance.report(session, since=roll_since, until=until, engine=engine, outcomes=False)
        baseline = await solana_performance.report(session, since=base_since, until=started, engine=engine, outcomes=False)
        per_mode = {}
        for mode in MODES:
            f = [p for p, _ in fwd.get((engine, mode), [])]
            b = [p for p, _ in base.get((engine, mode), [])]
            tokens = len({t for _, t in fwd.get((engine, mode), [])})
            m = (perf["trades_by_mode"].get(mode) or {})
            if not f and not b and not m:
                continue
            per_mode[mode] = {
                "judgement": judge(f, b, tokens=tokens, elapsed_days=elapsed, rules=rules),
                "forward": m.get("overall"), "daily": m.get("by_day") or {},
                "rolling_7d": (rolling["trades_by_mode"].get(mode) or {}).get("overall"),
                "baseline": (baseline["trades_by_mode"].get(mode) or {}).get("overall"),
                "by_exit_reason": m.get("by_exit_reason") or {}, "by_stage": m.get("by_stage") or {},
                "by_age": m.get("by_age") or {}, "by_regime": m.get("by_regime") or {},
                "by_entry_quality": m.get("by_entry_quality") or {},
                "coverage": perf["coverage"].get(mode),
            }
        strategies[engine] = {"modes": per_mode, "open_positions": perf["open_positions"]}
    sigs = await signals(session, started, until)
    return {
        "state": "ENDED" if period.get("ended_at") else "RUNNING",
        "period": {k: period.get(k) for k in ("id", "label", "started_at", "ended_at", "started_by", "deployment",
                                                "data_sources", "configuration")},
        "rules": rules, "elapsed_days": round(elapsed, 2), "baseline_window": [base_since.isoformat(), started.isoformat()],
        "configuration_changed": changed,
        "challenger_models": {**challengers, "note": "challengers are never used for decisions; listed, not a change"},
        "configuration_note": ("unchanged since the start" if not changed else
                               "CHANGED since the start: " + ", ".join(changed)
                               + ". Results after the change mix two configurations; start a new period to judge "
                                 "the new one on its own."),
        "strategies": strategies, "signals": sigs,
        "notes": ["Closed trades only: open positions are listed apart and never booked.",
                  "PAPER and LIVE are judged separately; PAPER is not evidence of LIVE results.",
                  "Seven days is an initial observation window, not proof of an edge. A verdict needs enough "
                  "independent trades; if the evidence is weak, extend the test.",
                  "Signals: every ledger decision in the period (accepted, refused, watched), with the outcome the "
                  "ledger measured after it; decisions dropped before the ledger (no curve address, token too young) "
                  "are only in the funnel counters (tools.momentum_pipeline)."],
    }


async def signals(session: AsyncSession, since: datetime, until: datetime,
                  engines: tuple[str, ...] = ENGINES) -> list[dict[str, Any]]:
    """Ledger decisions in [since, until) per strategy, stage, decision and
    first reason, with what the ledger measured after them."""
    return [{"engine": r.engine, "stage": r.stage, "decision": r.decision, "traded": r.traded, "reason": r.reason,
             "count": int(r.n), "outcome_complete": int(r.complete), "measured": int(r.measured),
             "positive": int(r.positive),
             "median_executable_return_pct": round(float(r.median_exec_pct), 2) if r.median_exec_pct is not None else None,
             "median_peak_pct": round(float(r.median_peak_pct), 2) if r.median_peak_pct is not None else None}
            for r in (await session.execute(text(SIGNALS_SQL), {"engines": list(engines), "since": since,
                                                                 "until": until})).all()]


async def history(session: AsyncSession) -> list[dict]:
    cur = await _get(session, KEY) or {}
    out = ([{**cur["current"], "state": "RUNNING"}] if cur.get("current") else []) + \
        [{**h, "state": "ENDED"} for h in cur.get("history") or []]
    return [{k: h.get(k) for k in ("id", "label", "started_at", "ended_at", "started_by", "deployment", "state")}
            for h in out]
