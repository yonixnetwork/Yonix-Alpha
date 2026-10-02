"""Copy trading (paper) and wallet profiles.

Wallet profiles are sorted by the metric the operator picks; there is no
"best wallet" label and no overall ranking field. Copy targets are
created, changed and removed by the operator only (audited); a target's
trade is a candidate that still passes every gate and safety check.
"""

import re

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from sqlalchemy import desc, func, nulls_last, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_username, get_db, get_redis, get_settings
from app.api.util import audit, jsonable
from yonixalpha_core import copy_outcomes as co
from yonixalpha_core import copy_trading as ct
from yonixalpha_core import events, wallet_validation
from yonixalpha_core.chains.evm import address_kinds
from yonixalpha_core.config import Settings
from yonixalpha_core.db.models import CopyEvent, CopyPosition, CopyTarget, PaperPosition, PlatformSetting, WalletProfile

router = APIRouter(tags=["copy"])
CHAIN = "^(solana|bsc|robinhood)$"
EVM_ADDR = re.compile(r"^0x[0-9a-fA-F]{40}$")
SOL_ADDR = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
SORTS = {"last_seen": WalletProfile.last_seen, "trades": WalletProfile.trades, "tokens": WalletProfile.tokens,
         "score": WalletProfile.score}


class TargetIn(BaseModel):
    chain: str = Field(..., pattern=CHAIN)
    wallet: str = Field(..., min_length=32, max_length=64)
    label: str | None = Field(default=None, max_length=64)
    mode: str = Field(default="NOTIFY", pattern="^(NOTIFY|BUY_ONLY|MIRROR|SELL_ONLY)$")
    enabled: bool = True
    settings: dict = Field(default_factory=dict)


class TargetPatch(BaseModel):
    label: str | None = Field(default=None, max_length=64)
    mode: str | None = Field(default=None, pattern="^(NOTIFY|BUY_ONLY|MIRROR|SELL_ONLY)$")
    enabled: bool | None = None
    settings: dict | None = None


def _valid_wallet(chain: str, wallet: str) -> str:
    if chain == "solana":
        if not SOL_ADDR.match(wallet):
            raise HTTPException(422, "not a Solana address")
        return wallet
    if not EVM_ADDR.match(wallet):
        raise HTTPException(422, "not an EVM address")
    return wallet


def _target(t: CopyTarget, stats: dict | None = None) -> dict:
    s, _ = ct.parse_settings(t.settings)
    return {"id": t.id, "chain": t.chain, "wallet": t.wallet, "label": t.label, "mode": t.mode, "enabled": t.enabled,
            "settings": s.to_dict(), "created_by": t.created_by, "created_at": t.created_at, "updated_at": t.updated_at,
            **({"stats": stats} if stats is not None else {})}


@router.get("/wallets/profiles")
async def profiles(chain: str | None = Query(None, pattern=CHAIN), label: str | None = None,
                   stage: str | None = Query(None, pattern="^(COLLECTING_HISTORY|VALIDATED|PAPER_FOLLOWED|REJECTED)$"),
                   sort: str = Query("last_seen", pattern="^(last_seen|trades|tokens|score)$"),
                   min_trades: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500),
                   include_contracts: bool = False,
                   db: AsyncSession = Depends(get_db), _: str = Depends(get_current_username)) -> dict:
    q = select(WalletProfile).where(WalletProfile.trades >= min_trades)
    if not include_contracts:  # routers and bots credited with trades are not wallets (address_kinds)
        q = q.where(func.coalesce(WalletProfile.metrics["account"]["kind"].astext, "") != address_kinds.CONTRACT)
    if chain:
        q = q.where(WalletProfile.chain == chain)
    if label:
        q = q.where(WalletProfile.labels.contains([label.upper()]))
    if stage:
        q = q.where(WalletProfile.metrics["discovery"]["stage"].astext == stage)
    rows = (await db.execute(q.order_by(nulls_last(desc(SORTS[sort]))).limit(limit))).scalars().all()
    targets = {(t.chain, t.wallet.lower()) for t in (await db.execute(select(CopyTarget))).scalars()}
    return jsonable({
        "profiles": [{"chain": r.chain, "wallet": r.wallet, "metrics": r.metrics, "labels": r.labels, "score": r.score,
                      "score_detail": r.score_detail, "source": r.source, "trades": r.trades, "tokens": r.tokens,
                      "first_seen": r.first_seen, "last_seen": r.last_seen, "updated_at": r.updated_at,
                      "is_copy_target": (r.chain, r.wallet.lower()) in targets} for r in rows],
        "sorted_by": sort,
        "note": "profiles describe observed behaviour; they are not a ranking and no wallet is labelled best. "
                "A score needs enough closed trades and is shrunk toward a base rate. Contract addresses (routers, "
                "bots) are hidden unless asked for: the trades credited to them belong to the wallets that call them."})


