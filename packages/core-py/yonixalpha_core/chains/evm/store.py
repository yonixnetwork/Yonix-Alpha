"""Persistence for EVM discovery: tokens, trades, scan cursors, and the
rolling per-token stats / category.

Idempotency: a trade's primary key is chain:tx_hash:log_index and is
inserted with ON CONFLICT DO NOTHING; the cursor is advanced in the same
transaction as the rows of its range. A restart therefore resumes at the
next unprocessed block and never stores (or acts on) an event twice.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, exists, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core.chains import activity
from yonixalpha_core.chains.base import TokenCategory
from yonixalpha_core.chains.evm.launchpad import ScanResult
from yonixalpha_core.chains.evm.settings import EvmTradingSettings
from yonixalpha_core.chains.evm.abi import ZERO_ADDRESS
from yonixalpha_core.chains.registry import BSC_WBNB, ROBINHOOD_WETH
from yonixalpha_core.db.models import EvmCursor, EvmToken, EvmTrade

# Quote assets measured in the chain's native unit (wrapped native is 1:1).
NATIVE_QUOTES = tuple(a.lower() for a in (ZERO_ADDRESS, BSC_WBNB, ROBINHOOD_WETH))


def native_quote_trade(e=EvmTrade):
    """SQL condition: the trade's quote_amount is in the chain's native unit,
    or nothing says otherwise. A trade flagged native_quote = false, or on a
    token whose recorded quote is an ERC-20 (Four.meme / Genius.fun curves
    quoted in tokenized stocks), is excluded from native-unit sums: wallet
    profit and loss, market regimes, volume."""
    other_quote = exists().where(EvmToken.chain == e.chain, EvmToken.token == e.token,
                                 EvmToken.quote_token.is_not(None),
                                 func.lower(EvmToken.quote_token).not_in(NATIVE_QUOTES))
    return and_(func.coalesce(e.extra["native_quote"].astext, "true") != "false", ~other_quote)

VENUE_KEYS = ("curve", "pool", "factory")


def _js(v: Any) -> Any:
    if isinstance(v, dict):
        return {str(k): _js(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_js(x) for x in v]
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, int) and not isinstance(v, bool) and abs(v) >= 2 ** 53:
        return str(v)
    return v


async def get_cursor(session: AsyncSession, chain: str, launchpad: str) -> int | None:
    row = await session.get(EvmCursor, (chain, launchpad))
    return row.last_block if row else None


async def set_cursor(session: AsyncSession, chain: str, launchpad: str, block: int, now: datetime) -> None:
    stmt = insert(EvmCursor).values(chain=chain, launchpad=launchpad, last_block=block, updated_at=now)
    await session.execute(stmt.on_conflict_do_update(index_elements=["chain", "launchpad"],
                                                     set_={"last_block": block, "updated_at": now}))


async def _ensure_token(session: AsyncSession, chain: str, launchpad: str, token: str, now: datetime) -> None:
    """A token seen trading whose launch this system did not observe (it
    predates the scan): stored with launch_seen = false, never FRESH."""
    await session.execute(insert(EvmToken).values(
        chain=chain, token=token, launchpad=launchpad, created_at=now, venue={}, stats={},
        extra={"launch_seen": False}, category=TokenCategory.OTHER.value, stage="UNKNOWN",
    ).on_conflict_do_nothing(index_elements=["chain", "token"]))


async def persist_scan(session: AsyncSession, adapter, res: ScanResult, now: datetime) -> dict[str, int]:
    """Stores one scan's launches, trades, migrations and settings events and
    advances the cursor to res.to_block. Caller commits."""
    from yonixalpha_core.chains.evm import observation

    chain, key = adapter.spec.chain.value, adapter.spec.key
    counts = {"launches": 0, "trades": 0, "migrations": 0}
    acc = activity.Accumulator()
    ocfg = await observation.load_config(session) if res.launches else None
    for ln in res.launches:
        venue = {k: ln.extra[k] for k in VENUE_KEYS if ln.extra.get(k)}
        stmt = insert(EvmToken).values(
            chain=chain, token=ln.token, launchpad=key, creator=ln.creator, name=(ln.name or None) and ln.name[:128],
            symbol=(ln.symbol or None) and ln.symbol[:64], created_at=ln.created_at, created_block=ln.block,
            created_tx=ln.tx_hash, quote_token=ln.quote_token, venue=_js(venue), stats={},
            extra=_js({"launch_seen": True, **{k: v for k, v in ln.extra.items() if k not in VENUE_KEYS}}),
            category=TokenCategory.FRESH.value, stage="DEX" if venue.get("pool") else "CURVE")
        # A row created earlier from a trade (launch not yet seen) gets its real launch data.
        r = await session.execute(stmt.on_conflict_do_update(
            index_elements=["chain", "token"],
            set_={"creator": stmt.excluded.creator, "name": stmt.excluded.name, "symbol": stmt.excluded.symbol,
                  "created_at": stmt.excluded.created_at, "created_block": stmt.excluded.created_block,
                  "created_tx": stmt.excluded.created_tx, "quote_token": stmt.excluded.quote_token,
                  "venue": stmt.excluded.venue, "extra": stmt.excluded.extra, "stage": stmt.excluded.stage,
                  "category": stmt.excluded.category},
            where=EvmToken.created_block.is_(None)))
        counts["launches"] += r.rowcount or 0
        if r.rowcount:
            acc.launch(ln.created_at)
            await observation.ensure(session, chain, ln.token, "FRESH", ln.created_at, "launch observed", ocfg)
    for t in res.trades:
        await _ensure_token(session, chain, key, t.token, t.at)
        r = await session.execute(insert(EvmTrade).values(
            event_id=t.event_id, chain=chain, launchpad=key, token=t.token, trader=t.trader, is_buy=t.is_buy,
            token_amount=t.token_amount, quote_amount=t.quote_amount, fee=t.fee, block=t.block, tx_hash=t.tx_hash,
            at=t.at, extra=_js(t.extra) or None).on_conflict_do_nothing(index_elements=["event_id"]))
        counts["trades"] += r.rowcount or 0
        if r.rowcount:
            # Volume is in native units: a trade against an ERC-20 pair token
            # (e.g. Genius.fun's tokenized stocks) counts as a trade, not as volume.
            acc.trade(t.at, 0 if (t.extra or {}).get("native_quote") is False else t.quote_amount)
    for m in res.migrations:
        await _ensure_token(session, chain, key, m["token"], m.get("at") or now)
        row = await session.get(EvmToken, (chain, m["token"]))
        if row is not None and row.migrated_at is None:
            row.migrated_at = m.get("at") or now
            row.migration = _js(m)
            row.stage = "DEX"
            if m.get("pool"):
                row.venue = {**(row.venue or {}), "pool": m["pool"]}
            counts["migrations"] += 1
            acc.migration(row.migrated_at)
    for o in res.other:
        tok = o.get("token")
        if not tok:
            continue
        row = await session.get(EvmToken, (chain, tok))
        if row is None:
            continue
        ev = o.get("event")
        extra = dict(row.extra or {})
        if ev == "TokenQuoteSet":
            row.quote_token = o.get("quoteToken")
        elif ev in ("FlapTokenTaxSet", "FlapTokenAsymmetricTaxSet"):
            extra["tax_event"] = {k: v for k, v in o.items() if k != "event"}
        elif ev == "TokenExtensionEnabled":
            extra["extension"] = {k: v for k, v in o.items() if k != "event"}
        else:
            extra.setdefault("events", [])
            extra["events"] = (extra["events"] + [_js(o)])[-10:]
        row.extra = _js(extra)
    await activity.record(session, chain, key, acc, now)
    await set_cursor(session, chain, key, res.to_block, now)
    return counts


async def restore_adapter(session: AsyncSession, adapter) -> int:
    """Re-registers the curves / pools / factories a launchpad announced
    earlier, so a restarted service keeps accepting their events."""
    rows = (await session.execute(select(EvmToken.token, EvmToken.venue, EvmToken.quote_token).where(
        EvmToken.chain == adapter.spec.chain.value, EvmToken.launchpad == adapter.spec.key))).all()
    n = 0
    for token, venue, quote in rows:
        venue = venue or {}
        if venue.get("curve") and hasattr(adapter, "register_curve"):
            adapter.register_curve(venue["curve"], token, quote)
            n += 1
        if venue.get("pool") and hasattr(adapter, "register_pool"):
            adapter.register_pool(venue["pool"], token, quote)
            n += 1
        if venue.get("factory") and hasattr(adapter, "token_factory"):
            adapter.token_factory[token.lower()] = venue["factory"]
    return n


# --- stats and category ----------------------------------------------------------------------------

class _Point:
    """Duck-typed like solana.flow.Trade for realized_volatility."""

    def __init__(self, at: datetime, price: Decimal) -> None:
        self.at, self._p = at, price

    def price(self, _decimals: int) -> Decimal:
        return self._p


def exec_price(t: EvmTrade) -> Decimal | None:
    """Quote per token of the trade itself (both sides in 18-decimal units:
    BNB / ETH and launchpad tokens), from the amounts, not from any
    launchpad price field whose scale is not verified."""
    return (Decimal(t.quote_amount) / Decimal(t.token_amount)) if t.token_amount else None


def trade_stats(trades: list[EvmTrade], now: datetime, window_seconds: int) -> dict[str, Any]:
    from yonixalpha_core.solana.flow import realized_volatility

    start = now - timedelta(seconds=window_seconds)
    w = [t for t in trades if t.at >= start]
    buys = [t for t in w if t.is_buy]
    sells = [t for t in w if not t.is_buy]
    bv = sum((Decimal(t.quote_amount) for t in buys), Decimal(0))
    sv = sum((Decimal(t.quote_amount) for t in sells), Decimal(0))
    pts = [_Point(t.at, p) for t in sorted(trades, key=lambda x: x.at) if (p := exec_price(t))]
    vol = realized_volatility(pts, now, 900, 18) if pts else None
    if vol is None and pts:
        fast = realized_volatility(pts, now, 300, 18, 10)
        vol = (fast * Decimal(str(math.sqrt(6)))).quantize(Decimal("0.00000001")) if fast is not None else None
    wp = [p for t in sorted(w, key=lambda x: x.at) if (p := exec_price(t))]
    return {
        "window_s": window_seconds, "trades": len(w), "buys": len(buys), "sells": len(sells),
        "unique_buyers": len({t.trader for t in buys}), "unique_sellers": len({t.trader for t in sells}),
        "buy_volume": str(bv / 10 ** 18), "sell_volume": str(sv / 10 ** 18),
        "net_buy_ratio": str((bv / (bv + sv)).quantize(Decimal("0.0001"))) if bv + sv > 0 else None,
        "first_price": str(wp[0]) if wp else None, "last_price": str(pts[-1].price(18)) if pts else None,
        "price_change": str((wp[-1] / wp[0] - 1).quantize(Decimal("0.0001"))) if len(wp) > 1 and wp[0] > 0 else None,
        "volatility": str(vol) if vol is not None else None,
    }


def categorize(row: EvmToken, stats: dict[str, Any], now: datetime, s: EvmTradingSettings) -> TokenCategory:
    """MIGRATED (migrated recently) > FRESH (launch observed, young, still on
    its curve or first pool) > MOMENTUM (sustained, broad buying) > OTHER."""
    if row.migrated_at is not None and now - row.migrated_at <= timedelta(minutes=s.migrated_window_minutes):
        return TokenCategory.MIGRATED
    launch_seen = bool((row.extra or {}).get("launch_seen"))
    if launch_seen and row.migrated_at is None and now - row.created_at <= timedelta(minutes=s.fresh_max_age_minutes):
        return TokenCategory.FRESH
    ratio = Decimal(stats["net_buy_ratio"]) if stats.get("net_buy_ratio") else Decimal(0)
    if (stats.get("buys", 0) >= s.momentum_min_buys and stats.get("unique_buyers", 0) >= s.momentum_min_unique_buyers
            and ratio >= s.min_net_buy_ratio):
        return TokenCategory.MOMENTUM
    return TokenCategory.OTHER


async def refresh_stats(session: AsyncSession, chain: str, tokens: set[str], now: datetime,
                        s: EvmTradingSettings) -> int:
    """Recomputes stats + category for tokens that traded in this pass."""
    from yonixalpha_core.chains.evm import observation

    since = now - timedelta(minutes=20)
    n = 0
    ocfg = await observation.load_config(session) if tokens else None
    for token in tokens:
        row = await session.get(EvmToken, (chain, token))
        if row is None:
            continue
        trades = (await session.execute(select(EvmTrade).where(
            EvmTrade.chain == chain, EvmTrade.token == token, EvmTrade.at >= since).order_by(EvmTrade.at))).scalars().all()
        st = trade_stats(list(trades), now, s.stats_window_seconds)
        row.stats = st
        if trades:
            row.last_trade_at = max(t.at for t in trades)
        row.category = categorize(row, st, now, s).value
        if row.category == "FRESH":
            await observation.ensure(session, chain, token, "FRESH", row.created_at, "launch observed", ocfg)
        elif row.category == "MIGRATED":
            await observation.ensure(session, chain, token, "MIGRATED", row.migrated_at or now, "migration observed", ocfg)
        elif row.category == "MOMENTUM":
            await observation.ensure(session, chain, token, "MOMENTUM", now, f"momentum: {st.get('buys')} buys, "
                                     f"{st.get('unique_buyers')} buyers in {st.get('window_s')} s", ocfg)
        n += 1
    return n
