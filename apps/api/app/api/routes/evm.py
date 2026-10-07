"""EVM chains (BSC, Robinhood Chain): discovered tokens with category, stats,
safety, launch-window coordination and the last entry decision; per-token
trades; paper positions; the EVM trading settings (amounts in BNB / ETH) and
the launch-coordination settings and approvals."""

import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from redis.asyncio import Redis
from sqlalchemy import desc, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_username, get_db, get_redis, get_settings
from app.api.util import audit, jsonable
from yonixalpha_core import events, launch_coordination, position_pnl
from yonixalpha_core.chains.evm import native_price, token_view
from yonixalpha_core.chains.evm import paper as evm_paper
from yonixalpha_core.chains.evm import observation as evm_observation
from yonixalpha_core.chains.evm import settings as evm_settings
from yonixalpha_core.chains.evm import crosscheck as evm_crosscheck
from yonixalpha_core.chains.evm import streams as evm_streams
from yonixalpha_core.chains.evm import wallet as evm_wallet
from yonixalpha_core.chains.evm import rpc_registry as evm_rpc_registry
from yonixalpha_core.config import Settings
from yonixalpha_core.db.models import (CopyEvent, EvmCursor, EvmObservation, EvmScanGap, EvmToken, EvmTrade, PaperAccount,
                                       PaperPosition, PlatformSetting)

router = APIRouter(prefix="/evm", tags=["evm"])
CHAIN = "^(bsc|robinhood)$"


def _token(r: EvmToken, full: bool = False, native_usd: str | None = None) -> dict:
    d = {"chain": r.chain, "token": r.token, "launchpad": r.launchpad, "name": r.name, "symbol": r.symbol,
         "creator": r.creator, "created_at": r.created_at, "category": r.category, "stage": r.stage,
         "migrated_at": r.migrated_at, "safety_verdict": r.safety_verdict, "safety_at": r.safety_at,
         "stats": r.stats, "last_trade_at": r.last_trade_at, "launch_seen": bool((r.extra or {}).get("launch_seen")),
         "entry_decision": (r.extra or {}).get("entry_decision"),
         "coordination_status": (r.coordination or {}).get("status"),
         "coordination_action": (r.coordination or {}).get("action"), "coordination_at": r.coordination_at,
         "market": token_view.market(r.chain, r.state, r.extra, r.quote_token, native_usd)}
    if full:
        d.update(coordination=r.coordination, coordination_approval=(r.extra or {}).get("coordination_approval"),
                 venue=r.venue, quote_token=r.quote_token, migration=r.migration, state=r.state, state_at=r.state_at,
                 safety=r.safety, created_block=r.created_block, created_tx=r.created_tx, extra=r.extra)
    return d


@router.get("/tokens")
async def tokens(chain: str | None = Query(None, pattern=CHAIN), category: str | None = Query(None),
                 launchpad: str | None = None, active_minutes: int = Query(60, ge=1, le=10080),
                 limit: int = Query(100, ge=1, le=500), db: AsyncSession = Depends(get_db),
                 redis: Redis = Depends(get_redis), _: str = Depends(get_current_username)) -> dict:
    now = datetime.now(timezone.utc)
    since = now - timedelta(minutes=active_minutes)
    q = select(EvmToken).where(func.coalesce(EvmToken.last_trade_at, EvmToken.created_at) >= since)
    if chain:
        q = q.where(EvmToken.chain == chain)
    if category:
        q = q.where(EvmToken.category == category.upper())
    if launchpad:
        q = q.where(EvmToken.launchpad == launchpad)
    rows = (await db.execute(q.order_by(desc(func.coalesce(EvmToken.last_trade_at, EvmToken.created_at))).limit(limit))).scalars().all()
    counts = dict((await db.execute(select(EvmToken.category, func.count()).where(
        func.coalesce(EvmToken.last_trade_at, EvmToken.created_at) >= since,
        *([EvmToken.chain == chain] if chain else [])).group_by(EvmToken.category))).all())
    rates = {c: (await native_price.usd_rate(redis, c, now))["price"] for c in ("bsc", "robinhood")}
    return jsonable({"tokens": [_token(r, native_usd=rates[r.chain]) for r in rows], "categories": counts,
                     "native_usd": rates,
                     "note": "EVM discovery, quotes and safety run on the real chains; paper only (no EVM live execution)"})


