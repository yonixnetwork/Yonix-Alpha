"""Execution funnel: where every observed Pump.fun token stopped, counted
from the database the engines write, never estimated.

Stages, for a time window:

  OBSERVED     token_observations (fresh-token observation outcomes)
  CANDIDATE    trading_candidates created (handed to the safety gate)
  ASSESSED     risk_assessments written (one per gate evaluation)
  SIGNAL       assessments whose strategy signal qualified (a BUY signal)
  EXECUTABLE   assessments the gate decided EXECUTE / REDUCE_SIZE
  POSITION     paper/live positions opened
  ORDER        live BUY/SELL orders, by status (SIGNED / SUBMITTED /
               CONFIRMED / FAILED / EXPIRED / CANCELLED)

plus the exact finding codes that blocked tokens with a BUY signal, the
HIGH-level findings behind "needs approval" decisions (which AUTO turns
into NO_TRADE), execution failures from the trade timeline, live-order
latencies, and the modes and locks in force. `token_trace` gives the same
path for one mint, evaluation by evaluation.

Used by the api (/api/control/execution-funnel) and by
`python -m yonixalpha_core.tools.execution_funnel` on the server.
"""

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core.safety import store

SOLANA_ENGINES = ("solana_fresh", "solana_momentum", "solana_migration")
NOT_A_SIGNAL = ("SIGNAL_NOT_QUALIFIED", "NO_SIGNAL")
# Decisions that end in the approval rule rather than in a finding of their own.
APPROVAL_META = ("RISK_ABOVE_AUTO_CEILING", "AUTO_NO_APPROVAL", "MANUAL_MODE")
EXECUTION_FAILURE_EVENTS = ("paper_entry_failed", "live_entry_refused", "live_entry_failed", "live_exit_failed",
                            "paper_exit_failed", "execution_blocked")

_SIGNAL_OK = ("NOT EXISTS (SELECT 1 FROM jsonb_array_elements(ra.assessment->'findings') s "
              "WHERE s->>'code' IN ('SIGNAL_NOT_QUALIFIED', 'NO_SIGNAL'))")


async def _rows(session: AsyncSession, sql: str, **params) -> list[dict]:
    return [dict(r._mapping) for r in (await session.execute(text(sql), params)).all()]


def _num(v):
    return int(v) if v is not None else 0


