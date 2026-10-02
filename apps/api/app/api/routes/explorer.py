"""Token Explorer for Solana, BSC and Robinhood Chain (master §54-55).

Search by token name / symbol (prefix), mint / contract address, creator
or wallet. Every result carries the explorer actions of its own chain
(yonixalpha_core.explorer_links): a Solana link is never built for an EVM
token. The EVM token view gathers price, USD market cap, liquidity,
volume, buyers / sellers, safety, launchpad, migration, status, smart
money, manipulation (launch-window coordination), ML and current positions;
a value that is not tracked is None with the reason, never 0. A Solana
token opens the Solana token terminal, which shows the same fields."""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from redis.asyncio import Redis
from sqlalchemy import desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_username, get_db, get_redis
from app.api.util import jsonable
from yonixalpha_core import explorer_links, position_pnl
from yonixalpha_core.chains.evm import native_price, token_view
from yonixalpha_core.chains.registry import LAUNCHPADS
from yonixalpha_core.db.models import CopyTarget, EvmToken, EvmTrade, PaperPosition, Token, WalletProfile

router = APIRouter(prefix="/explorer", tags=["explorer"])
CHAIN = "^(solana|bsc|robinhood)$"
EVM_CHAINS = ("bsc", "robinhood")
PER_KIND = 20


def _evm_forms(address: str) -> list[str]:
    """An EVM address as stored (checksummed) and as typed."""
    from eth_utils import to_checksum_address

    return list(dict.fromkeys([address, address.lower(), to_checksum_address(address)]))


def _prefix(q: str) -> str:
    """A LIKE prefix pattern; %, _ and the escape character match literally."""
    return "".join("\\" + c if c in "\\%_" else c for c in q.lower()) + "%"


def _evm_token_result(t: EvmToken, match: str) -> dict:
    return {"kind": "token", "chain": t.chain, "address": t.token, "symbol": t.symbol, "name": t.name,
            "launchpad": t.launchpad, "launchpad_name": LAUNCHPADS[t.launchpad].name if t.launchpad in LAUNCHPADS else None,
            "creator": t.creator, "category": t.category, "stage": t.stage, "match": match,
            "last_activity": t.last_trade_at or t.created_at,
            **explorer_links.links(t.chain, t.token, t.launchpad, creator=t.creator)}


def _sol_token_result(t: Token, match: str) -> dict:
    return {"kind": "token", "chain": "solana", "address": t.mint_address, "symbol": t.symbol, "name": t.name,
            "launchpad": "pumpfun", "launchpad_name": "Pump.fun", "creator": t.creator_address, "match": match,
            "last_activity": t.last_event_at or t.first_seen_at,
            **explorer_links.links("solana", t.mint_address, "pumpfun", creator=t.creator_address)}


@router.get("/search")
async def search(q: str = Query(min_length=2, max_length=64), chain: str | None = Query(None, pattern=CHAIN),
                 db: AsyncSession = Depends(get_db), _: str = Depends(get_current_username)) -> dict:
    q = q.strip()
    chains = [chain] if chain else ["solana", *EVM_CHAINS]
    evm = [c for c in chains if c in EVM_CHAINS]
    results: list[dict] = []
    if explorer_links.EVM_ADDRESS.match(q):
        forms = _evm_forms(q)
        if evm:
            for t in (await db.execute(select(EvmToken).where(EvmToken.chain.in_(evm), EvmToken.token.in_(forms))))\
                    .scalars():
                results.append(_evm_token_result(t, "contract"))
            for t in (await db.execute(select(EvmToken).where(EvmToken.chain.in_(evm), EvmToken.creator.in_(forms))
                                       .order_by(desc(EvmToken.created_at)).limit(PER_KIND))).scalars():
                results.append(_evm_token_result(t, "creator"))
            results += await _wallet_results(db, evm, forms)
        kind = "EVM address"
    elif explorer_links.SOLANA_ADDRESS.match(q):
        if "solana" in chains:
            for t in (await db.execute(select(Token).where(Token.mint_address == q))).scalars():
                results.append(_sol_token_result(t, "mint"))
            for t in (await db.execute(select(Token).where(Token.creator_address == q)
                                       .order_by(desc(Token.first_seen_at)).limit(PER_KIND))).scalars():
                results.append(_sol_token_result(t, "creator"))
            results += await _wallet_results(db, ["solana"], [q])
        kind = "Solana address"
    else:
        p = _prefix(q)
        if "solana" in chains:
            rows = (await db.execute(select(Token).where(or_(func.lower(Token.symbol).like(p), func.lower(Token.name).like(p)))
                                     .order_by(desc(func.coalesce(Token.last_event_at, Token.first_seen_at)))
                                     .limit(PER_KIND))).scalars().all()
            results += [_sol_token_result(t, "name / symbol") for t in rows]
        if evm:
            rows = (await db.execute(select(EvmToken).where(EvmToken.chain.in_(evm), or_(
                func.lower(EvmToken.symbol).like(p), func.lower(EvmToken.name).like(p)))
                .order_by(desc(func.coalesce(EvmToken.last_trade_at, EvmToken.created_at))).limit(PER_KIND))).scalars().all()
            results += [_evm_token_result(t, "name / symbol") for t in rows]
        kind = "name / symbol (starts with)"
    return jsonable({"query": q, "interpreted_as": kind, "chains": chains, "results": results,
                     "note": "Addresses match exactly; names and symbols match from the start. A Solana address "
                             "only searches Solana, a 0x address only BSC and Robinhood Chain."})


