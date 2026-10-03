"""EVM opportunities as ML samples (master §36-44, §71-75; M12a).

Every BSC / Robinhood observation (chains.evm.observation: one per token and
category: FRESH, MIGRATED, MOMENTUM) becomes one sample, traded or not:

  decision point D   the observation's T+5 snapshot (DECISION_MINUTE): the
                     same moment for every token, entered or not, so traded,
                     waited and rejected opportunities are comparable;
  features           only the snapshots taken at or before D (T0, T+5). The
                     token's live state (liquidity, curve progress) is a
                     feature only when it was read within LIVE_STATE_TOLERANCE
                     of the snapshot moment; read later it could carry the
                     future, so it is left missing (with its __missing flag);
  labels             from the token's stored trades in (D, D + 60 min]:
                     upside_50 / upside_100 (the price reached +50 % / +100 %),
                     fast_dump (-50 % within 10 min), return_60m,
                     max_drawdown_pct, migrate_60m; executable_return_pct
                     only for a traded opportunity (its closed paper
                     position, fees and taxes included);
  verdicts (§41)     deterministic (did the trade signal qualify), risk
                     (did safety allow it), final (what the system did), and
                     ML (from the shadow models, once trained) as BUY / WAIT /
                     REJECT, compared against the outcome.

Curves quoted in another token (Four.meme tokenized stocks) are left out:
their volumes are not in BNB / ETH and would not be comparable. Nothing here
is read by the entry code: samples feed SHADOW models only (review data).
"""

from __future__ import annotations

import statistics
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

FEATURE_VERSION = "evmfeat-2026.10.1"
LABEL_VERSION = "evmlabel-2026.10.1"
DECISION_MINUTE = 5
HORIZON = timedelta(minutes=60)
FAST_DUMP_WINDOW = timedelta(minutes=10)
LIVE_STATE_TOLERANCE = timedelta(seconds=90)
CHAINS = ("bsc", "robinhood")
CATEGORIES = ("FRESH", "MIGRATED", "MOMENTUM")
LAUNCHPADS = ("fourmeme", "flap", "pons_v2", "pons_v1", "genius_fun")
BUY, WAIT, REJECT, ALLOW = "BUY", "WAIT", "REJECT", "ALLOW"
# The ML verdict from the shadow scores (shadow comparison only).
ML_BUY_UPSIDE = 0.5
ML_REJECT_DUMP = 0.5

NUMERIC = ("trades", "buyers", "sellers", "buy_volume", "sell_volume", "volume_total", "holders", "holder_growth",
           "effective_buyers", "top_buyer_share", "net_flow", "organic_net_flow", "smart_money_buyers",
           "creator_trades", "buy_sell_volume_ratio", "price_change_t0", "market_cap", "liquidity", "curve_progress",
           "t0_trades", "t0_buyers", "t0_buy_volume")
LIVE_STATE = ("liquidity", "curve_progress")
BINARY = tuple(f"chain_{c}" for c in CHAINS) + tuple(f"cat_{c}" for c in CATEGORIES) + \
    tuple(f"lp_{lp}" for lp in LAUNCHPADS)
FEATURE_NAMES: tuple[str, ...] = NUMERIC + tuple(f"{n}__missing" for n in NUMERIC) + BINARY


def _f(v: Any) -> float | None:
    try:
        return float(Decimal(str(v))) if v is not None and v != "" else None
    except (InvalidOperation, ValueError):
        return None


