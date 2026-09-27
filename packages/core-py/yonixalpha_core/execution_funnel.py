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


# Status ladder, in order. A token's stage is the furthest it reached; each
# stage is a separate fact from the database, so "PROMOTE" can never be read
# as "bought":
#   OBSERVED            the fresh-token funnel (or a momentum/migration prefilter) saw it
#   ANALYSIS_POSITIVE   activity looked positive (trend INCREASING/STABLE, or a prefilter passed)
#   PROMOTE             handed to the safety gate as a candidate ("deserves consideration")
#   BUY_SIGNAL          the strategy signal qualified in at least one gate evaluation
#   RISK_APPROVED       BUY signal and no risk-side blocker (token, holders, flow, market,
#                       liquidity, data, creator, approval ceiling); only execution-side
#                       findings (sizing, costs, account limits, routes, mode) may remain
#   EXECUTION_APPROVED  the gate decided EXECUTE / REDUCE_SIZE: permitted to submit the buy
#   BUY_SUBMITTED       LIVE: order signed and sent; PAPER: simulated entry attempted
#   BUY_CONFIRMED       LIVE: confirmed on chain with the balance change; PAPER: simulated fill
#   POSITION_OPEN / SELL_SUBMITTED / SELL_CONFIRMED / POSITION_CLOSED
LADDER = ("OBSERVED", "ANALYSIS_POSITIVE", "PROMOTE", "BUY_SIGNAL", "RISK_APPROVED", "EXECUTION_APPROVED",
          "BUY_SUBMITTED", "BUY_CONFIRMED", "POSITION_OPEN", "SELL_SUBMITTED", "SELL_CONFIRMED", "POSITION_CLOSED")
BLOCKING_ACTIONS = ("REJECT", "NO_TRADE", "WAIT", "REQUIRE_MANUAL_APPROVAL")
# Execution-side: whether THIS trade can be carried out now (size, costs,
# account limits, routes, mode). Everything else is risk-side.
EXECUTION_SIDE_CATEGORIES = ("EXECUTION", "ACCOUNT")
RISK_SIDE_ACCOUNT_CODES = ("RISK_ABOVE_AUTO_CEILING",)
EXECUTION_SIDE_CODES = ("MANUAL_MODE", "STRATEGY_OFF")
_RISK_OK = ("NOT EXISTS (SELECT 1 FROM jsonb_array_elements(ra.assessment->'findings') r "
            "WHERE r->>'action' IN ('REJECT', 'NO_TRADE', 'WAIT', 'REQUIRE_MANUAL_APPROVAL') "
            "AND NOT ((r->>'category' IN ('EXECUTION', 'ACCOUNT') AND r->>'code' <> 'RISK_ABOVE_AUTO_CEILING') "
            "OR r->>'code' IN ('MANUAL_MODE', 'STRATEGY_OFF')))")
ROUTE_CODES = {"NO_BUY_ROUTE", "ROUTE_UNVERIFIED", "EXECUTION_UNAVAILABLE", "LIVE_NOT_READY", "LIVE_NOT_PERMITTED"}
SELLABILITY_CODES = {"NO_SELL_ROUTE", "SELL_TAX_EXCESSIVE", "TAX_UNKNOWN", "EXIT_IMPACT", "EXIT_COSTS", "ROUND_TRIP_LOSS",
                     "FREEZE_AUTHORITY", "DEFAULT_FROZEN", "PAUSED", "PAUSABLE", "TRANSFER_FEE", "TRANSFER_FEE_MUTABLE"}
LIQUIDITY_CODES = {"BOOK_TOO_THIN", "ENTRY_IMPACT"}
BLOCKER_GROUPS = ("exit_signal_at_entry", "liquidity", "sellability", "stale_or_missing_data", "route", "sizing_account",
                  "mode", "risk")


def blocker_group(code: str, category: str | None) -> str:
    """Which dashboard group a blocking finding belongs to."""
    if code == "EXIT_SIGNAL_AT_ENTRY":
        return "exit_signal_at_entry"
    if code in ROUTE_CODES:
        return "route"
    if code in SELLABILITY_CODES:
        return "sellability"
    if category == "LIQUIDITY" or code in LIQUIDITY_CODES:
        return "liquidity"
    if category == "DATA":
        return "stale_or_missing_data"
    if code in EXECUTION_SIDE_CODES:
        return "mode"
    if category in EXECUTION_SIDE_CATEGORIES and code not in RISK_SIDE_ACCOUNT_CODES:
        return "sizing_account"
    return "risk"


