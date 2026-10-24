"""Large-buy observation: does a large buy attract other buyers, after which
earlier holders sell into it? (seven-day investigation, Phase 5, 2026-10-24)

This is a hypothesis to test, not a rule: nothing here trades or sells.

Definition (DEFINITION_VERSION; a change gets a new version, never applied
to old rows):
  a BUY on a Pump.fun bonding curve of at least 0.3 SOL that is
    - at least 2 % of the curve's virtual SOL reserves after it (liquidity), and
    - at least 3 x the median buy of the 60 s before it (recent volume),
  with at least MIN_PRE_TRADES trades in those 60 s (a quieter token cannot be
  measured) and at most one event per token per COOLDOWN.

The stream (pump_stream.ingest) notes buys that pass the first two tests;
process() checks the volume test and records the EVENT together with one
CONTROL moment: another active token at the same time, with enough trades
and no large buy in COOLDOWN, measured the same way. Controls let a later
study compare "after a large buy" with "an ordinary moment".

Before (pre, the 60 s up to the event): trades, buys, sells, distinct
buyers and sellers, net SOL flow, the price move, virtual SOL reserves,
token age; whether the large buyer is the token's creator (a related wallet;
other relations are not known and are reported as such).

After (post, at 30 s / 60 s / 5 min / 15 min): price return, net SOL flow,
new buyers (wallets not seen before the moment), sells by earlier holders
(wallets that had bought before the moment: profit-taking), and for EVENT
rows what the large buyer itself sold. Rows whose stored trade history no
longer covers the window are INCOMPLETE, never guessed.

Observation runs whatever the trading mode (engine-solana-discovery); a
failure is logged and never stops the scanner.
"""

from __future__ import annotations

import hashlib
import statistics
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core.db.models import LargeTradeEvent
from yonixalpha_core.solana import pump_stream
from yonixalpha_core.solana.flow import Trade

DEFINITION_VERSION = "lbuy-2026.10.1"
MIN_REL_VOLUME = 3.0
PRE_SECONDS = 60
MIN_PRE_TRADES = 3
COOLDOWN = 300
HORIZONS = {"30s": 30, "60s": 60, "5m": 300, "15m": 900}
COMPLETE_AFTER = max(HORIZONS.values()) + 30
RETENTION_DAYS = 30
CANDIDATES_PER_PASS = 50
TRACK_PER_PASS = 200
LAMPORTS = Decimal(1_000_000_000)


def _price(t: Trade) -> float | None:
    return t.virtual_sol / t.virtual_token if t.virtual_token else None


def pre_state(trades: list[Trade], at: datetime, meta: dict | None) -> dict[str, Any]:
    window = [t for t in trades if at - timedelta(seconds=PRE_SECONDS) <= t.at < at]
    before = [t for t in trades if t.at < at]
    first = window[0] if window else None
    last = before[-1] if before else None
    buy_sol = [t.sol_lamports for t in window if t.is_buy]
    created = (meta or {}).get("created_at")
    return {
        "trades": len(window), "buys": len(buy_sol), "sells": len(window) - len(buy_sol),
        "buyers": len({t.trader for t in window if t.is_buy}), "sellers": len({t.trader for t in window if not t.is_buy}),
        "net_flow_sol": round(sum(t.sol_lamports if t.is_buy else -t.sol_lamports for t in window) / 1e9, 6),
        "median_buy_sol": round(statistics.median(buy_sol) / 1e9, 6) if buy_sol else None,
        "return_pct": round((_price(last) / _price(first) - 1) * 100, 3) if first and last and _price(first) else None,
        "virtual_sol": round(last.virtual_sol / 1e9, 3) if last else None,
        "token_age_s": int(at.timestamp() - int(created)) if created else None,
        "history_starts": trades[0].at.isoformat() if trades else None,
    }


