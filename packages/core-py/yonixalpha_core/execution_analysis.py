"""Execution diagnostics for LIVE orders: where the time went and why the
price differed from the decision.

Nothing here changes how a trade is built, guarded, signed or sent. It
reads what the decision, the executor stages and the confirmed transaction
already recorded, and stores the result in `execution_orders.diagnostics`:

  decision   the state the BUY was decided on (price, market cap, liquidity,
             curve/pool reserves, data age, evaluation timings)
  timing     wall-clock timestamps per stage and the derived latencies
             (decision_to_submit_ms, decision_to_confirm_ms, ...)
  price      decision price → spot at build → spot just before our trade
             (from the program's own trade event) → our trade price →
             all-in price, decomposed into market movement, price impact,
             fees and slippage, with ONE classification and its evidence

Units: prices are SOL per whole token (decimals applied); percentages are
relative to the previous price in the chain. A number that could not be
measured is None with the reason, never estimated.
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from yonixalpha_core.solana import pumpfun, pumpswap

LAMPORTS = Decimal(1_000_000_000)

# Thresholds for the classification (percent of price). A contributor must
# be at least this large to be named as the cause.
SIGNIFICANT_PCT = Decimal("2")
STALE_DECISION_SECONDS = 10  # decision price older than this at decision time = DATA_STALENESS
SLOW_LANDING_SLOTS = 8  # blockhash slot → landed slot beyond this = inclusion delay
SLOW_SUBMIT_MS = 1500  # pickup → submitted beyond this = our own pipeline was slow


def _d(v: Any) -> Decimal | None:
    try:
        return None if v is None else Decimal(str(v))
    except Exception:  # noqa: BLE001
        return None


def _pct(a: Decimal | None, b: Decimal | None) -> Decimal | None:
    """(a / b - 1) in percent."""
    if a is None or b is None or b == 0:
        return None
    return ((a / b - 1) * 100).quantize(Decimal("0.01"))


def _iso(ts: float | None) -> str | None:
    return None if ts is None else datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _ts(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, datetime):
        return v.timestamp()
    try:
        return datetime.fromisoformat(str(v)).timestamp()
    except ValueError:
        return None


def _ms(a: float | None, b: float | None) -> int | None:
    return None if a is None or b is None else int(round((b - a) * 1000))


# --- decision context -----------------------------------------------------------------

def decision_context(inp: Any, evidence: dict[str, Any], assessment: Any, started_at: float, finished_at: float,
                     lifecycle: str, operator_requested_at: float | None = None) -> dict[str, Any]:
    """What the BUY was decided on. `inp` is the gate's AssessmentInput."""
    m = getattr(inp, "market", None)
    tok = getattr(inp, "token", None)
    price = _d(getattr(m, "price", None)) if m is not None else None
    supply = None
    if tok is not None and getattr(tok, "supply_raw", None) is not None and getattr(tok, "decimals", None) is not None:
        supply = Decimal(tok.supply_raw) / Decimal(10) ** tok.decimals
    obs = getattr(m, "observation", None) if m is not None else None
    observed_at = getattr(obs, "observed_at", None) if obs is not None else None
    age = (datetime.fromtimestamp(finished_at, tz=timezone.utc) - observed_at).total_seconds() if observed_at else None
    flow = getattr(inp, "flow", None)
    plan = getattr(assessment, "plan", None)
    return {
        "lifecycle": lifecycle,
        "price_sol": str(price) if price is not None else None,
        "market_cap_sol": str((price * supply).quantize(Decimal("0.0001"))) if price is not None and supply is not None else None,
        "fdv_note": "market cap = price × total supply (Pump supply is fixed; no separate FDV)",
        "liquidity_sol": str(getattr(m, "liquidity_quote", None)) if m is not None and getattr(m, "liquidity_quote", None) is not None else None,
        "price_source": getattr(obs, "source", None) if obs is not None else None,
        "price_observed_at": observed_at.isoformat() if observed_at else None,
        "price_age_seconds": round(age, 2) if age is not None else None,
        "planned_entry_price": str(plan.entry_price) if plan is not None and getattr(plan, "entry_price", None) is not None else None,
        "size_sol": str(plan.position_size.value) if plan is not None and getattr(plan, "position_size", None) else None,
        "buyers": getattr(flow, "unique_buyers", None) if flow is not None else None,
        "sellers": getattr(flow, "unique_sellers", None) if flow is not None else None,
        "decision_started_at": _iso(started_at), "decision_at": _iso(finished_at),
        "approval_at": _iso(operator_requested_at or finished_at),
        "operator_requested_at": _iso(operator_requested_at),
        "decision_eval_ms": _ms(started_at, finished_at),
        "data_timings_ms": evidence.get("timings_ms"),
        "data_errors": [str(e)[:160] for e in (evidence.get("errors") or [])][:6],
        "stream_trades": evidence.get("stream_trades"),
    }