async def _wallet_results(db: AsyncSession, chains: list[str], forms: list[str]) -> list[dict]:
    out = []
    targets = {(t.chain, t.wallet.lower()) for t in (await db.execute(select(CopyTarget).where(
        CopyTarget.chain.in_(chains), CopyTarget.wallet.in_(forms)))).scalars()}
    profiles = {(p.chain, p.wallet.lower()): p for p in (await db.execute(select(WalletProfile).where(
        WalletProfile.chain.in_(chains), WalletProfile.wallet.in_(forms)))).scalars()}
    trades: dict[str, int] = {}
    evm = [c for c in chains if c in EVM_CHAINS]
    if evm:
        since = datetime.now(timezone.utc) - timedelta(days=14)
        trades = dict((await db.execute(select(EvmTrade.chain, func.count()).where(
            EvmTrade.trader.in_(forms), EvmTrade.at >= since, EvmTrade.chain.in_(evm))
            .group_by(EvmTrade.chain))).all())
    for c in chains:
        key = (c, forms[0].lower())
        prof = profiles.get(key)
        if prof is None and key not in targets and not trades.get(c):
            continue
        out.append({"kind": "wallet", "chain": c, "address": prof.wallet if prof else forms[-1], "match": "wallet",
                    "trades_14d": trades.get(c) if c in EVM_CHAINS else None, "is_copy_target": key in targets,
                    "profile": {"labels": prof.labels, "trades": prof.trades, "tokens": prof.tokens,
                                "stage": ((prof.metrics or {}).get("discovery") or {}).get("stage"),
                                "stale": bool((prof.metrics or {}).get("stale")), "updated_at": prof.updated_at}
                    if prof else None,
                    **explorer_links.links(c, wallet=prof.wallet if prof else forms[-1])})
    return out


