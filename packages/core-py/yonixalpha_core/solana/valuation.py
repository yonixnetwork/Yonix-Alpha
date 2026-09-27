"""Token holdings of the live wallet, valued in SOL (and USD when a SOL/USD
price is cached) from sources this system already reads:

  - a token still on its pump.fun bonding curve: the curve's virtual reserves
    from the trade stream (price at the last trade, with its timestamp);
  - a migrated token: its canonical PumpSwap pool's reserves (RPC, now);
  - wrapped SOL: 1:1.

These are marginal prices, not executable quotes: selling moves the price
and pays fees. A holding with no reliable price is VALUATION UNAVAILABLE with
the reason; a price is never estimated or carried over from elsewhere.
"""

import json
from datetime import datetime
from decimal import Decimal
from typing import Any

from yonixalpha_core.solana import pump_stream, pumpswap
from yonixalpha_core.solana.pumpfun import WSOL_MINT
from yonixalpha_core.solana.sol_price import CACHE_KEY as SOL_USD_KEY

LAMPORTS = Decimal(1_000_000_000)
MAX_HOLDINGS = 25
CURVE_PRICE_STALE_SECONDS = 300


async def _sol_usd(redis) -> tuple[Decimal | None, str | None]:
    raw = await redis.get(SOL_USD_KEY) if redis is not None else None
    if not raw:
        return None, None
    try:
        c = json.loads(raw)
        return Decimal(c["price"]), f"{c['source']} at {c['at']}"
    except (ValueError, KeyError, TypeError, ArithmeticError):
        return None, None


async def value_holdings(redis, rpc, tokens: dict[str, dict], now: datetime) -> dict[str, Any]:
    """{"holdings": [...], "total_sol": str|None, "sol_usd": ..., "valued_at"}.
    total_sol sums only reliably valued holdings and says how many were not."""
    sol_usd, usd_source = await _sol_usd(redis)
    out: list[dict[str, Any]] = []
    items = [(m, i) for m, i in tokens.items() if int(i.get("amount") or 0) > 0][:MAX_HOLDINGS]
    for mint, info in items:
        dec = info.get("decimals")
        row: dict[str, Any] = {"mint": mint, "amount_raw": str(info["amount"]), "decimals": dec,
                               "quantity": str(Decimal(int(info["amount"])) / Decimal(10) ** int(dec)) if dec is not None else None,
                               "price_sol": None, "value_sol": None, "value_usd": None, "price_source": None,
                               "price_at": None, "status": "VALUATION UNAVAILABLE", "reason": None}
        if dec is None:
            row["reason"] = "token decimals unknown"
            out.append(row)
            continue
        qty = Decimal(int(info["amount"])) / Decimal(10) ** int(dec)
        try:
            if mint == WSOL_MINT:
                price, source, at, status = Decimal(1), "wrapped SOL (1:1)", now, "VALUED"
            else:
                curve = await pump_stream.load_curve(redis, mint) if redis is not None else None
                if curve is not None and not curve.pool and not curve.complete and curve.vtok:
                    price = (Decimal(curve.vsol) / LAMPORTS) / (Decimal(curve.vtok) / Decimal(10) ** int(dec))
                    source, at = "pump.fun bonding curve (last stream trade)", curve.updated_at
                    age = (now - at).total_seconds() if at else None
                    status = "VALUED" if age is not None and age <= CURVE_PRICE_STALE_SECONDS else "STALE"
                elif rpc is not None:
                    state = await pumpswap.fetch_pool(rpc, mint, now, None, int(dec))
                    price, source, at, status = state.price, "PumpSwap pool reserves (RPC)", now, "VALUED"
                else:
                    raise pumpswap.PoolUnavailable("no curve in the stream and no RPC for the pool")
        except pumpswap.PoolUnavailable as exc:
            row["reason"] = str(exc)[:160]
            out.append(row)
            continue
        except Exception as exc:  # noqa: BLE001 - an unreadable source is unavailable, never guessed
            row["reason"] = f"price source failed: {type(exc).__name__}"
            out.append(row)
            continue
        value = qty * price
        row.update({"price_sol": str(price), "value_sol": str(value), "price_source": source,
                    "price_at": at.isoformat() if at else None, "status": status, "reason": None,
                    "value_usd": str((value * sol_usd).quantize(Decimal("0.01"))) if sol_usd is not None else None})
        out.append(row)
    valued = [Decimal(r["value_sol"]) for r in out if r["value_sol"] is not None and r["status"] == "VALUED"]
    return {"holdings": out, "total_sol": str(sum(valued, Decimal(0))), "valued_count": len(valued),
            "unvalued_count": len(out) - len(valued), "more_not_shown": max(0, len([1 for i in tokens.values()
                                                                                   if int(i.get("amount") or 0) > 0]) - len(items)),
            "sol_usd": str(sol_usd) if sol_usd is not None else None, "sol_usd_source": usd_source,
            "valued_at": now.isoformat(), "note": "marginal prices, not executable quotes"}