@router.get("/tokens/{chain}/{token}")
async def token_detail(chain: str, token: str, db: AsyncSession = Depends(get_db), redis: Redis = Depends(get_redis),
                       _: str = Depends(get_current_username)) -> dict:
    row = (await db.execute(select(EvmToken).where(EvmToken.chain == chain,
                                                   func.lower(EvmToken.token) == token.lower()))).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "token not discovered")
    trades = (await db.execute(select(EvmTrade).where(EvmTrade.chain == chain, EvmTrade.token == row.token)
                               .order_by(desc(EvmTrade.at)).limit(200))).scalars().all()
    positions = (await db.execute(select(PaperPosition).where(
        PaperPosition.engine == f"evm_{chain}", func.lower(PaperPosition.asset_id) == row.token.lower())
        .order_by(desc(PaperPosition.entry_at)))).scalars().all()
    observations = (await db.execute(select(EvmObservation).where(
        EvmObservation.chain == chain, EvmObservation.token == row.token).order_by(EvmObservation.started_at))).scalars().all()
    rate = (await native_price.usd_rate(redis, chain, datetime.now(timezone.utc)))["price"]
    return jsonable({"token": _token(row, full=True, native_usd=rate), "observations": [_observation(o) for o in observations],
                     "trades": [{"event_id": t.event_id, "trader": t.trader, "side": "BUY" if t.is_buy else "SELL",
                                 "token_amount": str(t.token_amount), "quote_amount": str(t.quote_amount / 10 ** 18),
                                 "at": t.at, "block": t.block, "tx_hash": t.tx_hash} for t in trades],
                     "positions": [{"id": p.id, "status": p.status, "entry_at": p.entry_at, "entry_price": p.entry_price,
                                    "quantity": p.remaining_quantity, "stop_loss": p.stop_loss, "last_price": p.last_price,
                                    "realized_pnl": p.realized_pnl, "exit_reason": p.exit_reason,
                                    "venue": (p.plan or {}).get("venue"),
                                    "pnl": position_pnl.view(p, datetime.now(timezone.utc))} for p in positions]})


async def _unpriced(redis: Redis, pid) -> dict | None:
    """Why an open position has no sell quote (written by data-evm), or None."""
    raw, since = await redis.get(evm_paper.UNPRICED_KEY.format(pid=pid)), await redis.get(
        evm_paper.UNPRICED_SINCE_KEY.format(pid=pid))
    if not raw and not since:
        return None
    try:
        last = json.loads(raw) if raw else {}
    except ValueError:
        last = {}
    return {"reason": last.get("error") or "unknown", "at": last.get("at"), "since": since,
            "effect": "no sell quote: the position cannot be marked or exited (stop and take-profit cannot fire)"}


@router.get("/positions")
async def positions(chain: str | None = Query(None, pattern=CHAIN), status: str = Query("open", pattern="^(open|closed)$"),
                    limit: int = Query(100, ge=1, le=500), db: AsyncSession = Depends(get_db),
                    redis: Redis = Depends(get_redis), _: str = Depends(get_current_username)) -> dict:
    engines = [f"evm_{chain}"] if chain else ["evm_bsc", "evm_robinhood"]
    rows = (await db.execute(select(PaperPosition).where(PaperPosition.engine.in_(engines), PaperPosition.status == status)
                             .order_by(desc(PaperPosition.entry_at)).limit(limit))).scalars().all()
    accounts = (await db.execute(select(PaperAccount).where(PaperAccount.name.in_(engines)))).scalars().all()
    now = datetime.now(timezone.utc)
    out = []
    for p in rows:
        rem = p.remaining_quantity if p.remaining_quantity is not None else p.quantity
        value = (p.last_price or p.entry_price) * rem
        cost_open = (p.entry_cost_quote or 0) * (rem / (p.initial_quantity or p.quantity)) if (p.initial_quantity or p.quantity) else 0
        out.append({"id": p.id, "chain": p.engine.removeprefix("evm_"), "token": p.asset_id, "symbol": p.symbol,
                    "status": p.status, "entry_at": p.entry_at, "entry_price": p.entry_price, "quantity": rem,
                    "entry_cost": p.entry_cost_quote, "last_price": p.last_price, "last_marked_at": p.last_marked_at,
                    "stop_loss": p.stop_loss, "trailing_stop": p.trailing_stop, "tp_hits": p.tp_hits,
                    "unrealized_pnl": (value - cost_open) if p.status == "open" else None,
                    "realized_pnl": p.realized_pnl, "exit_reason": p.exit_reason, "exit_at": p.exit_at,
                    "venue": (p.plan or {}).get("venue"), "category": (p.plan or {}).get("category"),
                    "pnl_basis": "marked at the executable sell quote of the remaining tokens (fees and taxes included)",
                    "exit_requested": bool(p.exit_requested), "entry_source": (p.plan or {}).get("entry_source"),
                    "unpriced": await _unpriced(redis, p.id) if p.status == "open" else None,
                    "pnl": position_pnl.view(p, now)})
    return jsonable({"positions": out, "accounts": [{"name": a.name, "currency": a.quote_currency, "cash": a.cash_balance,
                                                     "starting": a.starting_balance} for a in accounts],
                     "mode": "PAPER"})


