"""Token terminal data: price, market cap, liquidity, windowed flow, chart
series and the live activity feed of one mint, from the recorded Pump.fun
trade stream (real trade events only; nothing is synthesised).

Units: prices in SOL per whole token; market cap = price × total supply
(Pump.fun mints a fixed 1,000,000,000 at creation, so market cap and FDV are
the same number there); liquidity = SOL actually in the bonding curve (real
reserves), a different number from market cap.

After migration the stream no longer carries the token's trades: the
header then uses the latest gate assessment's pool figures and says so.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

LAMPORTS = Decimal(1_000_000_000)
PUMP_DECIMALS = 6
PUMP_SUPPLY_TOKENS = Decimal(1_000_000_000)
CURVE_INITIAL_VIRTUAL_SOL = 30 * 10**9  # virtual SOL a fresh curve starts with (real SOL = virtual − this)
LARGE_TRADE_SOL = Decimal("1")
WINDOWS = (("1m", 60), ("5m", 300))


def _price(t, decimals: int) -> Decimal | None:
    if not t.virtual_token:
        return None
    return (Decimal(t.virtual_sol) / LAMPORTS) / (Decimal(t.virtual_token) / Decimal(10) ** decimals)


def _window(trades, now: datetime, seconds: int, decimals: int) -> dict[str, Any]:
    cur = [t for t in trades if now - timedelta(seconds=seconds) < t.at <= now]
    prev = [t for t in trades if now - timedelta(seconds=2 * seconds) < t.at <= now - timedelta(seconds=seconds)]
    buys, sells = [t for t in cur if t.is_buy], [t for t in cur if not t.is_buy]
    bv, sv = sum(t.sol_lamports for t in buys), sum(t.sol_lamports for t in sells)

    def change(w) -> Decimal | None:
        if len(w) < 2:
            return None
        a, b = _price(w[0], decimals), _price(w[-1], decimals)
        return ((b / a - 1) * 100).quantize(Decimal("0.01")) if a and b else None

    c_now, c_prev = change(cur), change(prev)
    buyers, sellers = len({t.trader for t in buys}), len({t.trader for t in sells})
    return {
        "trades": len(cur), "tx_per_minute": round(len(cur) * 60 / seconds, 2), "buyers": buyers, "sellers": sellers,
        "buy_sell_ratio": str((Decimal(buyers) / Decimal(sellers)).quantize(Decimal("0.01"))) if sellers else None,
        "buy_volume_sol": str(Decimal(bv) / LAMPORTS), "sell_volume_sol": str(Decimal(sv) / LAMPORTS),
        "volume_sol": str(Decimal(bv + sv) / LAMPORTS),
        "price_change_pct": str(c_now) if c_now is not None else None,
        "price_acceleration_pct": str(c_now - c_prev) if c_now is not None and c_prev is not None else None,
        "previous_trades": len(prev),
    }


def market_view(mint: str, meta: dict[str, Any] | None, curve: Any, trades: list, now: datetime,
                latest: dict[str, Any] | None = None, migrated_at: datetime | None = None,
                decimals: int = PUMP_DECIMALS) -> dict[str, Any]:
    meta = meta or {}
    trades = sorted(trades or [], key=lambda t: t.at)
    last = trades[-1] if trades else None
    price = _price(last, decimals) if last else None
    price_at = last.at if last else None
    price_source = "pump.fun stream (last trade)" if last else None
    migrated = bool(curve is not None and (getattr(curve, "pool", None) or getattr(curve, "complete", False)))
    liquidity = None
    if curve is not None and not migrated and getattr(curve, "rsol", None) is not None:
        liquidity = {"sol": str(Decimal(curve.rsol) / LAMPORTS), "basis": "bonding-curve real SOL reserve"}
    evidence = ((latest or {}).get("inputs_snapshot") or {}) if latest else {}
    if migrated:
        pool = evidence.get("pool") or {}
        feats = evidence.get("features") or {}
        if feats.get("price"):
            price, price_source = Decimal(str(feats["price"])), "PumpSwap pool (latest gate assessment)"
            price_at = None
        if pool.get("quote_reserve_lamports"):
            liquidity = {"sol": str(Decimal(pool["quote_reserve_lamports"]) / LAMPORTS),
                         "basis": "PumpSwap pool SOL reserve (latest gate assessment)"}
    created = int(meta["created_at"]) if meta.get("created_at") else None
    age = (now.timestamp() - created) if created else None

    series = [{"t": t.at.isoformat(), "price": str(_price(t, decimals)),
               "liquidity_sol": str(Decimal(max(t.virtual_sol - CURVE_INITIAL_VIRTUAL_SOL, 0)) / LAMPORTS)}
              for t in trades if _price(t, decimals) is not None]
    buckets: dict[int, dict[str, int]] = {}
    for t in trades:
        k = int(t.at.timestamp()) // 10 * 10
        b = buckets.setdefault(k, {"buy": 0, "sell": 0})
        b["buy" if t.is_buy else "sell"] += t.sol_lamports
    volume = [{"t": datetime.fromtimestamp(k, tz=now.tzinfo).isoformat(), "buy_sol": str(Decimal(v["buy"]) / LAMPORTS),
               "sell_sol": str(Decimal(v["sell"]) / LAMPORTS)} for k, v in sorted(buckets.items())]

    activity: list[dict[str, Any]] = []
    for t in reversed(trades[-60:]):
        sol = Decimal(t.sol_lamports) / LAMPORTS
        activity.append({"kind": "BUY" if t.is_buy else "SELL", "at": t.at.isoformat(), "wallet": t.trader,
                         "sol": str(sol), "tokens": str(Decimal(t.token_raw) / Decimal(10) ** decimals),
                         "price": str(_price(t, decimals)) if _price(t, decimals) is not None else None,
                         "large": sol >= LARGE_TRADE_SOL,
                         "creator": bool(meta.get("creator")) and t.trader == meta.get("creator")})
    if migrated_at is not None:
        activity.insert(0, {"kind": "MIGRATION", "at": migrated_at.isoformat(),
                            "detail": f"moved to PumpSwap pool {getattr(curve, 'pool', None) or ''}".strip()})
    if created:
        activity.append({"kind": "CREATED", "at": datetime.fromtimestamp(created, tz=now.tzinfo).isoformat(),
                         "wallet": meta.get("creator")})
    activity.sort(key=lambda e: e["at"], reverse=True)

    a = latest or {}
    snap = a.get("inputs_snapshot") or {}
    strategy = [{"code": f.get("code"), "message": f.get("message"), "action": f.get("action")}
                for f in (a.get("findings") or []) if f.get("category") in ("STRATEGY", "ML")]
    decision = None
    if a:
        decision = {"decision": a.get("decision"), "status_label": a.get("status_label"), "overall_risk": a.get("overall_risk"),
                    "reasons": (a.get("reasons") or [])[:8], "evaluated_at": a.get("evaluated_at"),
                    "qualified": a.get("qualified"), "signal": strategy[:6],
                    "ml": snap.get("ml"), "entry_quality": snap.get("entry_quality"),
                    "volatility": {"confidence": snap.get("volatility_confidence"), "source": snap.get("volatility_source")}}
    return {
        "mint": mint, "symbol": meta.get("symbol"), "name": meta.get("name"), "creator": meta.get("creator"),
        "header": {
            "price_sol": str(price) if price is not None else None, "price_source": price_source,
            "price_at": price_at.isoformat() if price_at else None,
            "market_cap_sol": str((price * PUMP_SUPPLY_TOKENS).quantize(Decimal("0.01"))) if price is not None else None,
            "market_cap_basis": "price × 1,000,000,000 (Pump.fun fixed supply; market cap = FDV)",
            "liquidity": liquidity,
            "migration_state": "MIGRATED (PumpSwap)" if getattr(curve, "pool", None) else
                               "CURVE COMPLETE (migrating)" if migrated else "BONDING CURVE" if curve is not None else "UNKNOWN",
            "age_seconds": round(age) if age is not None else None,
            "risk_status": a.get("overall_risk"), "decision": a.get("decision"),
        },
        "windows": {name: _window(trades, now, sec, decimals) for name, sec in WINDOWS},
        "series": series[-400:], "volume": volume[-120:], "activity": activity[:80],
        "decision": decision,
        "links": {
            "dexscreener": f"https://dexscreener.com/solana/{mint}",
            "explorer": f"https://solscan.io/token/{mint}",
            "pump_fun": f"https://pump.fun/coin/{mint}",
            "telegram": None,
            "note": "Links are built from the mint address; the external site may not have indexed this token yet. "
                    "No Telegram link: the token metadata this platform stores has none.",
        },
        "stream_trades": len(trades),
    }