def _stage_of(tok: dict[str, Any]) -> str:
    stage = "OBSERVED"
    for name, reached in (("ANALYSIS_POSITIVE", tok.get("analysis_positive")), ("PROMOTE", tok.get("candidate_id")),
                          ("BUY_SIGNAL", tok.get("any_signal")), ("RISK_APPROVED", tok.get("any_risk_ok")),
                          ("EXECUTION_APPROVED", tok.get("any_executable")), ("BUY_SUBMITTED", tok.get("buy_submitted")),
                          ("BUY_CONFIRMED", tok.get("buy_confirmed")), ("POSITION_OPEN", tok.get("position_opened")),
                          ("SELL_SUBMITTED", tok.get("sell_submitted")), ("SELL_CONFIRMED", tok.get("sell_confirmed")),
                          ("POSITION_CLOSED", tok.get("position_closed"))):
        if reached:
            stage = name
    return stage


def _final_blocker(tok: dict[str, Any]) -> dict[str, Any] | None:
    """Exactly why the token stopped where it did, from its own records."""
    stage = tok["stage"]
    if stage in ("POSITION_OPEN", "SELL_SUBMITTED", "SELL_CONFIRMED", "POSITION_CLOSED"):
        return None
    if tok.get("order_failure"):
        return {"stage": stage, "code": tok["order_failure"]["code"], "reason": tok["order_failure"]["error"]}
    if tok.get("entry_failure"):
        return {"stage": stage, "code": tok["entry_failure"]["event_type"].upper(), "reason": tok["entry_failure"]["reason"]}
    if stage == "EXECUTION_APPROVED":
        return {"stage": stage, "code": "AWAITING_EXECUTION",
                "reason": tok.get("order_status") and f"BUY order {tok['order_status']}" or tok.get("last_reason")}
    latest = tok.get("latest") or {}
    blocking = latest.get("blocking") or []
    if blocking:
        codes = [b["code"] for b in blocking]
        return {"stage": stage, "code": codes[0], "codes": codes, "groups": sorted({blocker_group(b["code"], b.get("category"))
                                                                                   for b in blocking}),
                "reason": blocking[0].get("message"), "decision": latest.get("decision")}
    if tok.get("candidate_id"):
        return {"stage": stage, "code": f"CANDIDATE_{str(tok.get('state') or '').upper()}",
                "reason": tok.get("last_reason") or "no gate evaluation recorded"}
    return {"stage": stage, "code": f"OBSERVATION_{tok.get('observation_outcome') or 'PENDING'}",
            "reason": tok.get("observation_reason")}