# --- our own trade event ------------------------------------------------------------

def own_trade_event(logs: list[str], wallet: str, mint: str) -> dict[str, Any] | None:
    """The Pump / PumpSwap trade event of `wallet` in a confirmed
    transaction's logs: exactly what the program charged, against which
    reserves. None when the logs hold no such event (e.g. a Jupiter route)."""
    for kind, f in pumpfun.decode_log_events(logs or []):
        if kind == "trade" and f.get("user") == wallet and f.get("mint") == mint:
            buy = bool(f.get("is_buy"))
            sol, tok = int(f["sol_amount"]), int(f["token_amount"])
            vs_post, vt_post = int(f["virtual_sol_reserves"]), int(f["virtual_token_reserves"])
            # The event reports reserves AFTER the trade.
            vs_pre = vs_post - sol if buy else vs_post + sol
            vt_pre = vt_post + tok if buy else vt_post - tok
            return {"venue": "PUMP_BONDING_CURVE", "is_buy": buy, "quote_lamports": sol, "token_raw": tok,
                    "fee_lamports": int(f.get("fee") or 0), "creator_fee_lamports": int(f.get("creator_fee") or 0),
                    "reserves_before": {"quote": vs_pre, "base": vt_pre}, "reserves_after": {"quote": vs_post, "base": vt_post}}
    for line in logs or []:
        if not line.startswith("Program data: "):
            continue
        try:
            raw = base64.b64decode(line[len("Program data: "):])
            ev = pumpswap.decode_trade_event(raw)
        except Exception:  # noqa: BLE001 - not a PumpSwap event
            continue
        if ev is None or ev.user != wallet:
            continue
        r = pumpswap.BorshReader(raw, 8)
        r.i64()
        r.u64(), r.u64(), r.u64(), r.u64(), r.u64(), r.u64()
        swap_quote = r.u64()  # buy: quote into the pool before fees; sell: out of the pool before fees
        return {"venue": "PUMP_AMM", "is_buy": ev.is_buy, "quote_lamports": swap_quote, "token_raw": ev.base_raw,
                "user_quote_lamports": ev.quote_lamports, "fee_bps": ev.fee_bps, "pool": ev.pool,
                # Event reserves exclude the pool's virtual quote reserves.
                "reserves_before_excl_virtual": {"quote": ev.pool_quote, "base": ev.pool_base}}
    return None


# --- what the wallet paid -------------------------------------------------------------

BASE_FEE_PER_SIGNATURE = 5000


def _all_keys(tx: dict) -> list[str]:
    keys = ((tx.get("transaction") or {}).get("message") or {}).get("accountKeys") or []
    out = [k["pubkey"] if isinstance(k, dict) else k for k in keys]
    loaded = (tx.get("meta") or {}).get("loadedAddresses") or {}
    return out + list(loaded.get("writable") or []) + list(loaded.get("readonly") or [])


def _parsed_instructions(tx: dict) -> list[dict]:
    outer = ((tx.get("transaction") or {}).get("message") or {}).get("instructions") or []
    inner = [ix for group in ((tx.get("meta") or {}).get("innerInstructions") or []) for ix in (group.get("instructions") or [])]
    return [ix for ix in outer + inner if isinstance(ix, dict) and isinstance(ix.get("parsed"), dict)]