@router.get("/settings")
async def get_settings_(db: AsyncSession = Depends(get_db), _: str = Depends(get_current_username)) -> dict:
    return {"settings": (await evm_settings.load(db)).to_dict(), "defaults": evm_settings.EvmTradingSettings().to_dict(),
            "note": "amounts are per chain in BNB (BSC) / ETH (Robinhood Chain); percentages come from the shared risk settings"}


@router.put("/settings")
async def put_settings(body: dict, request: Request, db: AsyncSession = Depends(get_db), redis: Redis = Depends(get_redis),
                       username: str = Depends(get_current_username)) -> dict:
    current = await db.get(PlatformSetting, evm_settings.KEY)
    merged = {**(dict(current.value) if current else {}), **body}
    for c in ("bsc", "robinhood"):  # per-chain objects merge key by key
        if isinstance(body.get(c), dict) and current and isinstance(current.value.get(c), dict):
            merged[c] = {**current.value[c], **body[c]}
    s, errors = evm_settings.parse(merged)
    if errors:
        raise HTTPException(422, {"errors": errors})
    value = s.to_dict()
    await db.execute(insert(PlatformSetting).values(key=evm_settings.KEY, value=value).on_conflict_do_update(
        index_elements=["key"], set_={"value": value, "updated_at": func.now()}))
    await audit(db, username, request, "evm_settings.update", {"changes": body})
    await db.commit()
    await events.publish(redis, "settings.updated", {"key": evm_settings.KEY}, "api")
    return {"settings": value}


@router.get("/wallet")
async def wallet(settings: Settings = Depends(get_settings), db: AsyncSession = Depends(get_db),
                 _: str = Depends(get_current_username)) -> dict:
    """The EVM half of the trading wallet: public address, whether a key is
    configured (never the key), native balances read from each chain now,
    and the EVM paper accounts, never mixed with LIVE."""
    acct = evm_wallet.account(settings)
    balances = {}
    if acct.get("address"):
        rpcs = {c: await evm_rpc_registry.rpc_for(db, settings, c) for c in ("bsc", "robinhood")}
        try:
            balances = await evm_wallet.native_balances(acct["address"], rpcs)
        finally:
            for r in rpcs.values():
                await r.aclose()
    papers = (await db.execute(select(PaperAccount).where(PaperAccount.name.like("evm_%")))).scalars().all()
    return jsonable({"live": {**acct, "balances": balances, "currency": {"bsc": "BNB", "robinhood": "ETH"},
                              "execution": "EVM LIVE execution is not implemented; the address is watch-only"},
                     "paper": [{"name": a.name, "currency": a.quote_currency, "cash": a.cash_balance,
                                "starting": a.starting_balance} for a in papers]})


# --- launch-window coordination (master upgrade §11) ------------------------------------------------

@router.get("/coordination-settings")
async def get_coordination_settings(db: AsyncSession = Depends(get_db), _: str = Depends(get_current_username)) -> dict:
    row = await db.get(PlatformSetting, launch_coordination.SETTINGS_KEY)
    cfg, errors = launch_coordination.parse_config(dict(row.value) if row else None)
    return {"settings": cfg.to_dict(), "defaults": launch_coordination.CoordinationConfig().to_dict(), "errors": errors,
            "actions": list(launch_coordination.ACTIONS), "detections": list(launch_coordination.DEFAULT_ACTIONS),
            "note": "applied on the next safety pass (data-evm) and on every EVM copy buy; the strictest action of "
                    "the findings applies"}