def decision_snapshot(snapshots: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """(snapshot at D, the T0 snapshot): only snapshots with minutes <= D."""
    by_min = {s.get("minutes"): s for s in (snapshots or {}).values() if isinstance(s, dict)}
    return by_min.get(DECISION_MINUTE), by_min.get(0)


def features(snap: dict[str, Any], t0: dict[str, Any] | None, chain: str, category: str, launchpad: str) -> dict[str, Any]:
    """Decision-time features; None = unknown (never 0), with __missing flags."""
    x: dict[str, Any] = {n: _f(snap.get(n)) for n in ("trades", "buyers", "sellers", "buy_volume", "sell_volume",
                                                      "volume_total", "holders", "holder_growth", "effective_buyers",
                                                      "top_buyer_share", "net_flow", "organic_net_flow",
                                                      "smart_money_buyers", "creator_trades", "market_cap")}
    bv, sv = x["buy_volume"], x["sell_volume"]
    x["buy_sell_volume_ratio"] = bv / sv if bv is not None and sv else None
    p, p0 = _f(snap.get("price")), _f((t0 or {}).get("price"))
    x["price_change_t0"] = (p / p0 - 1) if p is not None and p0 else None
    for n, key in (("t0_trades", "trades"), ("t0_buyers", "buyers"), ("t0_buy_volume", "buy_volume")):
        x[n] = _f((t0 or {}).get(key))
    live_ok = False
    try:
        read = datetime.fromisoformat(snap.get("state_read_at") or snap["taken_at"])
        live_ok = read - datetime.fromisoformat(snap["at"]) <= LIVE_STATE_TOLERANCE
    except (KeyError, TypeError, ValueError):
        pass
    x["liquidity"] = _f(snap.get("liquidity")) if live_ok else None
    x["curve_progress"] = _f(snap.get("curve_progress")) if live_ok else None
    for n in NUMERIC:
        x[f"{n}__missing"] = 1.0 if x.get(n) is None else 0.0
    for c in CHAINS:
        x[f"chain_{c}"] = 1.0 if chain == c else 0.0
    for c in CATEGORIES:
        x[f"cat_{c}"] = 1.0 if category == c else 0.0
    for lp in LAUNCHPADS:
        x[f"lp_{lp}"] = 1.0 if launchpad == lp else 0.0
    return x


def labels(p0: float | None, path: list[tuple[datetime, float]], decided_at: datetime,
           migrated_at: datetime | None) -> dict[str, Any]:
    """Outcome after D from the price path (time, price) in (D, D + 60 min]."""
    if not p0:
        return {"unknown": "no trade price at the decision point"}
    pts = [(t, p) for t, p in path if decided_at < t <= decided_at + HORIZON and p > 0]
    if not pts:
        return {"upside_50": False, "upside_100": False, "fast_dump": False, "return_60m_pct": 0.0,
                "max_drawdown_pct": 0.0, "max_return_pct": 0.0, "trades_after": 0,
                "migrate_60m": bool(migrated_at and decided_at < migrated_at <= decided_at + HORIZON),
                "note": "no trade in the hour after the decision: price unchanged"}
    hi, lo = max(p for _, p in pts), min(p for _, p in pts)
    early_lo = min((p for t, p in pts if t <= decided_at + FAST_DUMP_WINDOW), default=None)
    return {"upside_50": hi >= 1.5 * p0, "upside_100": hi >= 2.0 * p0,
            "fast_dump": early_lo is not None and early_lo <= 0.5 * p0,
            "return_60m_pct": round((pts[-1][1] / p0 - 1) * 100, 4),
            "max_return_pct": round((hi / p0 - 1) * 100, 4), "max_drawdown_pct": round((lo / p0 - 1) * 100, 4),
            "migrate_60m": bool(migrated_at and decided_at < migrated_at <= decided_at + HORIZON),
            "trades_after": len(pts)}


def verdicts(state: str, history: list[dict[str, Any]], ml: str | None) -> dict[str, str]:
    """§41: the deterministic trade signal, the risk layer, the final action
    and the ML recommendation (shadow), each as BUY / WAIT / REJECT
    (risk: ALLOW / REJECT)."""
    states = {h.get("state") for h in history or []} | {state}
    qualified = bool(states & {"QUALIFIED", "WAITING_FOR_ENTRY", "ENTRY_PENDING", "ENTERED"})
    unsafe = bool(states & {"SAFETY_FAILURE", "REJECTED"})
    final = BUY if state == "ENTERED" else REJECT if state == "REJECTED" else WAIT
    return {"deterministic": BUY if qualified else WAIT, "risk": REJECT if unsafe else ALLOW, "final": final,
            "ml": ml or "NOT_AVAILABLE"}


def ml_verdict(scores: dict[str, Any]) -> str | None:
    up = (scores.get("P_UPSIDE_50") or {}).get("value")
    dump = (scores.get("P_FAST_DUMP") or {}).get("value")
    if up is None and dump is None:
        return None
    if dump is not None and dump >= ML_REJECT_DUMP:
        return REJECT
    if up is not None and up >= ML_BUY_UPSIDE:
        return BUY
    return WAIT


def compare(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Outcome of each recommender's BUY / WAIT / REJECT groups (§41), plus
    missed winners and bad entries of the final action. rows: {"verdicts",
    "labels", "executable_return_pct"}."""
    def stats(group: list[dict[str, Any]]) -> dict[str, Any]:
        lab = [g["labels"] for g in group if g.get("labels") and "unknown" not in g["labels"]]
        if not lab:
            return {"n": len(group), "labelled": 0}
        rets = sorted(x["return_60m_pct"] for x in lab)
        ex = [g["executable_return_pct"] for g in group if g.get("executable_return_pct") is not None]
        return {"n": len(group), "labelled": len(lab),
                "upside_50_rate": round(sum(x["upside_50"] for x in lab) / len(lab), 4),
                "upside_100_rate": round(sum(x["upside_100"] for x in lab) / len(lab), 4),
                "fast_dump_rate": round(sum(x["fast_dump"] for x in lab) / len(lab), 4),
                "mean_return_60m_pct": round(sum(rets) / len(rets), 4), "median_return_60m_pct": round(statistics.median(rets), 4),
                "executable": {"n": len(ex), "mean_pct": round(sum(ex) / len(ex), 4)} if ex else None}

    out: dict[str, Any] = {}
    for who in ("deterministic", "risk", "final", "ml"):
        groups: dict[str, list] = {}
        for r in rows:
            groups.setdefault(r["verdicts"].get(who) or "NOT_AVAILABLE", []).append(r)
        out[who] = {k: stats(v) for k, v in sorted(groups.items())}
    lab = [r for r in rows if r.get("labels") and "unknown" not in r["labels"]]
    out["final_missed_winners"] = sum(1 for r in lab if r["verdicts"]["final"] != BUY and r["labels"]["upside_100"])
    out["final_bad_entries"] = sum(1 for r in lab if r["verdicts"]["final"] == BUY and r["labels"]["fast_dump"])
    scored = [r for r in lab if (r["verdicts"].get("ml") or "NOT_AVAILABLE") != "NOT_AVAILABLE"]
    agree: dict[str, int] = {}
    for r in scored:
        k = f"final {r['verdicts']['final']} / ml {r['verdicts']['ml']}"
        agree[k] = agree.get(k, 0) + 1
    out["final_vs_ml"] = agree
    out["note"] = NOTE
    return out


NOTE = ("BUY / WAIT / REJECT at the decision point (T+5); outcome = the hour after it. SELL / HOLD (exits) "
        "are not compared yet. ML is a shadow recommendation: it never changes a decision.")
_LAB = "(labels <> '{}'::jsonb AND labels -> 'unknown' IS NULL)"


async def knowledge(session, since: datetime) -> dict[str, Any]:
    """The counts and the compare() result computed in the database (one
    row per observation adds up to ~20k rows a day on BSC: never loaded into
    memory). Same numbers as compare() on the same rows."""
    from sqlalchemy import text

    base = f"FROM (SELECT *, {_LAB} AS lab FROM evm_ml_samples WHERE decided_at >= :since) s"
    p = {"since": since}
    c = (await session.execute(text(
        "SELECT count(*), count(*) FILTER (WHERE lab), count(*) FILTER (WHERE traded), "
        "count(*) FILTER (WHERE observation_state = 'REJECTED'), count(*) FILTER (WHERE observation_state = 'EXPIRED'), "
        "count(*) FILTER (WHERE traded AND executable_return_pct > 0), "
        "count(*) FILTER (WHERE traded AND executable_return_pct < 0), "
        "count(*) FILTER (WHERE jsonb_typeof(ml_shadow) = 'object'), "
        "count(*) FILTER (WHERE (ml_shadow ->> 'out_of_sample')::boolean) " + base), p)).one()
    by_cat = {k: v for k, v in (await session.execute(text(f"SELECT category, count(*) {base} GROUP BY 1"), p)).all()}
    cmp: dict[str, Any] = {}
    for who in ("deterministic", "risk", "final", "ml"):
        q = (f"SELECT coalesce(verdicts ->> '{who}', 'NOT_AVAILABLE'), count(*), count(*) FILTER (WHERE lab), "
             "avg(CASE WHEN (labels ->> 'upside_50')::boolean THEN 1.0 ELSE 0.0 END) FILTER (WHERE lab), "
             "avg(CASE WHEN (labels ->> 'upside_100')::boolean THEN 1.0 ELSE 0.0 END) FILTER (WHERE lab), "
             "avg(CASE WHEN (labels ->> 'fast_dump')::boolean THEN 1.0 ELSE 0.0 END) FILTER (WHERE lab), "
             "avg((labels ->> 'return_60m_pct')::float8) FILTER (WHERE lab), "
             "percentile_cont(0.5) WITHIN GROUP (ORDER BY (labels ->> 'return_60m_pct')::float8) FILTER (WHERE lab), "
             f"count(executable_return_pct), avg(executable_return_pct) {base} GROUP BY 1 ORDER BY 1")
        groups: dict[str, Any] = {}
        for v, n, nl, u50, u100, fd, mean, med, ex_n, ex_mean in (await session.execute(text(q), p)).all():
            if not nl:
                groups[v] = {"n": n, "labelled": 0}
                continue
            groups[v] = {"n": n, "labelled": nl, "upside_50_rate": round(float(u50), 4),
                         "upside_100_rate": round(float(u100), 4), "fast_dump_rate": round(float(fd), 4),
                         "mean_return_60m_pct": round(float(mean), 4), "median_return_60m_pct": round(float(med), 4),
                         "executable": {"n": ex_n, "mean_pct": round(float(ex_mean), 4)} if ex_n else None}
        cmp[who] = groups
    missed, bad = (await session.execute(text(
        "SELECT count(*) FILTER (WHERE lab AND coalesce(verdicts ->> 'final', '') <> 'BUY' "
        "AND (labels ->> 'upside_100')::boolean), "
        "count(*) FILTER (WHERE lab AND verdicts ->> 'final' = 'BUY' AND (labels ->> 'fast_dump')::boolean) " + base),
        p)).one()
    cmp["final_missed_winners"], cmp["final_bad_entries"] = missed, bad
    cmp["final_vs_ml"] = {f"final {f} / ml {m}": n for f, m, n in (await session.execute(text(
        "SELECT verdicts ->> 'final', verdicts ->> 'ml', count(*) " + base +
        " WHERE lab AND coalesce(verdicts ->> 'ml', 'NOT_AVAILABLE') <> 'NOT_AVAILABLE' GROUP BY 1, 2 ORDER BY 1, 2"),
        p)).all()}
    cmp["note"] = NOTE
    return {"samples": {"evm_total": c[0], "evm_labelled": c[1], "by_category": by_cat, "traded": c[2],
                        "rejected": c[3], "expired_no_entry": c[4], "missed_winners": missed, "wins": c[5],
                        "losses": c[6], "scored": c[7], "scored_out_of_sample": c[8]},
            "comparison": cmp}


# --- builder (ml service) ------------------------------------------------------------------------

async def build(session, now: datetime, limit: int = 500) -> dict[str, int]:
    """Materialises the samples whose outcome hour has passed and whose
    trades are still retained. Idempotent (one row per observation)."""
    from sqlalchemy import and_, select
    from sqlalchemy.dialects.postgresql import insert

    from yonixalpha_core.chains.evm.store import NATIVE_QUOTES
    from yonixalpha_core.db.models import EvmMlSample, EvmObservation, EvmToken, EvmTrade, PaperPosition

    retention_start = now - timedelta(days=14)
    ready = now - HORIZON - timedelta(minutes=DECISION_MINUTE)
    done = select(EvmMlSample.chain).where(EvmMlSample.chain == EvmObservation.chain,
                                           EvmMlSample.token == EvmObservation.token,
                                           EvmMlSample.category == EvmObservation.category).exists()
    obs_rows = (await session.execute(select(EvmObservation, EvmToken).join(EvmToken, and_(
        EvmToken.chain == EvmObservation.chain, EvmToken.token == EvmObservation.token)).where(
        EvmObservation.started_at <= ready, EvmObservation.started_at >= retention_start, ~done)
        .order_by(EvmObservation.started_at).limit(limit))).all()
    out = {"built": 0, "skipped_other_quote": 0, "skipped_no_snapshot": 0}
    for obs, tok in obs_rows:
        if tok.quote_token and tok.quote_token.lower() not in NATIVE_QUOTES:
            out["skipped_other_quote"] += 1
            sample = {"labels": {"unknown": "curve quoted in another token: not comparable"}, "features": {}}
        else:
            snap, t0 = decision_snapshot(obs.snapshots)
            if snap is None:
                out["skipped_no_snapshot"] += 1
                sample = {"labels": {"unknown": f"no T+{DECISION_MINUTE} snapshot was taken"}, "features": {}}
            else:
                d = obs.started_at + timedelta(minutes=DECISION_MINUTE)
                trades = (await session.execute(select(EvmTrade.at, EvmTrade.quote_amount, EvmTrade.token_amount).where(
                    EvmTrade.chain == obs.chain, EvmTrade.token == obs.token, EvmTrade.at <= d + HORIZON,
                    EvmTrade.token_amount > 0).order_by(EvmTrade.at))).all()
                path = [(t, float(Decimal(q) / Decimal(a))) for t, q, a in trades if a]
                before = [p for t, p in path if t <= d]
                sample = {"features": features(snap, t0, obs.chain, obs.category, tok.launchpad),
                          "labels": labels(before[-1] if before else None, path, d, tok.migrated_at)}
        d = obs.started_at + timedelta(minutes=DECISION_MINUTE)
        pos = (await session.execute(select(PaperPosition).where(
            PaperPosition.engine == f"evm_{obs.chain}", PaperPosition.asset_id == obs.token,
            PaperPosition.entry_at >= obs.started_at, PaperPosition.entry_at <= obs.deadline)
            .order_by(PaperPosition.entry_at).limit(1))).scalar_one_or_none()
        exec_ret = float(pos.realized_pnl_pct * 100) if pos is not None and pos.realized_pnl_pct is not None else None
        await session.execute(insert(EvmMlSample).values(
            chain=obs.chain, token=obs.token, category=obs.category, launchpad=tok.launchpad, decided_at=d,
            features=sample["features"], labels=sample["labels"], feature_version=FEATURE_VERSION,
            label_version=LABEL_VERSION, verdicts=verdicts(obs.state, obs.history, None),
            traded=pos is not None, position_id=pos.id if pos is not None else None,
            executable_return_pct=exec_ret, observation_state=obs.state, created_at=now)
            .on_conflict_do_nothing(index_elements=["chain", "token", "category"]))
        out["built"] += 1
    return out


async def refresh_executable(session, limit: int = 500) -> int:
    """Fills the executable return of traded samples whose paper position
    closed after the sample was built."""
    from sqlalchemy import select

    from yonixalpha_core.db.models import EvmMlSample, PaperPosition

    rows = (await session.execute(select(EvmMlSample, PaperPosition).join(
        PaperPosition, PaperPosition.id == EvmMlSample.position_id).where(
        EvmMlSample.executable_return_pct.is_(None), PaperPosition.realized_pnl_pct.is_not(None)).limit(limit))).all()
    for s, p in rows:
        s.executable_return_pct = float(p.realized_pnl_pct * 100)
    return len(rows)