async def funnel(session: AsyncSession, since: datetime, redis=None, app_settings=None,
                 engines: tuple[str, ...] = SOLANA_ENGINES) -> dict[str, Any]:
    p = {"since": since, "engines": list(engines)}
    out: dict[str, Any] = {"since": since.isoformat(), "engines": list(engines)}

    out["observed"] = {r["outcome"]: _num(r["n"]) for r in await _rows(session, """
        SELECT outcome, count(*) n FROM token_observations WHERE decided_at >= :since GROUP BY outcome""", **p)}

    cand = await _rows(session, """
        SELECT engine, state, count(*) n FROM trading_candidates
        WHERE created_at >= :since AND detail->>'source' = 'pump_stream' GROUP BY engine, state""", **p)
    out["candidates"] = {}
    for r in cand:
        out["candidates"].setdefault(r["engine"], {})[r["state"]] = _num(r["n"])

    per_engine = await _rows(session, f"""
        SELECT ra.engine,
               count(*) assessments, count(DISTINCT ra.asset_id) tokens,
               count(DISTINCT ra.asset_id) FILTER (WHERE {_SIGNAL_OK}) signal_tokens,
               count(*) FILTER (WHERE {_SIGNAL_OK}) signal_assessments,
               count(DISTINCT ra.asset_id) FILTER (WHERE ra.executable) executable_tokens,
               count(*) FILTER (WHERE ra.executable) executable_assessments,
               count(*) FILTER (WHERE ra.executable AND ra.execution_target = 'LIVE') executable_live,
               count(*) FILTER (WHERE ra.executable AND ra.execution_target = 'PAPER') executable_paper
        FROM risk_assessments ra WHERE ra.evaluated_at >= :since AND ra.engine = ANY(:engines)
        GROUP BY ra.engine""", **p)
    out["stages"] = {r.pop("engine"): {k: _num(v) for k, v in r.items()} for r in per_engine}

    decisions = await _rows(session, """
        SELECT engine, decision, count(*) n, count(DISTINCT asset_id) tokens FROM risk_assessments
        WHERE evaluated_at >= :since AND engine = ANY(:engines) GROUP BY engine, decision""", **p)
    out["decisions"] = {}
    for r in decisions:
        out["decisions"].setdefault(r["engine"], {})[r["decision"]] = {"assessments": _num(r["n"]), "tokens": _num(r["tokens"])}

    # What blocked tokens that HAD a BUY signal: the findings whose action is the decision.
    out["blocked_with_buy_signal"] = await _rows(session, f"""
        SELECT ra.engine, f->>'code' code, ra.decision, count(*) assessments, count(DISTINCT ra.asset_id) tokens,
               min(f->>'message') example
        FROM risk_assessments ra, jsonb_array_elements(ra.assessment->'findings') f
        WHERE ra.evaluated_at >= :since AND ra.engine = ANY(:engines) AND NOT ra.executable AND {_SIGNAL_OK}
          AND f->>'action' = ra.decision
        GROUP BY ra.engine, f->>'code', ra.decision ORDER BY tokens DESC, assessments DESC LIMIT 40""", **p)
    # Behind "needs approval" (AUTO -> NO_TRADE): the HIGH findings that raised overall risk.
    out["approval_drivers"] = await _rows(session, f"""
        SELECT ra.engine, f->>'code' code, f->>'action' action, count(*) assessments, count(DISTINCT ra.asset_id) tokens,
               min(f->>'message') example
        FROM risk_assessments ra, jsonb_array_elements(ra.assessment->'findings') f
        WHERE ra.evaluated_at >= :since AND ra.engine = ANY(:engines) AND NOT ra.executable AND {_SIGNAL_OK}
          AND EXISTS (SELECT 1 FROM jsonb_array_elements(ra.assessment->'findings') m
                      WHERE m->>'code' IN ('RISK_ABOVE_AUTO_CEILING', 'AUTO_NO_APPROVAL'))
          AND f->>'level' IN ('HIGH', 'CRITICAL') AND f->>'code' <> ALL(:meta)
        GROUP BY ra.engine, f->>'code', f->>'action' ORDER BY tokens DESC LIMIT 25""", meta=list(APPROVAL_META), **p)
    # Every blocking code, BUY signal or not.
    out["blocked_all"] = await _rows(session, """
        SELECT ra.engine, f->>'code' code, ra.decision, count(*) assessments, count(DISTINCT ra.asset_id) tokens
        FROM risk_assessments ra, jsonb_array_elements(ra.assessment->'findings') f
        WHERE ra.evaluated_at >= :since AND ra.engine = ANY(:engines) AND NOT ra.executable AND f->>'action' = ra.decision
        GROUP BY ra.engine, f->>'code', ra.decision ORDER BY tokens DESC LIMIT 40""", **p)
    # Near misses: a BUY signal blocked by exactly one finding code.
    out["near_misses"] = await _rows(session, f"""
        WITH b AS (
          SELECT ra.id, ra.engine, ra.asset_id,
                 array_agg(DISTINCT f->>'code') FILTER (WHERE f->>'code' <> ALL(:meta)) codes
          FROM risk_assessments ra, jsonb_array_elements(ra.assessment->'findings') f
          WHERE ra.evaluated_at >= :since AND ra.engine = ANY(:engines) AND NOT ra.executable AND {_SIGNAL_OK}
            AND (f->>'action' = ra.decision OR (f->>'level' IN ('HIGH', 'CRITICAL')
                 AND ra.decision IN ('REQUIRE_MANUAL_APPROVAL', 'NO_TRADE')))
          GROUP BY ra.id, ra.engine, ra.asset_id)
        SELECT engine, codes[1] code, count(*) assessments, count(DISTINCT asset_id) tokens
        FROM b WHERE cardinality(codes) = 1 GROUP BY engine, codes[1] ORDER BY tokens DESC LIMIT 20""",
                                     meta=list(APPROVAL_META), **p)

    # The evidence behind *_UNAVAILABLE / *_STALE: the assembler's recorded errors, addresses masked.
    out["data_errors"] = await _rows(session, """
        SELECT ra.engine, left(regexp_replace(e, '[1-9A-HJ-NP-Za-km-z]{32,44}', '<addr>', 'g'), 160) error,
               count(DISTINCT ra.asset_id) tokens, count(*) assessments
        FROM risk_assessments ra,
             jsonb_array_elements_text(coalesce(ra.assessment->'inputs_snapshot'->'errors', '[]'::jsonb)) e
        WHERE ra.evaluated_at >= :since AND ra.engine = ANY(:engines) AND NOT ra.executable
        GROUP BY ra.engine, 2 ORDER BY tokens DESC LIMIT 15""", **p)

    positions = await _rows(session, """
        SELECT engine, execution_mode, status, count(*) n FROM paper_positions
        WHERE created_at >= :since AND engine = ANY(:engines) GROUP BY engine, execution_mode, status""", **p)
    out["positions"] = {}
    for r in positions:
        out["positions"].setdefault(r["engine"], {}).setdefault(r["execution_mode"], {})[r["status"]] = _num(r["n"])

    out["orders"] = {}
    for r in await _rows(session, """
        SELECT side, status, count(*) n FROM execution_orders
        WHERE created_at >= :since AND mode = 'LIVE' AND provider = 'pumpportal_local' GROUP BY side, status""", **p):
        out["orders"].setdefault(r["side"], {})[r["status"]] = _num(r["n"])
    out["order_errors"] = await _rows(session, """
        SELECT side, left(error, 160) error, count(*) n FROM execution_orders
        WHERE created_at >= :since AND mode = 'LIVE' AND provider = 'pumpportal_local' AND error IS NOT NULL
        GROUP BY side, left(error, 160) ORDER BY n DESC LIMIT 10""", **p)
    lat = await _rows(session, """
        SELECT side,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM submitted_at - created_at)) sign_submit_s,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM confirmed_at - submitted_at)) confirm_s,
               count(*) n
        FROM execution_orders WHERE created_at >= :since AND mode = 'LIVE' AND submitted_at IS NOT NULL GROUP BY side""", **p)
    out["order_latency_median_seconds"] = {r["side"]: {"order_to_signed": r["sign_submit_s"], "signed_to_confirmed": r["confirm_s"],
                                                       "orders": _num(r["n"])} for r in lat}
    out["execution_failures"] = await _rows(session, """
        SELECT event_type, left(coalesce(detail->>'reason', detail->>'error', ''), 160) reason, count(*) n
        FROM trade_timeline_events WHERE occurred_at >= :since AND event_type = ANY(:ev)
        GROUP BY event_type, left(coalesce(detail->>'reason', detail->>'error', ''), 160) ORDER BY n DESC LIMIT 15""",
                                            ev=list(EXECUTION_FAILURE_EVENTS), **p)

    modes = {"global": (await store.load_global_mode(session)).value,
             "strategies": {e: (await store.load_strategy_mode(session, e)).value for e in engines}}
    if app_settings is not None:
        modes["live_permitted"] = store.live_trading_permitted(app_settings)
    if redis is not None:
        from yonixalpha_core import live_trading

        for key, name in ((live_trading.READY_KEY, "live_worker"), (live_trading.WALLET_KEY, "wallet")):
            raw = await redis.get(key)
            modes[name] = json.loads(raw) if raw else None
        if app_settings is not None:
            ok, why = await live_trading.live_readiness(redis, app_settings)
            modes["live_ready"], modes["live_not_ready_reason"] = ok, why
    out["modes"] = modes
    out["diagnosis"] = diagnose(out)
    return out