@router.put("/coordination-settings")
async def put_coordination_settings(body: dict, request: Request, db: AsyncSession = Depends(get_db),
                                    redis: Redis = Depends(get_redis), username: str = Depends(get_current_username)) -> dict:
    row = await db.get(PlatformSetting, launch_coordination.SETTINGS_KEY)
    current = dict(row.value) if row else {}
    merged = {**current, **body}
    if isinstance(body.get("actions"), dict):  # actions merge detection by detection
        merged["actions"] = {**(current.get("actions") or {}), **body["actions"]}
    cfg, errors = launch_coordination.parse_config(merged)
    if errors:
        raise HTTPException(422, {"errors": errors})
    value = cfg.to_dict()
    await db.execute(insert(PlatformSetting).values(key=launch_coordination.SETTINGS_KEY, value=value).on_conflict_do_update(
        index_elements=["key"], set_={"value": value, "updated_at": func.now()}))
    await audit(db, username, request, "launch_coordination.update", {"changes": body})
    await db.commit()
    await events.publish(redis, "settings.updated", {"key": launch_coordination.SETTINGS_KEY}, "api")
    return {"settings": value}


@router.post("/tokens/{chain}/{token}/coordination-approval")
async def approve_coordination(chain: str, token: str, request: Request, db: AsyncSession = Depends(get_db),
                               username: str = Depends(get_current_username)) -> dict:
    """Operator approval of a MANUAL_APPROVAL assessment. It covers exactly the
    findings shown (fingerprint) until it expires; a NO_TRADE finding is never
    approvable here."""
    row = (await db.execute(select(EvmToken).where(EvmToken.chain == chain,
                                                   func.lower(EvmToken.token) == token.lower()))).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "token not discovered")
    res = row.coordination or {}
    if res.get("action") != launch_coordination.MANUAL:
        raise HTTPException(409, f"nothing to approve: the current assessment's action is {res.get('action') or 'none'}")
    cfg = await launch_coordination.load_config(db)
    now = datetime.now(timezone.utc)
    approval = {"by": username, "at": now.isoformat(), "fingerprint": res.get("fingerprint"),
                "expires_at": (now + timedelta(minutes=cfg.approval_minutes)).isoformat(),
                "findings": [f["code"] for f in res.get("findings", [])]}
    row.extra = {**(row.extra or {}), "coordination_approval": approval}
    await audit(db, username, request, "launch_coordination.approve", {"chain": chain, "token": row.token, **approval})
    await db.commit()
    return {"approval": approval, "note": "paper entries only; a new or changed finding needs a new approval"}


@router.delete("/tokens/{chain}/{token}/coordination-approval")
async def revoke_coordination(chain: str, token: str, request: Request, db: AsyncSession = Depends(get_db),
                              username: str = Depends(get_current_username)) -> dict:
    row = (await db.execute(select(EvmToken).where(EvmToken.chain == chain,
                                                   func.lower(EvmToken.token) == token.lower()))).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "token not discovered")
    extra = dict(row.extra or {})
    had = extra.pop("coordination_approval", None)
    row.extra = extra
    await audit(db, username, request, "launch_coordination.revoke", {"chain": chain, "token": row.token})
    await db.commit()
    return {"revoked": had is not None}


