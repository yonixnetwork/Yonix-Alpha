"""Where did a token's early buyers get their SOL?

For each of the first N distinct buyers that is a *fresh* wallet (fewer
than FRESH_SIGNATURES transactions in its whole history), the funder is the
source of the SOL transfer in its oldest transaction. Established wallets
are skipped (a long history is itself evidence against a throwaway), and
funders with a very busy history (exchanges, bridges, payment processors)
are ignored, because many unrelated users are funded by them.

Results are indicators, reported as CREATOR-LINKED INDICATOR (a buyer's
funder is the token's creator) and RELATED-WALLET INDICATOR (several fresh
buyers share one funder). They are never presented as proof of common
control. RPC cost is bounded: at most 2 calls per checked wallet plus 1 per
distinct funder, all cached for a week.
"""

import json
from typing import Any

from yonixalpha_core.solana.rpc import RpcAllEndpointsFailedError, call_optional, get_transaction_params

FRESH_SIGNATURES = 25
BUSY_FUNDER_SIGNATURES = 1000
CACHE_TTL = 7 * 86400


async def _cached(redis, key: str, fetch):
    if redis is not None:
        hit = await redis.get(key)
        if hit is not None:
            return json.loads(hit)
    value = await fetch()
    if redis is not None:
        await redis.set(key, json.dumps(value), ex=CACHE_TTL)
    return value


def funder_from_transaction(tx: dict | None, wallet: str) -> str | None:
    """Source of the first System transfer into `wallet` (jsonParsed)."""
    if not tx:
        return None
    ixs = list(((tx.get("transaction") or {}).get("message") or {}).get("instructions") or [])
    for inner in ((tx.get("meta") or {}).get("innerInstructions") or []):
        ixs.extend(inner.get("instructions") or [])
    for ix in ixs:
        parsed = ix.get("parsed") if isinstance(ix, dict) else None
        if not isinstance(parsed, dict) or parsed.get("type") not in ("transfer", "transferWithSeed", "createAccount"):
            continue
        info = parsed.get("info") or {}
        dest = info.get("destination") or info.get("newAccount")
        if dest == wallet and info.get("source") and info["source"] != wallet:
            return info["source"]
    return None


async def wallet_funder(rpc, redis, wallet: str) -> dict[str, Any]:
    async def fetch():
        sigs = await call_optional(rpc, "getSignaturesForAddress", [wallet, {"limit": FRESH_SIGNATURES}]) or []
        if len(sigs) >= FRESH_SIGNATURES:
            return {"fresh": False, "funder": None}
        if not sigs:
            return {"fresh": True, "funder": None}
        oldest = sigs[-1]["signature"]
        tx = await call_optional(rpc, "getTransaction", get_transaction_params(oldest))
        return {"fresh": True, "funder": funder_from_transaction(tx, wallet)}
    return await _cached(redis, f"yx:funder:{wallet}", fetch)


async def funder_is_busy(rpc, redis, funder: str) -> bool:
    async def fetch():
        sigs = await call_optional(rpc, "getSignaturesForAddress", [funder, {"limit": BUSY_FUNDER_SIGNATURES}]) or []
        return {"busy": len(sigs) >= BUSY_FUNDER_SIGNATURES}
    return (await _cached(redis, f"yx:funder_busy:{funder}", fetch))["busy"]


async def funding_links(rpc, redis, early_buyers: list[str], creator: str | None, max_wallets: int) -> dict[str, Any]:
    """{'checked', 'creator_linked', 'largest_group', 'groups', 'errors'}"""
    out: dict[str, Any] = {"checked": 0, "creator_linked": 0, "largest_group": 0, "groups": {}, "errors": []}
    if max_wallets <= 0:
        return out
    funders: dict[str, list[str]] = {}
    for wallet in early_buyers[:max_wallets]:
        if wallet == creator:
            continue
        try:
            info = await wallet_funder(rpc, redis, wallet)
        except RpcAllEndpointsFailedError as exc:
            # No endpoint can answer now (usually HTTP 429): asking for the
            # remaining wallets would only extend the rate limit.
            out["errors"].append(f"stopped after {out['checked']} wallets: {type(exc).__name__}")
            break
        except Exception as exc:  # noqa: BLE001 - partial evidence is reported, never filled in
            out["errors"].append(f"{wallet[:6]}…: {type(exc).__name__}")
            continue
        out["checked"] += 1
        f = info.get("funder")
        if not info.get("fresh") or not f:
            continue
        if creator and f == creator:
            out["creator_linked"] += 1
        funders.setdefault(f, []).append(wallet)
    for f, wallets in funders.items():
        if len(wallets) < 2 or f == creator:
            continue
        try:
            if await funder_is_busy(rpc, redis, f):
                continue
        except RpcAllEndpointsFailedError as exc:
            out["errors"].append(f"funder check stopped: {type(exc).__name__}")
            break
        except Exception as exc:  # noqa: BLE001
            out["errors"].append(f"funder {f[:6]}…: {type(exc).__name__}")
            continue
        out["groups"][f] = wallets
    out["largest_group"] = max((len(w) for w in out["groups"].values()), default=0)
    return out


def early_buyers(trades, limit: int) -> list[str]:
    seen: list[str] = []
    for t in sorted(trades, key=lambda t: t.at):
        if t.is_buy and t.trader not in seen:
            seen.append(t.trader)
            if len(seen) >= limit:
                break
    return seen
