"""Where can this token actually be traded right now? Decided from on-chain
state at execution time — never from the engine label, the token name or
the source that discovered it.

  PUMP_BONDING_CURVE  the mint's Pump bonding curve exists, is not complete
                      and has reserves: trade on the curve (no DEX pool is
                      needed, and "no DEX pool" is not a rejection).
  PUMP_AMM            the curve is complete (or absent) and the mint's
                      canonical PumpSwap pool exists, is SOL-quoted and has
                      reserves: trade on the pool.
  JUPITER_ROUTE       no Pump venue; Jupiter returns an executable route
                      (a quote with a route plan and a positive output).
  OTHER_SUPPORTED_DEX reserved: no direct integration exists for any other
                      DEX; such pools are reached through Jupiter.
  NO_EXECUTABLE_ROUTE nothing above. `reason` says exactly why (curve
                      complete but the pool not created yet = migration in
                      progress; Jupiter has no route; not an SPL mint...).
                      Liquidity is never assumed.

One getMultipleAccounts call reads the mint, its bonding curve and its
canonical pool together, so the three are from the same slot.
"""

import base64
import struct
from dataclasses import dataclass, field
from typing import Any

from yonixalpha_core.solana.pump_tx import (
    PUMP, PUMP_AMM, TOKEN, TOKEN_2022, WSOL, AmmPoolInfo, bonding_curve_pda,
)
from yonixalpha_core.solana.pumpfun import BondingCurveState, decode_bonding_curve
from yonixalpha_core.solana.pumpswap import canonical_pool, decode_pool

PUMP_BONDING_CURVE = "PUMP_BONDING_CURVE"
PUMP_AMM_VENUE = "PUMP_AMM"
JUPITER_ROUTE = "JUPITER_ROUTE"
OTHER_SUPPORTED_DEX = "OTHER_SUPPORTED_DEX"
NO_EXECUTABLE_ROUTE = "NO_EXECUTABLE_ROUTE"
RPC_UNAVAILABLE = "RPC_UNAVAILABLE"


@dataclass
class Venue:
    kind: str
    mint: str
    reason: str = ""
    token_program: str | None = None
    decimals: int | None = None
    slot: int | None = None
    curve: BondingCurveState | None = None
    pool: AmmPoolInfo | None = None
    pool_base_reserve: int | None = None
    pool_quote_reserve: int | None = None  # effective: vault + virtual quote reserves
    jupiter_quote: dict[str, Any] | None = None
    checks: list[str] = field(default_factory=list)  # what was verified, for the funnel

    @property
    def executable(self) -> bool:
        return self.kind in (PUMP_BONDING_CURVE, PUMP_AMM_VENUE, JUPITER_ROUTE)

    def summary(self) -> dict[str, Any]:
        out: dict[str, Any] = {"venue": self.kind, "reason": self.reason, "token_program": self.token_program,
                               "decimals": self.decimals, "slot": self.slot, "checks": self.checks}
        if self.curve is not None:
            out["curve"] = {"complete": self.curve.complete, "virtual_sol": self.curve.virtual_quote_reserves,
                            "virtual_tokens": self.curve.virtual_token_reserves, "creator": self.curve.creator,
                            "mayhem": self.curve.is_mayhem_mode, "cashback": self.curve.is_cashback_coin}
        if self.pool is not None:
            out["pool"] = {"address": self.pool.pool, "base_reserve": self.pool_base_reserve,
                           "quote_reserve": self.pool_quote_reserve}
        if self.jupiter_quote is not None:
            q = self.jupiter_quote
            out["jupiter"] = {"outAmount": q.get("outAmount"), "priceImpactPct": q.get("priceImpactPct"),
                              "route": [s.get("swapInfo", {}).get("label") for s in q.get("routePlan", [])]}
        return out


def _data(acct: dict | None) -> bytes | None:
    if not acct:
        return None
    d = acct.get("data")
    return base64.b64decode(d[0]) if isinstance(d, list) and d else None