@router.get("/coordination/summary")
async def coordination_summary(chain: str | None = Query(None, pattern=CHAIN), hours: int = Query(24, ge=1, le=720),
                               db: AsyncSession = Depends(get_db), _: str = Depends(get_current_username)) -> dict:
    """What the check found per launchpad over tokens assessed in the window:
    how often each detection fired and which action resulted."""
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    q = select(EvmToken.chain, EvmToken.launchpad, EvmToken.coordination).where(EvmToken.coordination_at >= since)
    if chain:
        q = q.where(EvmToken.chain == chain)
    out: dict[str, dict] = {}
    for c, lp, res in (await db.execute(q)).all():
        k = f"{c}:{lp}"
        o = out.setdefault(k, {"chain": c, "launchpad": lp, "assessed": 0, "status": {}, "action": {}, "detections": {},
                               "unknown_checks": {}})
        o["assessed"] += 1
        res = res or {}
        o["status"][res.get("status")] = o["status"].get(res.get("status"), 0) + 1
        o["action"][res.get("action")] = o["action"].get(res.get("action"), 0) + 1
        for f in res.get("findings", []):
            o["detections"][f["code"]] = o["detections"].get(f["code"], 0) + 1
        for ch in res.get("checks", []):
            if ch.get("status") in (launch_coordination.UNKNOWN, launch_coordination.NOT_CONFIGURED):
                o["unknown_checks"][ch["check"]] = o["unknown_checks"].get(ch["check"], 0) + 1
    return {"hours": hours, "launchpads": sorted(out.values(), key=lambda o: -o["assessed"]),
            "note": "tokens assessed by data-evm's safety pass or a copy buy in the window; counts, not verdicts"}


# --- observation (master upgrade §14-17) ------------------------------------------------------------

def _observation(o: EvmObservation, full: bool = True) -> dict:
    d = {"chain": o.chain, "token": o.token, "category": o.category, "state": o.state, "state_at": o.state_at,
         "reason": o.reason, "started_at": o.started_at, "deadline": o.deadline,
         "observation_reason": o.observation_reason, "expiry_reason": o.expiry_reason, "decided_at": o.decided_at,
         "extensions": o.extensions, "safety_failures": o.safety_failures, "last_decision": o.last_decision,
         "snapshot_labels": sorted(o.snapshots or {}, key=lambda k: (o.snapshots[k] or {}).get("minutes", 0))}
    if full:
        d.update(snapshots=o.snapshots, history=o.history)
    return d


@router.get("/observations")
async def observations(chain: str | None = Query(None, pattern=CHAIN), state: str | None = None,
                       category: str | None = Query(None, pattern="^(FRESH|MIGRATED|MOMENTUM)$"),
                       hours: int = Query(24, ge=1, le=720), limit: int = Query(100, ge=1, le=500),
                       db: AsyncSession = Depends(get_db), _: str = Depends(get_current_username)) -> dict:
    """Observations started in the window, newest first, with counts per
    category and state and, for the expired ones, what held them."""
    if state is not None and state not in evm_observation.STATES:
        raise HTTPException(422, f"state: one of {', '.join(evm_observation.STATES)}")
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    base = [EvmObservation.started_at >= since, *([EvmObservation.chain == chain] if chain else [])]
    q = select(EvmObservation, EvmToken.symbol, EvmToken.launchpad).join(EvmToken, (EvmToken.chain == EvmObservation.chain)
                                                                        & (EvmToken.token == EvmObservation.token)).where(*base)
    if state:
        q = q.where(EvmObservation.state == state)
    if category:
        q = q.where(EvmObservation.category == category)
    rows = (await db.execute(q.order_by(desc(EvmObservation.started_at)).limit(limit))).all()
    counts: dict[str, dict[str, int]] = {}
    for cat, st, n in (await db.execute(select(EvmObservation.category, EvmObservation.state, func.count())
                                        .where(*base).group_by(EvmObservation.category, EvmObservation.state))).all():
        counts.setdefault(cat, {})[st] = n
    held: dict[str, int] = {}
    for (ld,) in (await db.execute(select(EvmObservation.last_decision).where(
            *base, EvmObservation.state == evm_observation.EXPIRED))).all():
        for code in (ld or {}).get("blockers") or ["NEVER_EVALUATED"]:
            held[code] = held.get(code, 0) + 1
    def last_snapshot(o: EvmObservation) -> dict | None:
        snaps = o.snapshots or {}
        return max(snaps.values(), key=lambda v: v.get("minutes", -1)) if snaps else None

    return jsonable({"observations": [{**_observation(o, full=False), "symbol": sym, "launchpad": lp,
                                       "last_snapshot": last_snapshot(o)} for o, sym, lp in rows],
                     "counts": counts, "expired_held_by": dict(sorted(held.items(), key=lambda kv: -kv[1])),
                     "states": list(evm_observation.STATES), "hours": hours,
                     "note": "every discovered token is observed per category before it can be traded; observations "
                             "are kept (never deleted) as training data"})