def cost_breakdown(tx: dict, wallet: str, mint: str, event: dict[str, Any] | None = None) -> dict[str, Any]:
    """Where the wallet's SOL went in one confirmed transaction (lamports),
    from the transaction itself (getTransaction, jsonParsed):

      trade             SOL into (buy) / out of (sell) the curve or pool, before fees
      program_fees      protocol + creator (+ LP) fees, from our trade event
      network_fee       base + priority fee (meta.fee)
      deposits          SOL put into accounts this transaction created
                        (system createAccount from the wallet); a token
                        account's deposit is rent, returned only when that
                        account is closed
      refunds           SOL returned by accounts closed to the wallet (e.g.
                        the temporary wrapped-SOL account)
      residual          wallet change not explained by the above (0 when
                        everything is accounted for)
    """
    meta = tx.get("meta") or {}
    keys = _all_keys(tx)
    i = keys.index(wallet)
    pre, post = meta["preBalances"], meta["postBalances"]
    wallet_change = int(post[i]) - int(pre[i])
    token_owner: dict[str, tuple[str | None, str | None]] = {}
    for b in (meta.get("preTokenBalances") or []) + (meta.get("postTokenBalances") or []):
        idx = b.get("accountIndex")
        if isinstance(idx, int) and idx < len(keys):
            token_owner.setdefault(keys[idx], (b.get("mint"), b.get("owner")))

    deposits, refunds = [], []
    for ix in _parsed_instructions(tx):
        kind, info = ix["parsed"].get("type"), ix["parsed"].get("info") or {}
        if ix.get("program") == "system" and kind in ("createAccount", "createAccountWithSeed") and info.get("source") == wallet:
            acct = info.get("newAccount")
            m, owner = token_owner.get(acct, (None, None))
            what = ("token account for this token (rent: returned when the account is closed)" if m == mint and owner == wallet
                    else "wrapped-SOL token account" if owner == wallet else "program account")
            deposits.append({"account": acct, "lamports": int(info.get("lamports") or 0), "what": what, "space": info.get("space")})
        elif ix.get("program") in ("spl-token", "spl-token-2022") and kind == "closeAccount" and info.get("destination") == wallet:
            acct = info.get("account")
            j = keys.index(acct) if acct in keys else None
            refunds.append({"account": acct, "lamports": int(pre[j]) if j is not None else 0})
    # An account created and closed in the same transaction returned its deposit
    # plus whatever it held (wrapped SOL): count only the deposit as refunded.
    created = {d["account"]: d["lamports"] for d in deposits}
    for r in refunds:
        if r["account"] in created:
            r["lamports"] = created[r["account"]]
    net_deposits = sum(d["lamports"] for d in deposits) - sum(r["lamports"] for r in refunds)

    fee = int(meta.get("fee") or 0) if i == 0 else 0
    sigs = len(((tx.get("transaction") or {}).get("signatures")) or []) or 1
    trade = program_fees = None
    if event:
        if event.get("venue") == "PUMP_BONDING_CURVE":
            trade = int(event["quote_lamports"])
            program_fees = int(event.get("fee_lamports") or 0) + int(event.get("creator_fee_lamports") or 0)
        elif event.get("user_quote_lamports") is not None:
            trade = int(event["quote_lamports"])
            program_fees = abs(int(event["user_quote_lamports"]) - trade)
    residual = None
    if trade is not None:
        if event.get("is_buy"):
            residual = -wallet_change - (trade + program_fees + fee + net_deposits)
        else:
            residual = wallet_change - (trade - program_fees - fee - net_deposits)
    return {
        "wallet_change_lamports": wallet_change, "trade_lamports": trade, "program_fees_lamports": program_fees,
        "network_fee_lamports": fee, "priority_fee_lamports": max(0, fee - BASE_FEE_PER_SIGNATURE * sigs) if fee else 0,
        "deposits": deposits, "refunds": refunds, "net_deposits_lamports": net_deposits,
        "token_account_rent_lamports": sum(d["lamports"] for d in deposits if d["what"].startswith("token account for")),
        "residual_lamports": residual,
    }


# --- timing ---------------------------------------------------------------------------

STAGE_ORDER = ("VENUE_RESOLVED", "TRANSACTION_BUILT", "TRANSACTION_GUARD_PASSED", "TRANSACTION_SIGNED", "SIMULATED",
               "TRANSACTION_SUBMITTED", "TRANSACTION_SEEN", "TRANSACTION_CONFIRMED", "FILL_VERIFIED")