async def pipeline(session: AsyncSession, since: datetime, engines: tuple[str, ...] = SOLANA_ENGINES,
                   token_limit: int = 150, mint: str | None = None) -> dict[str, Any]:
    """The status ladder for every candidate handed to the gate in the window
    (plus observation counts): how many tokens reached each stage, which
    blocker groups stopped the ones with a BUY signal, and each token's
    final stage and exact final blocker."""
    p = {"since": since}
    obs = (await _rows(session, """
        SELECT count(*) observed,
               count(*) FILTER (WHERE outcome = 'PROMOTE' OR trend IN ('INCREASING', 'STABLE')) positive
        FROM token_observations WHERE decided_at >= :since""", **p))[0]
    cands = await _rows(session, f"""
        WITH c AS (
          SELECT c.id, c.engine, c.state, c.created_at, t.mint_address mint, t.symbol,
                 c.state_history->-1->>'reason' last_reason
          FROM trading_candidates c JOIN tokens t ON t.id = c.token_id
          WHERE c.created_at >= :since AND c.detail->>'source' = 'pump_stream'
            AND (CAST(:mint AS text) IS NULL OR t.mint_address = :mint)),
        a AS (
          SELECT ra.candidate_id, count(*) n, bool_or({_SIGNAL_OK}) any_signal,
                 bool_or({_SIGNAL_OK} AND {_RISK_OK}) any_risk_ok, bool_or(ra.executable) any_executable
          FROM risk_assessments ra WHERE ra.candidate_id IN (SELECT id FROM c) GROUP BY ra.candidate_id),
        l AS (
          SELECT DISTINCT ON (ra.candidate_id) ra.candidate_id, ra.decision, ra.evaluated_at, ra.engine gate_engine,
                 (SELECT jsonb_agg(jsonb_build_object('code', f->>'code', 'category', f->>'category',
                                                      'message', left(f->>'message', 300)))
                  FROM jsonb_array_elements(ra.assessment->'findings') f
                  WHERE f->>'action' = ra.decision AND NOT ra.executable) blocking
          FROM risk_assessments ra WHERE ra.candidate_id IN (SELECT id FROM c)
          ORDER BY ra.candidate_id, ra.evaluated_at DESC, (ra.idempotency_key LIKE '%:smoke:%') ASC)
        SELECT c.*, coalesce(a.n, 0) assessments, a.any_signal, a.any_risk_ok, a.any_executable,
               l.decision latest_decision, l.evaluated_at latest_at, l.gate_engine, l.blocking latest_blocking
        FROM c LEFT JOIN a ON a.candidate_id = c.id LEFT JOIN l ON l.candidate_id = c.id
        ORDER BY c.created_at DESC""", mint=mint, **p)
    ids = [c["id"] for c in cands]
    positions: dict[Any, list[dict]] = {}
    orders: dict[Any, list[dict]] = {}
    failures: dict[Any, dict] = {}
    if ids:
        for r in await _rows(session, """
            SELECT id, candidate_id, execution_mode, status, exit_reason, execution_route, execution_provider
            FROM paper_positions WHERE candidate_id = ANY(:ids)""", ids=ids):
            positions.setdefault(r["candidate_id"], []).append(r)
        pos_ids = [r["id"] for v in positions.values() for r in v]
        if pos_ids:
            for r in await _rows(session, """
                SELECT p.candidate_id, o.side, o.status, o.submitted_at, o.signature, o.error, o.result
                FROM execution_orders o JOIN paper_positions p ON p.id = o.position_id
                WHERE o.position_id = ANY(:ids) ORDER BY o.created_at""", ids=pos_ids):
                orders.setdefault(r["candidate_id"], []).append(r)
        for r in await _rows(session, """
            SELECT DISTINCT ON (candidate_id) candidate_id, event_type,
                   left(coalesce(detail->>'reason', detail->>'error', detail->>'code', ''), 200) reason
            FROM trade_timeline_events WHERE candidate_id = ANY(:ids) AND event_type = ANY(:ev)
            ORDER BY candidate_id, occurred_at DESC""", ids=ids, ev=list(EXECUTION_FAILURE_EVENTS)):
            failures[r["candidate_id"]] = r

    from yonixalpha_core.live_trading import failure_code_of  # local: keeps this module import-light

    tokens: list[dict[str, Any]] = []
    for c in cands:
        pos = positions.get(c["id"], [])
        ords = orders.get(c["id"], [])
        buys = [o for o in ords if o["side"] == "BUY"]
        sells = [o for o in ords if o["side"] == "SELL"]
        paper = [x for x in pos if x["execution_mode"] == "PAPER"]
        live_filled = [x for x in pos if x["execution_mode"] == "LIVE" and x["status"] in ("open", "closed")]
        tok = {
            "mint": c["mint"], "symbol": c["symbol"], "engine": c["gate_engine"] or c["engine"], "candidate_id": c["id"],
            "state": c["state"], "promoted_at": c["created_at"], "last_reason": c["last_reason"], "analysis_positive": True,
            "assessments": c["assessments"], "any_signal": c["any_signal"], "any_risk_ok": c["any_risk_ok"],
            "any_executable": c["any_executable"],
            "latest": {"decision": c["latest_decision"], "at": c["latest_at"], "blocking": c["latest_blocking"]}
            if c["latest_decision"] else None,
            "execution_mode": (pos[0]["execution_mode"] if pos else None),
            "execution_route": (pos[0]["execution_route"] if pos else None),
            "execution_provider": (pos[0]["execution_provider"] if pos else None),
            "buy_submitted": bool(paper) or any(o["submitted_at"] for o in buys),
            "buy_confirmed": bool(paper) or any(o["status"] == "CONFIRMED" for o in buys),
            "position_opened": bool(paper) or bool(live_filled),
            "sell_submitted": any(x["status"] == "closed" for x in paper) or any(o["submitted_at"] for o in sells),
            "sell_confirmed": any(x["status"] == "closed" for x in paper) or any(o["status"] == "CONFIRMED" for o in sells),
            "position_closed": any(x["status"] == "closed" for x in pos),
            "order_status": buys[-1]["status"] if buys else None,
        }
        failed = [o for o in buys if o["status"] in ("FAILED", "EXPIRED", "CANCELLED")]
        if failed and not tok["buy_confirmed"]:
            o = failed[-1]
            tok["order_failure"] = {"code": failure_code_of("BUY", o["status"], o["error"], o["signature"], o["result"]),
                                    "error": (o["error"] or "")[:200]}
        elif c["id"] in failures and not tok["position_opened"]:
            tok["entry_failure"] = failures[c["id"]]
        tok["stage"] = _stage_of(tok)
        tok["final_blocker"] = _final_blocker(tok)
        tokens.append(tok)

    counts = {s: 0 for s in LADDER}
    counts["OBSERVED"] = _num(obs["observed"]) + sum(1 for c in cands if c["engine"] != "discovery")
    counts["ANALYSIS_POSITIVE"] = _num(obs["positive"]) + sum(1 for c in cands if c["engine"] != "discovery")
    for tok in tokens:
        reached = LADDER.index(tok["stage"])
        for s in LADDER[LADDER.index("PROMOTE"):reached + 1]:
            counts[s] += 1
    groups = {g: 0 for g in BLOCKER_GROUPS}
    for tok in tokens:
        fb = tok["final_blocker"]
        if tok["any_signal"] and not tok["any_executable"] and fb:
            for g in fb.get("groups") or []:
                groups[g] += 1
    not_assessed: dict[tuple, int] = {}
    for tok in tokens:
        if tok["assessments"] == 0:
            key = (tok["engine"], tok["state"], _normalise(tok["last_reason"]))
            not_assessed[key] = not_assessed.get(key, 0) + 1
    final_codes: dict[tuple, int] = {}
    for tok in tokens:
        if tok["final_blocker"]:
            key = (tok["engine"], tok["stage"], tok["final_blocker"]["code"])
            final_codes[key] = final_codes.get(key, 0) + 1
    return {
        "stages": counts,
        "blocked_by": groups,
        "final_blockers": [{"engine": k[0], "stage": k[1], "code": k[2], "tokens": v}
                           for k, v in sorted(final_codes.items(), key=lambda kv: -kv[1])][:40],
        "promoted_not_assessed": [{"engine": k[0], "state": k[1], "reason": k[2], "tokens": v}
                                  for k, v in sorted(not_assessed.items(), key=lambda kv: -kv[1])][:20],
        "tokens": [{k: v for k, v in tok.items() if k not in ("analysis_positive",)} for tok in tokens[:token_limit]],
    }