@router.get("/observation-settings")
async def get_observation_settings(db: AsyncSession = Depends(get_db), _: str = Depends(get_current_username)) -> dict:
    row = await db.get(PlatformSetting, evm_observation.SETTINGS_KEY)
    cfg, errors = evm_observation.parse_config(dict(row.value) if row else None)
    return {"settings": cfg.to_dict(), "defaults": evm_observation.ObservationConfig().to_dict(), "errors": errors,
            "note": "snapshot times and windows apply to observations opened after the change"}


@router.put("/observation-settings")
async def put_observation_settings(body: dict, request: Request, db: AsyncSession = Depends(get_db),
                                   redis: Redis = Depends(get_redis), username: str = Depends(get_current_username)) -> dict:
    row = await db.get(PlatformSetting, evm_observation.SETTINGS_KEY)
    current = dict(row.value) if row else {}
    merged = {**current, **body}
    if isinstance(body.get("window_min"), dict):
        merged["window_min"] = {**(current.get("window_min") or {}), **body["window_min"]}
    cfg, errors = evm_observation.parse_config(merged)
    if errors:
        raise HTTPException(422, {"errors": errors})
    value = cfg.to_dict()
    await db.execute(insert(PlatformSetting).values(key=evm_observation.SETTINGS_KEY, value=value).on_conflict_do_update(
        index_elements=["key"], set_={"value": value, "updated_at": func.now()}))
    await audit(db, username, request, "evm_observation.update", {"changes": body})
    await db.commit()
    await events.publish(redis, "settings.updated", {"key": evm_observation.SETTINGS_KEY}, "api")
    return {"settings": value}


# --- transaction streams (master §9, §13) ------------------------------------------------------------

def _pct(vals: list[float], q: float) -> float | None:
    if not vals:
        return None
    vals = sorted(vals)
    return vals[min(len(vals) - 1, int(len(vals) * q))]


@router.get("/streams")
async def get_streams(db: AsyncSession = Depends(get_db), redis: Redis = Depends(get_redis),
                      _: str = Depends(get_current_username)) -> dict:
    """Live state of the Robinhood sequencer feed and the BSC pending-transaction
    stream as data-evm last published it, plus, over the last 24 hours, how many
    copy-target trades a stream saw before confirmed-trade detection and by how much."""
    row = await db.get(PlatformSetting, evm_streams.SETTINGS_KEY)
    cfg, errors = evm_streams.parse_config(dict(row.value) if row else None)
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    chains: dict[str, dict] = {}
    for chain in ("robinhood", "bsc"):
        reps = await evm_streams.reports(redis, chain)
        lat = (await db.execute(select(CopyEvent.latency_ms).where(CopyEvent.chain == chain, CopyEvent.detected_at >= since)
                                .order_by(desc(CopyEvent.detected_at)).limit(5000))).scalars().all()
        leads = [float(x["stream_lead"]) for x in lat if isinstance(x, dict) and x.get("stream_lead") is not None]
        by_source: dict[str, int] = {}
        for x in lat:
            if isinstance(x, dict) and x.get("stream_source"):
                by_source[x["stream_source"]] = by_source.get(x["stream_source"], 0) + 1
        chains[chain] = {
            "streams": reps,
            "expected": ["sequencer_feed"] if chain == "robinhood" else ["pending_tx"],
            "copy_events_24h": len(lat),
            "seen_on_stream_24h": len(leads),
            "by_source_24h": by_source,
            "lead_ms_median": _pct(leads, 0.5), "lead_ms_p95": _pct(leads, 0.95) if len(leads) >= 20 else None,
            # master §68: the streams and the launchpad logs checked against each other
            "crosscheck": await evm_crosscheck.report(redis, chain, datetime.now(timezone.utc)),
        }
    return jsonable({"settings": cfg.to_dict(), "defaults": evm_streams.StreamConfig().to_dict(), "errors": errors,
                     "chains": chains,
                     "note": "streams record and measure only: a sequenced or pending transaction can still revert, so "
                             "copy decisions stay on confirmed trades. A stream absent here is not running or has not "
                             "reported in 3 minutes. Lead = confirmed-trade detection time minus the time a stream saw "
                             "the same transaction."})