async def resolve(rpc, mint: str, *, side: str = "buy", amount_raw: int | None = None, jupiter=None,
                  slippage_bps: int = 1000) -> Venue:
    """`amount_raw`: lamports to spend (buy) or raw tokens to sell, used only
    for the Jupiter quote. Raises nothing: RPC failure is RPC_UNAVAILABLE."""
    curve_addr, pool_addr = bonding_curve_pda(mint), canonical_pool(mint)
    try:
        res = await rpc.call("getMultipleAccounts", [[mint, curve_addr, pool_addr],
                                                     {"encoding": "base64", "commitment": "confirmed"}])
    except Exception as exc:  # noqa: BLE001 - no RPC answer is a state, not a venue
        return Venue(RPC_UNAVAILABLE, mint, reason=f"venue state unavailable: {type(exc).__name__}: {str(exc)[:160]}")
    values = (res or {}).get("value") or [None, None, None]
    slot = ((res or {}).get("context") or {}).get("slot")
    mint_acct, curve_acct, pool_acct = (list(values) + [None, None, None])[:3]
    v = Venue(NO_EXECUTABLE_ROUTE, mint, slot=slot)

    if not mint_acct:
        v.reason = "mint account does not exist"
        return v
    if mint_acct.get("owner") not in (TOKEN, TOKEN_2022):
        v.reason = f"not an SPL mint (owner {mint_acct.get('owner')})"
        return v
    v.token_program = mint_acct["owner"]
    mdata = _data(mint_acct) or b""
    if len(mdata) >= 45:
        v.decimals = mdata[44]  # spl-token Mint: supply u64 @36, decimals u8 @44
    v.checks.append(f"mint owned by {'Token-2022' if v.token_program == TOKEN_2022 else 'SPL Token'}")

    curve = None
    if curve_acct and curve_acct.get("owner") == PUMP:
        curve = decode_bonding_curve(_data(curve_acct) or b"")
        v.curve = curve
    if curve is not None:
        v.checks.append(f"bonding curve {curve_addr} found (complete={curve.complete})")
        if not curve.sol_quoted:
            v.reason = f"bonding curve quoted in {curve.quote_mint}, not SOL"
            return v
        if not curve.complete and curve.has_reserves and curve.real_token_reserves > 0:
            v.kind = PUMP_BONDING_CURVE
            v.reason = "active Pump bonding curve"
            return v

    if pool_acct and pool_acct.get("owner") == PUMP_AMM:
        raw = _data(pool_acct) or b""
        try:
            acct = decode_pool(raw)
        except Exception as exc:  # noqa: BLE001
            v.reason = f"canonical pool {pool_addr} unreadable: {exc}"
            return v
        if acct.base_mint != mint or acct.quote_mint != WSOL:
            v.reason = "canonical pool mints do not match the token and WSOL"
            return v
        try:
            vaults = await rpc.call("getMultipleAccounts", [[acct.base_vault, acct.quote_vault],
                                                            {"encoding": "base64", "commitment": "confirmed"}])
        except Exception as exc:  # noqa: BLE001
            return Venue(RPC_UNAVAILABLE, mint, reason=f"pool vaults unavailable: {type(exc).__name__}")
        vals = (vaults or {}).get("value") or []
        amounts = []
        for acc in vals[:2]:
            d = _data(acc) or b""
            amounts.append(struct.unpack_from("<Q", d, 64)[0] if len(d) >= 72 else 0)  # token account amount @64
        base, quote = (amounts + [0, 0])[:2]
        quote += acct.virtual_quote_reserves
        if base <= 0 or quote <= 0:
            v.reason = "canonical PumpSwap pool has no reserves"
            return v
        v.kind = PUMP_AMM_VENUE
        v.pool = AmmPoolInfo(pool=pool_addr, base_mint=acct.base_mint, quote_mint=acct.quote_mint,
                             pool_base_token_account=acct.base_vault, pool_quote_token_account=acct.quote_vault,
                             coin_creator=acct.coin_creator or "11111111111111111111111111111111",
                             is_mayhem_mode=acct.is_mayhem_mode, is_cashback_coin=acct.is_cashback_coin,
                             account_size=len(raw))
        v.pool_base_reserve, v.pool_quote_reserve = base, quote
        v.reason = "canonical PumpSwap pool" + (" (bonding curve complete)" if curve is not None else "")
        v.checks.append(f"pool {pool_addr}: base {base}, quote {quote}")
        return v

    if curve is not None and curve.complete:
        # Graduated, but the pool isn't there (yet): during migration nothing
        # can trade this token on Pump; an older graduation (Raydium era) may
        # still be routable through Jupiter below.
        v.checks.append("bonding curve complete, canonical PumpSwap pool not found")
    if jupiter is None or not amount_raw:
        v.reason = ("MIGRATION_IN_PROGRESS: bonding curve complete, canonical PumpSwap pool not created yet"
                    if curve is not None and curve.complete else "no Pump venue, and no aggregator route checked")
        return v
    in_mint, out_mint = (WSOL, mint) if side == "buy" else (mint, WSOL)
    q = await jupiter.quote(in_mint, out_mint, amount_raw, slippage_bps)
    if q.status == "ok" and (q.out_amount or 0) > 0 and (q.data or {}).get("routePlan"):
        v.kind, v.jupiter_quote = JUPITER_ROUTE, q.data
        v.reason = "Jupiter route: " + " → ".join(q.labels)
        v.checks.append(f"Jupiter quote out {q.out_amount}")
        return v
    if q.status == "error":
        v.kind = RPC_UNAVAILABLE
        v.reason = f"Jupiter quote unavailable: {q.error}"
        return v
    v.reason = ("MIGRATION_IN_PROGRESS: bonding curve complete, no PumpSwap pool and no aggregator route yet"
                if curve is not None and curve.complete else f"no executable route (Jupiter: {q.error or 'no route'})")
    return v