def post_state(trades: list[Trade], at: datetime, ref_price: float | None, wallet: str | None,
               bought_raw: int | None) -> dict[str, Any]:
    """What happened after `at` (exclusive), from the stored trades."""
    seen_before = {t.trader for t in trades if t.at <= at}
    holders = {t.trader for t in trades if t.at <= at and t.is_buy}
    out: dict[str, Any] = {}
    for name, secs in HORIZONS.items():
        end = at + timedelta(seconds=secs)
        win = [t for t in trades if at < t.at <= end]
        last = win[-1] if win else None
        sells_prior = [t for t in win if not t.is_buy and t.trader in holders and t.trader != wallet]
        h = {
            "trades": len(win),
            "return_pct": round((_price(last) / ref_price - 1) * 100, 3) if last and ref_price and _price(last) else
            (0.0 if not win else None),
            "net_flow_sol": round(sum(t.sol_lamports if t.is_buy else -t.sol_lamports for t in win) / 1e9, 6),
            "new_buyers": len({t.trader for t in win if t.is_buy and t.trader not in seen_before}),
            "prior_holder_sells": len(sells_prior),
            "prior_holder_sell_sol": round(sum(t.sol_lamports for t in sells_prior) / 1e9, 6),
        }
        if wallet is not None:
            sold = sum(t.token_raw for t in win if not t.is_buy and t.trader == wallet)
            h["large_buyer_sold_share"] = round(sold / bought_raw, 4) if bought_raw else None
        out[name] = h
    return out


def _find(trades: list[Trade], ts: int, trader: str, lamports: int) -> Trade | None:
    return next((t for t in trades if int(t.at.timestamp()) == ts and t.trader == trader and t.sol_lamports == lamports
                 and t.is_buy), None)


def qualifies(trades: list[Trade], event: Trade) -> tuple[bool, dict[str, Any]]:
    pre = [t for t in trades if event.at - timedelta(seconds=PRE_SECONDS) <= t.at < event.at]
    buys = [t.sol_lamports for t in pre if t.is_buy]
    if len(pre) < MIN_PRE_TRADES or not buys:
        return False, {"why": f"{len(pre)} trades in the {PRE_SECONDS} s before (need {MIN_PRE_TRADES})"}
    rel_volume = event.sol_lamports / statistics.median(buys)
    rel_liq = event.sol_lamports / event.virtual_sol if event.virtual_sol else 0
    ok = rel_volume >= MIN_REL_VOLUME and rel_liq >= pump_stream.LARGE_BUY_MIN_LIQUIDITY_SHARE
    return ok, {"rel_volume": round(rel_volume, 3), "rel_liquidity": round(rel_liq, 5)}


def _pick_control(actives: list[str], exclude: set[str], seed: str) -> list[str]:
    """Active tokens in a deterministic order for this event (no RNG state)."""
    return sorted((m for m in actives if m not in exclude),
                  key=lambda m: hashlib.sha256(f"{seed}{m}".encode()).hexdigest())