@router.get("/detection")
async def get_detection(db: AsyncSession = Depends(get_db), redis: Redis = Depends(get_redis),
                        _: str = Depends(get_current_username)) -> dict:
    """Master §68-70 per chain: the detection methods and their state (event
    logs per launchpad with the cursor's last advance, the stream that
    sights transactions early), and the ranges the live scan skipped with
    their backfill. Missing data never passes a check: an entry without
    fresh data is NO_TRADE."""
    now = datetime.now(timezone.utc)
    cursors = (await db.execute(select(EvmCursor))).scalars().all()
    g = EvmScanGap
    sums = (await db.execute(select(g.chain, g.launchpad, g.status, func.count(), func.sum(g.to_block - g.from_block + 1),
                                    func.sum(g.next_block - g.from_block), func.sum(g.launches), func.sum(g.trades))
                             .group_by(g.chain, g.launchpad, g.status))).all()
    recent = (await db.execute(select(g).order_by(desc(g.id)).limit(20))).scalars().all()
    chains: dict[str, dict] = {}
    for chain, stream in (("bsc", "pending_tx"), ("robinhood", "sequencer_feed")):
        reps = await evm_streams.reports(redis, chain)
        logs = [{"launchpad": c.launchpad, "last_block": c.last_block, "advanced_at": c.updated_at,
                 "idle_s": round((now - c.updated_at).total_seconds())} for c in cursors if c.chain == chain]
        gaps: dict[str, dict] = {}
        for ch, lp, status, n, blocks, done_blocks, launches, trades in sums:
            if ch != chain:
                continue
            d = gaps.setdefault(lp, {})
            d[status] = {"ranges": n, "blocks": int(blocks or 0), "blocks_backfilled": int(done_blocks or 0),
                         "launches_recovered": int(launches or 0), "trades_recovered": int(trades or 0)}
        chains[chain] = {
            "methods": [
                {"method": "launchpad event logs (eth_getLogs, every adapter)", "role": "primary: launches, trades, "
                 "migrations", "launchpads": sorted(logs, key=lambda x: x["launchpad"])},
                {"method": "Robinhood sequencer feed" if chain == "robinhood" else "BSC pending transactions (WSS)",
                 "role": "early sighting and cross-check only, never a trading trigger",
                 "state": (reps.get(stream) or {}).get("state") or "NOT_RUNNING"},
                {"method": "skipped-range backfill", "role": "recovers history the live scan skipped (lag over "
                 "max_lag_minutes); never opens an entry", "gaps": gaps},
            ],
        }
    return jsonable({"chains": chains,
                     "recent_gaps": [{"id": x.id, "chain": x.chain, "launchpad": x.launchpad, "from": x.from_block,
                                      "to": x.to_block, "next": x.next_block, "status": x.status,
                                      "detected_at": x.detected_at, "completed_at": x.completed_at,
                                      "launches": x.launches, "trades": x.trades, "attempts": x.attempts,
                                      "last_error": x.last_error, "reason": x.reason} for x in recent],
                     "fallback": "provider failure: the next endpoint for the role, then the other endpoints; with "
                                 "none answering, data is unavailable and entries are NO_TRADE (never assumed safe)"})


@router.put("/stream-settings")
async def put_stream_settings(body: dict, request: Request, db: AsyncSession = Depends(get_db),
                              redis: Redis = Depends(get_redis), username: str = Depends(get_current_username)) -> dict:
    row = await db.get(PlatformSetting, evm_streams.SETTINGS_KEY)
    current = dict(row.value) if row else {}
    cfg, errors = evm_streams.parse_config({**current, **body})
    if errors:
        raise HTTPException(422, {"errors": errors})
    value = cfg.to_dict()
    await db.execute(insert(PlatformSetting).values(key=evm_streams.SETTINGS_KEY, value=value).on_conflict_do_update(
        index_elements=["key"], set_={"value": value, "updated_at": func.now()}))
    await audit(db, username, request, "evm_streams.update", {"changes": body})
    await db.commit()
    await events.publish(redis, "settings.updated", {"key": evm_streams.SETTINGS_KEY}, "api")
    return {"settings": value, "note": "data-evm picks the change up within a minute"}