def timing(order_created_at: Any, result: dict[str, Any] | None, decision: dict[str, Any] | None,
           discovered_at: Any = None, token_created_at: Any = None) -> dict[str, Any]:
    """Timestamps and latencies of one order, from the recorded stages."""
    stages = (result or {}).get("stages") or []
    first: dict[str, float] = {}
    last: dict[str, float] = {}
    for st in stages:
        at = _ts(st.get("at"))
        if at is None:
            continue
        first.setdefault(st["stage"], at)
        last[st["stage"]] = at
    dec = decision or {}
    t_decision = _ts(dec.get("decision_at"))
    t_approval = _ts(dec.get("approval_at")) or t_decision
    t_order = _ts(order_created_at)
    t_pickup = _ts(stages[0].get("at")) if stages else None
    t_venue = first.get("VENUE_RESOLVED")
    t_built = last.get("TRANSACTION_BUILT")  # the build that was signed (after any venue rebuild)
    t_signed = first.get("TRANSACTION_SIGNED")
    t_sim = first.get("SIMULATED")
    t_sub = first.get("TRANSACTION_SUBMITTED")
    t_seen = first.get("TRANSACTION_SEEN")
    t_conf = first.get("TRANSACTION_CONFIRMED")
    calls = (result or {}).get("rpc_calls") or []
    rpc_ms = sum(int(c.get("ms") or 0) for c in calls)
    before_submit = [c for c in calls if t_sub is None or (_ts(c.get("at")) or 0) <= t_sub]
    out = {
        "timestamps": {
            "token_created_at": _iso(_ts(token_created_at)), "discovered_at": _iso(_ts(discovered_at)),
            "decision_started_at": dec.get("decision_started_at"), "decision_at": dec.get("decision_at"),
            "approval_at": _iso(t_approval), "order_created_at": _iso(t_order), "worker_pickup_at": _iso(t_pickup),
            "quote_at": _iso(t_venue), "transaction_built_at": _iso(t_built), "signed_at": _iso(t_signed),
            "simulated_at": _iso(t_sim), "submitted_at": _iso(t_sub), "first_seen_at": _iso(t_seen),
            "confirmed_at": _iso(t_conf),
        },
        "discovery_to_decision_ms": _ms(_ts(discovered_at), t_decision),
        "decision_eval_ms": dec.get("decision_eval_ms"),
        "approval_to_order_ms": _ms(t_approval, t_order),
        "queue_wait_ms": _ms(t_order, t_pickup),
        "quote_latency_ms": _ms(t_pickup, t_venue),
        "build_ms": _ms(t_venue, t_built),
        "guard_and_recheck_ms": _ms(t_built, t_signed),
        "simulation_ms": _ms(t_signed, t_sim),
        "submission_latency_ms": _ms(t_sim or t_signed, t_sub),
        "submit_to_seen_ms": _ms(t_sub, t_seen),
        "submit_to_confirm_ms": _ms(t_sub, t_conf),
        "decision_to_submit_ms": _ms(t_approval, t_sub),
        "decision_to_confirm_ms": _ms(t_approval, t_conf),
        "rpc_latency_ms": rpc_ms if calls else None,
        "rpc_latency_before_submit_ms": sum(int(c.get("ms") or 0) for c in before_submit) if calls else None,
        "rpc_calls_before_submit": len(before_submit) if calls else None,
    }
    built = next((s for s in reversed(stages) if s.get("stage") == "TRANSACTION_BUILT"), {})
    confirmed = next((s for s in stages if s.get("stage") == "TRANSACTION_CONFIRMED"), {})
    b_slot, l_slot = built.get("blockhash_slot"), confirmed.get("slot")
    out["blockhash_slot"], out["landed_slot"] = b_slot, l_slot
    out["slots_to_land"] = (l_slot - b_slot) if isinstance(b_slot, int) and isinstance(l_slot, int) else None
    return out


# --- price analysis -------------------------------------------------------------------