def diagnose(f: dict[str, Any]) -> list[str]:
    """Plain statements derived only from the counts above."""
    notes: list[str] = []
    modes = f.get("modes") or {}
    strat = modes.get("strategies") or {}
    off = [e for e, m in strat.items() if m == "OFF"]
    if off:
        notes.append(f"Strategies OFF (never trade): {', '.join(off)}.")
    if modes.get("global") != "LIVE":
        notes.append(f"Global mode is {modes.get('global')}: no live buy is ever sent; executable decisions open PAPER positions.")
    elif not modes.get("live_ready", True):
        notes.append(f"Global mode LIVE but live execution not ready: {modes.get('live_not_ready_reason')} — every entry is NO_TRADE.")
    manual = [e for e, m in strat.items() if m == "MANUAL"]
    if manual:
        notes.append(f"Strategies in MANUAL ({', '.join(manual)}): every entry waits for your approval on the Decisions page.")
    paper_mode = [e for e, m in strat.items() if m == "PAPER"]
    if paper_mode:
        notes.append(f"Strategies in PAPER ({', '.join(paper_mode)}): a decision that needs approval waits for it "
                     "(it is NOT converted to NO_TRADE as in AUTO).")
    for engine, s in (f.get("stages") or {}).items():
        if s["assessments"] == 0:
            continue
        if s["signal_tokens"] == 0:
            notes.append(f"{engine}: {s['tokens']} tokens assessed, none produced a BUY signal — the strategy signal is the blocker.")
        elif s["executable_tokens"] == 0:
            top = [b for b in f.get("blocked_with_buy_signal", []) if b["engine"] == engine][:3]
            why = "; ".join(f"{b['code']} ({b['tokens']} tokens)" for b in top) or "see blocked_with_buy_signal"
            notes.append(f"{engine}: {s['signal_tokens']} tokens had a BUY signal, none was executable — blocked by: {why}.")
        else:
            opened = sum(sum(v.values()) for v in (f.get("positions", {}).get(engine) or {}).values())
            if opened == 0:
                notes.append(f"{engine}: {s['executable_tokens']} tokens were EXECUTABLE but no position was opened — "
                             "the decision→execution bridge failed; see execution_failures.")
    drivers = f.get("approval_drivers") or []
    if drivers:
        notes.append("Needs-approval decisions were driven by HIGH findings: "
                     + "; ".join(f"{d['code']} ({d['tokens']} tokens)" for d in drivers[:4]) + ".")
    buys = (f.get("orders") or {}).get("BUY") or {}
    if buys and not buys.get("CONFIRMED"):
        notes.append(f"Live BUY orders: {buys} — none confirmed; see order_errors.")
    return notes