@router.get("/wallets/validation-settings")
async def get_validation_settings(db: AsyncSession = Depends(get_db), _: str = Depends(get_current_username)) -> dict:
    row = await db.get(PlatformSetting, wallet_validation.SETTINGS_KEY)
    cfg, errors = wallet_validation.parse_config(dict(row.value) if row else None)
    return {"settings": cfg.to_dict(), "defaults": wallet_validation.ValidationConfig().to_dict(), "errors": errors,
            "history_checks": list(wallet_validation.HISTORY_CHECKS),
            "note": "applied on the next profile rebuild (every 10 minutes); a wallet that passes is VALIDATED and "
                    "followed on paper, never copied automatically"}


@router.put("/wallets/validation-settings")
async def put_validation_settings(body: dict, request: Request, db: AsyncSession = Depends(get_db),
                                  username: str = Depends(get_current_username)) -> dict:
    row = await db.get(PlatformSetting, wallet_validation.SETTINGS_KEY)
    merged = {**(dict(row.value) if row else {}), **body}
    cfg, errors = wallet_validation.parse_config(merged)
    if errors:
        raise HTTPException(422, {"errors": errors})
    value = cfg.to_dict()
    if row is None:
        db.add(PlatformSetting(key=wallet_validation.SETTINGS_KEY, value=value))
    else:
        row.value = value
    await audit(db, username, request, "wallet_validation.update", {"changes": body})
    await db.commit()
    return {"settings": value}


@router.get("/copy/targets")
async def targets(db: AsyncSession = Depends(get_db), _: str = Depends(get_current_username)) -> dict:
    rows = (await db.execute(select(CopyTarget).order_by(CopyTarget.created_at))).scalars().all()
    counts = {}
    for tid, decision, n in (await db.execute(select(CopyEvent.target_id, CopyEvent.decision, func.count())
                                              .group_by(CopyEvent.target_id, CopyEvent.decision))).all():
        counts.setdefault(tid, {})[decision] = n
    open_n = dict((await db.execute(select(CopyPosition.target_id, func.count()).join(
        PaperPosition, PaperPosition.id == CopyPosition.position_id).where(PaperPosition.status == "open")
        .group_by(CopyPosition.target_id))).all())
    return jsonable({"targets": [_target(t, {"events": counts.get(t.id, {}), "open_positions": open_n.get(t.id, 0)})
                                 for t in rows],
                     "modes": list(ct.MODES), "defaults": ct.CopySettings().to_dict(),
                     "note": "paper only; every copied buy still passes the gate / safety checks and the COPY TRADING switch"})


async def _evm_account_kind(db: AsyncSession, settings: Settings, chain: str, wallet: str) -> tuple[str | None, str | None]:
    """(kind, warning). A contract cannot sign: the trades credited to it are the
    trades of whoever called it (a router or bot), so it is never a copy target."""
    from datetime import datetime, timezone

    from yonixalpha_core.chains.evm import rpc_registry as evm_registry

    try:
        rpc = await evm_registry.rpc_for(db, settings, chain)
        try:
            kinds = await address_kinds.resolve(db, rpc, chain, [wallet], datetime.now(timezone.utc), max_lookups=1)
        finally:
            await rpc.aclose()
    except Exception as exc:  # noqa: BLE001 - the check is advisory when the RPC is down
        return None, f"could not check whether this address is a contract ({type(exc).__name__}); added anyway"
    kind = kinds.get(wallet.lower())
    return kind, None if kind else "could not check whether this address is a contract; added anyway"


@router.post("/copy/targets")
async def add_target(body: TargetIn, request: Request, db: AsyncSession = Depends(get_db),
                     redis: Redis = Depends(get_redis), settings: Settings = Depends(get_settings),
                     username: str = Depends(get_current_username)) -> dict:
    wallet = _valid_wallet(body.chain, body.wallet)
    s, errors = ct.parse_settings(body.settings)
    if errors:
        raise HTTPException(422, {"errors": errors})
    kind, warning = (None, None)
    if body.chain in ("bsc", "robinhood"):
        kind, warning = await _evm_account_kind(db, settings, body.chain, wallet)
        if kind == address_kinds.CONTRACT:
            await db.commit()  # keeps the classification (nothing else has been written yet)
            raise HTTPException(422, "this address is a contract (a router or bot), not a wallet: the trades credited "
                                     "to it belong to the wallets that call it. Copy one of those wallets instead.")
    exists = (await db.execute(select(CopyTarget).where(CopyTarget.chain == body.chain,
                                                        func.lower(CopyTarget.wallet) == wallet.lower()))).scalar_one_or_none()
    if exists:
        raise HTTPException(409, "this wallet is already a copy target on this chain")
    t = CopyTarget(chain=body.chain, wallet=wallet, label=body.label, mode=body.mode, enabled=body.enabled,
                   settings=s.to_dict(), created_by=username)
    db.add(t)
    await db.flush()
    await audit(db, username, request, "copy_target.create", {"chain": t.chain, "wallet": wallet, "mode": t.mode})
    await db.commit()
    await events.publish(redis, "copy.targets.updated", {"id": str(t.id)}, "api")
    return jsonable({**_target(t), "account_kind": kind, "warning": warning})