@router.get("/token/{chain}/{address}")
async def evm_token(chain: str, address: str, db: AsyncSession = Depends(get_db), redis: Redis = Depends(get_redis),
                    _: str = Depends(get_current_username)) -> dict:
    if chain not in EVM_CHAINS or not explorer_links.EVM_ADDRESS.match(address):
        raise HTTPException(422, "use /dashboard/tokens/<mint> for Solana; here chain is bsc or robinhood and a 0x address")
    t = (await db.execute(select(EvmToken).where(EvmToken.chain == chain, EvmToken.token.in_(_evm_forms(address)))))\
        .scalar_one_or_none()
    if t is None:
        raise HTTPException(404, "token not discovered on this chain")
    now = datetime.now(timezone.utc)
    rate = await native_price.usd_rate(redis, chain, now)
    spec = LAUNCHPADS.get(t.launchpad)
    st = t.stats or {}
    retained = (await db.execute(select(
        func.count().filter(EvmTrade.is_buy), func.count().filter(~EvmTrade.is_buy),
        func.count(func.distinct(EvmTrade.trader)).filter(EvmTrade.is_buy),
        func.count(func.distinct(EvmTrade.trader)).filter(~EvmTrade.is_buy)).where(
        EvmTrade.chain == chain, EvmTrade.token == t.token))).one()
    targets = {w.lower() for w, in (await db.execute(select(CopyTarget.wallet).where(CopyTarget.chain == chain))).all()}
    traders = [w for w, in (await db.execute(select(func.distinct(EvmTrade.trader)).where(
        EvmTrade.chain == chain, EvmTrade.token == t.token))).all()]
    traded_by = {w.lower() for w in traders}
    # profiles are keyed by the address as the trades store it (primary key lookup)
    validated = [p.wallet for p in (await db.execute(select(WalletProfile).where(
        WalletProfile.chain == chain, WalletProfile.wallet.in_(traders[:5000]),
        WalletProfile.metrics["discovery"]["stage"].astext.in_(("VALIDATED", "PAPER_FOLLOWED"))))).scalars()] \
        if traders else []
    positions = (await db.execute(select(PaperPosition).where(
        PaperPosition.engine == f"evm_{chain}", func.lower(PaperPosition.asset_id) == t.token.lower())
        .order_by(desc(PaperPosition.entry_at)).limit(20))).scalars().all()
    safety = t.safety or {}

    return jsonable({
        "chain": chain, "address": t.token, "symbol": t.symbol, "name": t.name, "creator": t.creator,
        "created_at": t.created_at, "last_trade_at": t.last_trade_at,
        "launchpad": {"key": t.launchpad, "name": spec.name if spec else t.launchpad,
                      "observe_only": bool(spec and not spec.supports_trading)},
        "market": token_view.market(chain, t.state, t.extra, t.quote_token, rate["price"]),
        "native_usd": rate,
        "volume": {"window_s": st.get("window_s"), "buy_volume": st.get("buy_volume"), "sell_volume": st.get("sell_volume"),
                   "trades": st.get("trades"), "currency": token_view.market(chain, None, None, None, None)["currency"],
                   "note": "native-coin volume over the stats window; a curve quoted in another token has no BNB volume"},
        "buyers": {"window": st.get("unique_buyers"), "retained_14d": retained[2], "buys_14d": retained[0]},
        "sellers": {"window": st.get("unique_sellers"), "retained_14d": retained[3], "sells_14d": retained[1]},
        "holders": {"count": None, "reason": "holder counts are not tracked on EVM chains (no holder index is read)"},
        "safety": {"verdict": t.safety_verdict, "at": t.safety_at, "findings": safety.get("findings") or [],
                   "sellable": safety.get("sellable"), "round_trip": safety.get("round_trip")},
        "migration": {"stage": t.stage, "migrated_at": t.migrated_at, "detail": t.migration},
        "status": {"category": t.category, "stage": t.stage, "entry_decision": (t.extra or {}).get("entry_decision"),
                   "manual_decision": (t.extra or {}).get("manual_decision")},
        "smart_money": {"copy_targets_traded": sorted(w for w in traded_by if w in targets),
                        "validated_wallets_traded": validated[:50],
                        "note": "wallets YonixAlpha follows or validated that traded this token (14-day trade history)"},
        "manipulation": {"coordination": t.coordination, "coordination_at": t.coordination_at,
                         "note": "launch-window coordination (bundled / creator-linked / common-funder buyers)"},
        "ml": {"status": "NOT_AVAILABLE", "reason": "ML models run on Solana only so far; EVM features are part of M12"},
        "positions": [{"id": p.id, "status": p.status, "mode": p.execution_mode, "engine": p.engine,
                       "entry_at": p.entry_at, "exit_at": p.exit_at, "exit_reason": p.exit_reason,
                       "exit_requested": bool(p.exit_requested), "source": (p.plan or {}).get("entry_source"),
                       "pnl": position_pnl.view(p, now)} for p in positions],
        **explorer_links.links(chain, t.token, t.launchpad, creator=t.creator, tx=t.created_tx),
    })


@router.get("/links/{chain}")
async def links(chain: str, token: str | None = None, launchpad: str | None = None, creator: str | None = None,
                tx: str | None = None, wallet: str | None = None, _: str = Depends(get_current_username)) -> dict:
    if chain not in explorer_links.EXPLORER:
        raise HTTPException(422, "chain must be solana, bsc or robinhood")
    return explorer_links.links(chain, token, launchpad, creator=creator, tx=tx, wallet=wallet)