async def process(session: AsyncSession, redis: Any, now: datetime | None = None) -> dict[str, int]:
    """Candidates noted by the stream -> EVENT (+ CONTROL) rows. Caller commits."""
    now = now or datetime.now(timezone.utc)
    out = {"candidates": 0, "events": 0, "controls": 0, "rejected": 0}
    t = int(now.timestamp())
    members = await redis.zrangebyscore(pump_stream.LARGE_CANDIDATES, "-inf", t, start=0, num=CANDIDATES_PER_PASS)
    if not members:
        return out
    await redis.zrem(pump_stream.LARGE_CANDIDATES, *members)
    actives = list(await redis.zrevrangebyscore(pump_stream.ACTIVE, "+inf", t - PRE_SECONDS, start=0, num=300))
    actives = [a.decode() if isinstance(a, bytes) else a for a in actives]
    for raw in members:
        out["candidates"] += 1
        mint, ts, trader, lamports = (raw.decode() if isinstance(raw, bytes) else raw).split("|")
        if not await redis.set(f"{pump_stream.PREFIX}:large_cooldown:{mint}", "1", nx=True, ex=COOLDOWN):
            out["rejected"] += 1
            continue
        trades = await pump_stream.load_trades(redis, mint)
        ev = _find(trades, int(ts), trader, int(lamports))
        ok, info = qualifies(trades, ev) if ev else (False, {"why": "trade not in the stored history"})
        if not ok:
            out["rejected"] += 1
            continue
        meta = await pump_stream.load_meta(redis, mint) or {}
        pre = pre_state(trades, ev.at, meta)
        pre["large_buyer_is_creator"] = bool(meta.get("creator")) and meta.get("creator") == trader
        pre["relation_note"] = "creator checked; other wallet relations are not known"
        pair = uuid.uuid4()
        res = await session.execute(insert(LargeTradeEvent).values(
            id=pair, kind="EVENT", pair_id=pair, mint=mint, at=ev.at, wallet=trader,
            sol=Decimal(int(lamports)) / LAMPORTS, rel_liquidity=Decimal(str(info["rel_liquidity"])),
            rel_volume=Decimal(str(info["rel_volume"])),
            pre={**pre, "event_price": _price(ev), "event_token_raw": ev.token_raw}, status="TRACKING",
            definition_version=DEFINITION_VERSION).on_conflict_do_nothing())
        if not res.rowcount:
            continue
        out["events"] += 1
        for cand in _pick_control(actives, {mint}, str(pair))[:10]:
            if await redis.exists(f"{pump_stream.PREFIX}:large_cooldown:{cand}"):
                continue  # had its own large buy recently: not an ordinary moment
            ct = [x for x in await pump_stream.load_trades(redis, cand) if x.at <= ev.at]
            cpre = pre_state(ct, ev.at, await pump_stream.load_meta(redis, cand) or {})
            if cpre["trades"] < MIN_PRE_TRADES or not ct:
                continue
            await session.execute(insert(LargeTradeEvent).values(
                kind="CONTROL", pair_id=pair, mint=cand, at=ev.at, pre={**cpre, "event_price": _price(ct[-1])},
                status="TRACKING", definition_version=DEFINITION_VERSION).on_conflict_do_nothing())
            out["controls"] += 1
            break
    return out


async def track(session: AsyncSession, redis: Any, now: datetime | None = None) -> dict[str, int]:
    """Writes `post` for rows whose last horizon has passed. Caller commits."""
    now = now or datetime.now(timezone.utc)
    rows = (await session.execute(select(LargeTradeEvent).where(
        LargeTradeEvent.status == "TRACKING", LargeTradeEvent.at <= now - timedelta(seconds=COMPLETE_AFTER))
        .order_by(LargeTradeEvent.at).limit(TRACK_PER_PASS))).scalars().all()
    out = {"completed": 0, "incomplete": 0}
    for r in rows:
        trades = await pump_stream.load_trades(redis, r.mint)
        # truncated: nothing stored at or before the moment, or the list is at its cap and no longer reaches
        # the start of the pre window (earlier holders would be undercounted)
        if (not trades or trades[0].at > r.at
                or (len(trades) >= pump_stream.MAX_TRADES_PER_MINT and trades[0].at > r.at - timedelta(seconds=PRE_SECONDS))):
            r.status, r.post = "INCOMPLETE", {"why": "the stored trade history no longer covers the window"}
            out["incomplete"] += 1
            continue
        pre = r.pre or {}
        r.post = post_state(trades, r.at, pre.get("event_price"), r.wallet, pre.get("event_token_raw"))
        r.status = "COMPLETE"
        out["completed"] += 1
    await session.execute(delete(LargeTradeEvent).where(LargeTradeEvent.id.in_(
        select(LargeTradeEvent.id).where(LargeTradeEvent.at < now - timedelta(days=RETENTION_DAYS)).limit(5000))))
    return out