@router.patch("/copy/targets/{target_id}")
async def patch_target(target_id: str, body: TargetPatch, request: Request, db: AsyncSession = Depends(get_db),
                       redis: Redis = Depends(get_redis), username: str = Depends(get_current_username)) -> dict:
    t = await db.get(CopyTarget, target_id)
    if t is None:
        raise HTTPException(404, "unknown copy target")
    changes = body.model_dump(exclude_none=True)
    if "settings" in changes:
        s, errors = ct.parse_settings({**(t.settings or {}), **changes["settings"]})
        if errors:
            raise HTTPException(422, {"errors": errors})
        t.settings = s.to_dict()
    for k in ("label", "mode", "enabled"):
        if k in changes:
            setattr(t, k, changes[k])
    t.updated_at = func.now()
    await audit(db, username, request, "copy_target.update", {"id": target_id, **changes})
    await db.commit()
    await db.refresh(t)
    await events.publish(redis, "copy.targets.updated", {"id": target_id}, "api")
    return jsonable(_target(t))


@router.delete("/copy/targets/{target_id}")
async def delete_target(target_id: str, request: Request, db: AsyncSession = Depends(get_db),
                        redis: Redis = Depends(get_redis), username: str = Depends(get_current_username)) -> dict:
    """Removes the target and its event history. Open copy positions are NOT
    closed by this: they keep their stop / take-profit management."""
    t = await db.get(CopyTarget, target_id)
    if t is None:
        raise HTTPException(404, "unknown copy target")
    await audit(db, username, request, "copy_target.delete", {"id": target_id, "chain": t.chain, "wallet": t.wallet})
    await db.delete(t)
    await db.commit()
    await events.publish(redis, "copy.targets.updated", {"id": target_id, "deleted": True}, "api")
    return {"deleted": target_id}