def _normalise(reason: str | None) -> str:
    """Groups reasons that differ only by numbers/addresses."""
    import re

    r = re.sub(r"[1-9A-HJ-NP-Za-km-z]{32,44}", "<addr>", reason or "(no reason recorded)")
    return re.sub(r"\d+(\.\d+)?", "#", r)[:160]


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

    out["pipeline"] = await pipeline(session, since, engines)

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
    pl = f.get("pipeline") or {}
    for row in (pl.get("promoted_not_assessed") or [])[:3]:
        notes.append(f"{row['tokens']} PROMOTED {row['engine']} candidate(s) never reached a gate evaluation "
                     f"(state {row['state']}): {row['reason']}")
    if (pl.get("blocked_by") or {}).get("exit_signal_at_entry"):
        notes.append(f"EXIT_SIGNAL_AT_ENTRY held back {pl['blocked_by']['exit_signal_at_entry']} token(s): exit intelligence "
                     "would have started selling them on the first tick.")
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
    pl = await pipeline(session, datetime(2000, 1, 1, tzinfo=timezone.utc), mint=mint)
    if pl["tokens"]:
        stage = {k: pl["tokens"][0][k] for k in ("stage", "final_blocker", "engine", "execution_mode", "execution_route",
                                                 "execution_provider")}
    else:
        o = obs[0] if obs else None
        positive = bool(o) and (o["outcome"] == "PROMOTE" or o["trend"] in ("INCREASING", "STABLE"))
        stage = {"stage": ("ANALYSIS_POSITIVE" if positive else "OBSERVED") if o else "NOT_OBSERVED",
                 "final_blocker": {"stage": "OBSERVED", "code": f"OBSERVATION_{o['outcome']}",
                                   "reason": (o["reasons"] or [None])[-1]} if o else None}
    return {"mint": mint, "observation": obs, "candidates": cands, "assessments": assessments, "positions": positions,
            "orders": orders, "position_events": events, "pipeline": stage}


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