def price_analysis(side: str, amount_sol: Any, decimals: int | None, result: dict[str, Any] | None,
                   decision: dict[str, Any] | None, t: dict[str, Any], priority_fee_sol: Any = None) -> dict[str, Any]:
    """Decision price → execution price for a confirmed BUY, decomposed and
    classified. SELLs get the realized numbers without classification."""
    res = result or {}
    fill = res.get("fill") or {}
    ev = res.get("trade_event")
    out: dict[str, Any] = {"classification": "UNKNOWN", "evidence": [], "components_pct": {}}
    if decimals is None or not fill:
        out["evidence"].append("no confirmed fill or token decimals unknown")
        return out
    unit = Decimal(10) ** int(decimals)
    tokens = Decimal(abs(int(fill.get("token_change_raw") or 0))) / unit
    sol = Decimal(abs(int(fill.get("sol_change_lamports") or 0))) / LAMPORTS
    if tokens <= 0:
        out["evidence"].append("fill moved no tokens")
        return out
    all_in = sol / tokens
    out["all_in_price_sol"] = str(all_in)
    out["network_fee_sol"] = str(Decimal(int(fill.get("fee_lamports") or 0)) / LAMPORTS)
    out["priority_fee_sol"] = str(priority_fee_sol) if priority_fee_sol is not None else None
    if side != "BUY":
        out["classification"] = None
        out["evidence"].append("sell: realized price recorded; classification applies to buys")
        return out

    dec = decision or {}
    p_decision = _d(dec.get("price_sol"))
    venue = res.get("venue") or {}
    spot_build = None
    if "curve" in venue and venue["curve"].get("virtual_tokens"):
        c = venue["curve"]
        spot_build = (Decimal(c["virtual_sol"]) / LAMPORTS) / (Decimal(c["virtual_tokens"]) / unit)
    elif "pool" in venue and venue["pool"].get("base_reserve"):
        pl = venue["pool"]
        spot_build = (Decimal(pl["quote_reserve"]) / LAMPORTS) / (Decimal(pl["base_reserve"]) / unit)
    built = next((s for s in reversed(res.get("stages") or []) if s.get("stage") == "TRANSACTION_BUILT"), {})
    expected_tokens = _d(built.get("tokens_out"))
    expected_price = (Decimal(str(amount_sol)) / (expected_tokens / unit)) if expected_tokens else None

    spot_pre = trade_price = None
    fees_pct = None
    if ev:
        if ev.get("venue") == "PUMP_BONDING_CURVE":
            rb = ev["reserves_before"]
            spot_pre = (Decimal(rb["quote"]) / LAMPORTS) / (Decimal(rb["base"]) / unit) if rb["base"] else None
        else:
            virtual = None
            if "pool" in venue and venue["pool"].get("virtual_quote") is not None:
                virtual = int(venue["pool"]["virtual_quote"])
            rb = ev.get("reserves_before_excl_virtual") or {}
            if virtual is not None and rb.get("base"):
                spot_pre = (Decimal(rb["quote"] + virtual) / LAMPORTS) / (Decimal(rb["base"]) / unit)
        if ev.get("token_raw"):
            trade_price = (Decimal(ev["quote_lamports"]) / LAMPORTS) / (Decimal(ev["token_raw"]) / unit)
        if trade_price:
            fees_pct = _pct(all_in, trade_price)
    else:
        out["evidence"].append("no Pump/PumpSwap trade event in the logs (route without an event, or logs unavailable)")

    comp = {
        "decision_to_build_pct": _pct(spot_build, p_decision),  # market moved while deciding/queueing
        "build_to_landing_pct": _pct(spot_pre, spot_build),  # other trades landed before ours
        "price_impact_pct": _pct(trade_price, spot_pre),  # our own size against the reserves
        "fees_pct": fees_pct,  # all the wallet paid beyond the trade: program fees, network fee, new-account deposits
        "vs_expected_pct": _pct(all_in, expected_price),  # all-in vs the build's own expectation
        "total_vs_decision_pct": _pct(all_in, p_decision),
    }
    costs = res.get("costs") or None
    if costs and costs.get("trade_lamports"):
        trade_l = Decimal(costs["trade_lamports"])
        comp["program_fees_pct"] = _pct(trade_l + Decimal(costs.get("program_fees_lamports") or 0), trade_l)
        comp["network_fee_pct"] = _pct(trade_l + Decimal(costs.get("network_fee_lamports") or 0), trade_l)
        comp["deposits_pct"] = _pct(trade_l + Decimal(costs.get("net_deposits_lamports") or 0), trade_l)
        out["costs_sol"] = {k: str(Decimal(costs[k]) / LAMPORTS) if costs.get(k) is not None else None
                            for k in ("trade_lamports", "program_fees_lamports", "network_fee_lamports",
                                      "priority_fee_lamports", "net_deposits_lamports", "token_account_rent_lamports",
                                      "residual_lamports")}
    out["components_pct"] = {k: (str(v) if v is not None else None) for k, v in comp.items()}
    out.update({"decision_price_sol": str(p_decision) if p_decision is not None else None,
                "spot_at_build_sol": str(spot_build) if spot_build is not None else None,
                "expected_price_sol": str(expected_price) if expected_price is not None else None,
                "spot_before_trade_sol": str(spot_pre) if spot_pre is not None else None,
                "trade_price_sol": str(trade_price) if trade_price is not None else None})

    ranked = sorted(((abs(v), k) for k, v in comp.items()
                     if v is not None and k in ("decision_to_build_pct", "build_to_landing_pct", "price_impact_pct", "fees_pct")),
                    reverse=True)
    if not ranked or ranked[0][0] < SIGNIFICANT_PCT:
        out["classification"] = "WITHIN_EXPECTED" if ranked else "UNKNOWN"
        if ranked:
            out["evidence"].append(f"every component below {SIGNIFICANT_PCT}%")
        return out
    top = ranked[0][1]
    age = dec.get("price_age_seconds")
    if top == "decision_to_build_pct":
        if age is not None and age > STALE_DECISION_SECONDS:
            out["classification"] = "DATA_STALENESS"
            out["evidence"].append(f"decision price was {age}s old when decided")
        elif (t.get("queue_wait_ms") or 0) + (t.get("decision_eval_ms") or 0) > 5000:
            out["classification"] = "MARKET_MOVED"
            out["evidence"].append(f"price moved {comp[top]}% during decision ({t.get('decision_eval_ms')} ms) "
                                   f"and queueing ({t.get('queue_wait_ms')} ms)")
        else:
            out["classification"] = "MARKET_MOVED"
            out["evidence"].append(f"price moved {comp[top]}% between decision and build")
    elif top == "build_to_landing_pct":
        slots = t.get("slots_to_land")
        sub = t.get("submission_latency_ms")
        if isinstance(slots, int) and slots > SLOW_LANDING_SLOTS:
            out["classification"] = "PRIORITY_FEE_DELAY"
            out["evidence"].append(f"{slots} slots from blockhash to landing while the curve moved {comp[top]}%")
        elif sub is not None and sub > SLOW_SUBMIT_MS:
            out["classification"] = "RPC_LATENCY"
            out["evidence"].append(f"{sub} ms from simulation to submission while the curve moved {comp[top]}%")
        else:
            out["classification"] = "CURVE_MOVEMENT" if ev and ev.get("venue") == "PUMP_BONDING_CURVE" else "MARKET_MOVED"
            out["evidence"].append(f"trades that landed before ours moved the price {comp[top]}%")
    elif top == "price_impact_pct":
        out["classification"] = "PRICE_IMPACT"
        out["evidence"].append(f"our {amount_sol} SOL moved the price {comp[top]}% against the reserves")
    elif top == "fees_pct":
        dep, pf, nf = comp.get("deposits_pct"), comp.get("program_fees_pct"), comp.get("network_fee_pct")
        if dep is not None and dep > (pf or 0) + (nf or 0):
            out["classification"] = "ACCOUNT_RENT"
            out["evidence"].append(
                f"{out['costs_sol']['net_deposits_lamports']} SOL (+{dep}%) went into new accounts, of which "
                f"{out['costs_sol']['token_account_rent_lamports']} SOL is the token account's rent (returned only when "
                f"that account is closed); program fees +{pf}%, network fee +{nf}%")
        else:
            out["classification"] = "FEES"
            if dep is not None:
                out["evidence"].append(f"costs on top of the trade price: program fees +{pf}%, network fee +{nf}%, "
                                       f"new-account deposits +{dep}%")
            else:
                out["evidence"].append(
                    f"costs added {comp[top]}% on top of the trade price: program fees, the network + priority fee and "
                    "SOL deposited into any new account (token account rent); cost_report itemizes them")
    if comp.get("vs_expected_pct") is not None and comp["vs_expected_pct"] > SIGNIFICANT_PCT:
        out["evidence"].append(f"all-in {comp['vs_expected_pct']}% above the build's expected price (the slippage limit "
                               "covers the trade amount only; fees and new-account deposits come on top)")
    return out


def analyze(order: Any, position: Any = None, candidate: Any = None) -> dict[str, Any]:
    """diagnostics for a finished order (confirmed or not)."""
    diag = dict(order.diagnostics or {})
    decision = diag.get("decision")
    decimals = (order.limits or {}).get("decimals")
    if decimals is None and position is not None:
        decimals = ((position.plan or {}).get("venue") or {}).get("decimals")
    discovered = getattr(candidate, "created_at", None) if candidate is not None else None
    t = timing(order.created_at, order.result, decision, discovered, (decision or {}).get("token_created_at"))
    diag["timing"] = t
    if order.status == "CONFIRMED":
        diag["price"] = price_analysis(order.side, order.amount if order.amount_kind == "sol" else None,
                                       int(decimals) if decimals is not None else None, order.result, decision, t,
                                       order.priority_fee_sol)
    return diag