async def token_trace(session: AsyncSession, mint: str, limit: int = 200) -> dict[str, Any]:
    """Everything recorded for one mint, in time order."""
    obs = await _rows(session, """SELECT outcome, trend, reasons, decided_at, report->'metrics' metrics
                                  FROM token_observations WHERE mint = :m""", m=mint)
    cands = await _rows(session, """SELECT c.id, c.engine, c.state, c.state_history, c.created_at FROM trading_candidates c
                                   JOIN tokens t ON t.id = c.token_id WHERE t.mint_address = :m ORDER BY c.created_at""", m=mint)
    assessments = await _rows(session, f"""
        SELECT ra.evaluated_at, ra.engine, ra.decision, ra.status_label, ra.executable, ra.execution_target,
               {_SIGNAL_OK} AS buy_signal,
               (SELECT jsonb_agg(jsonb_build_object('code', f->>'code', 'level', f->>'level', 'message', f->>'message'))
                FROM jsonb_array_elements(ra.assessment->'findings') f
                WHERE f->>'action' = ra.decision AND NOT ra.executable) blocking,
               ra.assessment->'plan'->'position_size'->>'value' size
        FROM risk_assessments ra WHERE ra.asset_id = :m ORDER BY ra.evaluated_at LIMIT :lim""", m=mint, lim=limit)
    positions = await _rows(session, """SELECT id, engine, execution_mode, status, entry_at, entry_price, exit_reason, realized_pnl,
                                        lifecycle, execution_route FROM paper_positions WHERE asset_id = :m ORDER BY entry_at""", m=mint)
    orders = await _rows(session, """SELECT side, reason, status, signature, error, created_at, submitted_at, confirmed_at
                                     FROM execution_orders WHERE mint = :m ORDER BY created_at""", m=mint)
    # What happened to each position after entry: exit-intelligence verdicts
    # (with their reasons), migrations, live fills and failures.
    events = await _rows(session, """SELECT e.occurred_at, e.event_type, e.detail FROM trade_timeline_events e
                                     JOIN paper_positions p ON p.id = e.position_id
                                     WHERE p.asset_id = :m ORDER BY e.occurred_at LIMIT :lim""", m=mint, lim=limit)
    return {"mint": mint, "observation": obs, "candidates": cands, "assessments": assessments, "positions": positions,
            "orders": orders, "position_events": events}


async def code_examples(session: AsyncSession, code: str, since: datetime, limit: int = 3) -> list[dict]:
    """The latest assessments blocked by `code`, with the numbers behind it:
    the finding, the plan's caps and cost split, the curve/pool reserves and
    the market features the gate saw."""
    return await _rows(session, """
        SELECT ra.evaluated_at, ra.engine, ra.asset_id, ra.decision,
               (SELECT f->>'message' FROM jsonb_array_elements(ra.assessment->'findings') f
                WHERE f->>'code' = :code LIMIT 1) message,
               ra.assessment->'plan'->'caps' caps, ra.assessment->'plan'->>'entry_cost_bps' entry_cost_bps,
               ra.assessment->'plan'->>'exit_cost_bps' exit_cost_bps, ra.assessment->'plan'->>'stop_distance_pct' stop_pct,
               ra.assessment->'plan'->'max_loss'->>'value' max_loss,
               ra.assessment->'inputs_snapshot'->'curve' curve, ra.assessment->'inputs_snapshot'->'pool' pool,
               ra.assessment->'inputs_snapshot'->'features' features,
               ra.assessment->'inputs_snapshot'->>'volatility_source' volatility_source,
               ra.assessment->'inputs_snapshot'->'errors' errors
        FROM risk_assessments ra
        WHERE ra.evaluated_at >= :since AND EXISTS (
            SELECT 1 FROM jsonb_array_elements(ra.assessment->'findings') f WHERE f->>'code' = :code)
        ORDER BY ra.evaluated_at DESC LIMIT :lim""", code=code, since=since, lim=limit)


def since_hours(hours: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(hours=hours)