@router.get("/copy/events")
async def copy_events(target_id: str | None = None, chain: str | None = Query(None, pattern=CHAIN),
                      decision: str | None = None, limit: int = Query(200, ge=1, le=1000),
                      db: AsyncSession = Depends(get_db), _: str = Depends(get_current_username)) -> dict:
    q = select(CopyEvent)
    if target_id:
        q = q.where(CopyEvent.target_id == target_id)
    if chain:
        q = q.where(CopyEvent.chain == chain)
    if decision:
        q = q.where(CopyEvent.decision == decision.upper())
    rows = (await db.execute(q.order_by(desc(CopyEvent.detected_at)).limit(limit))).scalars().all()
    lat = [r.latency_ms for r in rows if r.latency_ms and r.decision == "COPIED"]

    def med(key: str):
        vals = sorted(x[key] for x in lat if isinstance(x.get(key), int))
        return vals[len(vals) // 2] if vals else None

    return jsonable({"events": [{"id": r.id, "target_id": r.target_id, "chain": r.chain, "wallet": r.wallet, "token": r.token,
                                 "side": r.side, "decision": r.decision, "reason": r.reason, "target_at": r.target_at,
                                 "detected_at": r.detected_at, "decided_at": r.decided_at, "latency_ms": r.latency_ms,
                                 "position_id": r.position_id, "detail": r.detail, "outcome": r.outcome,
                                 "target_quote_amount": str(r.target_quote_amount / 10 ** ct.DECIMALS_QUOTE.get(r.chain, 18))}
                                for r in rows],
                     "median_latency_ms_copied": {k: med(k) for k in ("detection", "analysis", "risk", "decision",
                                                                      "execution", "total")},
                     "live_only_stages": list(ct.LIVE_ONLY_STAGES),
                     "latency_note": "build / sign / submission / landing / confirmation exist only for a signed "
                                     "transaction; copy trading is paper only"})


@router.get("/copy/positions")
async def copy_positions(status: str = Query("open", pattern="^(open|closed)$"), limit: int = Query(200, ge=1, le=500),
                         db: AsyncSession = Depends(get_db), _: str = Depends(get_current_username)) -> dict:
    rows = (await db.execute(select(PaperPosition, CopyPosition, CopyTarget).join(
        CopyPosition, CopyPosition.position_id == PaperPosition.id).join(CopyTarget, CopyTarget.id == CopyPosition.target_id)
        .where(PaperPosition.status == status).order_by(desc(PaperPosition.entry_at)).limit(limit))).all()
    pids = [p.id for p, _, _ in rows]
    entries = {e.position_id: e for e in (await db.execute(select(CopyEvent).where(
        CopyEvent.position_id.in_(pids), CopyEvent.side == "BUY", CopyEvent.decision == "COPIED"))).scalars()} if pids else {}
    sells: dict = {}
    if rows:
        for e in (await db.execute(select(CopyEvent).where(
                CopyEvent.target_id.in_({cp.target_id for _, cp, _ in rows}), CopyEvent.side == "SELL",
                CopyEvent.token.in_({cp.token for _, cp, _ in rows})))).scalars():
            sells.setdefault((e.target_id, e.token), []).append(e)
    out = []
    for p, cp, t in rows:
        entry = entries.get(p.id)
        after = entry.target_at if entry is not None else p.entry_at
        link = co.link(chain=cp.chain, wallet=t.wallet, mode=t.mode, entry_event=entry,
                       sells=[e for e in sells.get((cp.target_id, cp.token), []) if e.target_at >= after],
                       position=p, target_tokens_held=cp.target_tokens)
        rem = p.remaining_quantity if p.remaining_quantity is not None else p.quantity
        init = p.initial_quantity or p.quantity
        cost_open = (p.entry_cost_quote or 0) * (rem / init) if init else 0
        out.append({"id": p.id, "chain": cp.chain, "token": cp.token, "symbol": p.symbol, "target": t.wallet,
                    "target_label": t.label, "mode": t.mode, "status": p.status, "entry_at": p.entry_at,
                    "entry_cost": p.entry_cost_quote, "quantity": rem, "last_price": p.last_price, "stop_loss": p.stop_loss,
                    "unrealized_pnl": ((p.last_price or p.entry_price) * rem - cost_open) if p.status == "open" else None,
                    "realized_pnl": p.realized_pnl, "exit_reason": p.exit_reason, "currency": ct.NATIVE.get(cp.chain),
                    "link": link})
    return jsonable({"positions": out, "mode": "PAPER"})


@router.get("/copy/outcomes")
async def copy_outcomes(chain: str | None = Query(None, pattern=CHAIN), target_id: str | None = None,
                        limit: int = Query(100, ge=1, le=500),
                        db: AsyncSession = Depends(get_db), _: str = Depends(get_current_username)) -> dict:
    """Paper outcome of every evaluated target buy, grouped by target and by
    what we did with it (copied / missed / blocked by safety / filtered by
    settings / not copyable / notify only). Counts with no evaluated event
    show None, never 0 %."""
    q = select(CopyEvent, CopyTarget).join(CopyTarget, CopyTarget.id == CopyEvent.target_id).where(
        CopyEvent.side == "BUY", CopyEvent.outcome_at.is_not(None))
    if chain:
        q = q.where(CopyEvent.chain == chain)
    if target_id:
        q = q.where(CopyEvent.target_id == target_id)
    rows = (await db.execute(q.order_by(desc(CopyEvent.outcome_at)).limit(5000))).all()
    groups: dict = {}
    for ev, t in rows:
        o = ev.outcome or {}
        g = groups.setdefault((t.id, o.get("class") or "UNKNOWN"), {
            "target_id": t.id, "chain": t.chain, "wallet": t.wallet, "label": t.label, "mode": t.mode,
            "class": o.get("class") or "UNKNOWN", "events": 0, "no_price_data": 0, "won": 0, "lost": 0, "results": []})
        g["events"] += 1
        if o.get("status") != "EVALUATED":
            g["no_price_data"] += 1
            continue
        g["won" if o.get("label") == "WOULD_HAVE_WON" else "lost"] += 1
        g["results"].append(float(o["result_pct"]))
    summary = []
    for g in groups.values():
        r = sorted(g.pop("results"))
        n = len(r)
        summary.append({**g, "evaluated": n, "won_rate": round(g["won"] / n, 4) if n else None,
                        "avg_result_pct": round(sum(r) / n, 2) if n else None,
                        "median_result_pct": round(r[n // 2] if n % 2 else (r[n // 2 - 1] + r[n // 2]) / 2, 2) if n else None})
    summary.sort(key=lambda g: (g["wallet"], co.CLASSES.index(g["class"]) if g["class"] in co.CLASSES else 99))
    recent = [{"id": ev.id, "chain": ev.chain, "wallet": ev.wallet, "token": ev.token, "target_at": ev.target_at,
               "decision": ev.decision, "reason": ev.reason, "outcome": ev.outcome} for ev, _ in rows[:limit]]
    return jsonable({"summary": summary, "recent": recent, "horizon_min": co.HORIZON.total_seconds() / 60,
                     "classes": list(co.CLASSES), "basis": co.BASIS,
                     "note": "what each target buy would have done in paper, copied or not: a measure of what each "
                             "filter cost or saved; it never changes a decision"})
