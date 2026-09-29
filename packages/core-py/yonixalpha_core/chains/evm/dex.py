"""Shared EVM DEX / token reads: ERC-20 metadata, Uniswap V3 (QuoterV2 and
pool Swap events) and Uniswap-V2-style routers (PancakeSwap V2 on BSC).

Every quote here comes from the contract (QuoterV2 / getAmountsOut via
eth_call), never from a price API.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from yonixalpha_core.chains.base import Quote
from yonixalpha_core.chains.evm.abi import decode_output, encode_call, event
from yonixalpha_core.chains.evm.rpc import EvmRpc, EvmRpcError, EvmRpcUnavailableError

# Any address works as the caller of a view-like simulation; this one holds
# nothing and is not a precompile.
SIM_ACCOUNT = "0x00000000000000000000000000000000000Da7a5"

V3_SWAP = event("Swap", ("sender", "address", True), ("recipient", "address", True), ("amount0", "int256"),
                ("amount1", "int256"), ("sqrtPriceX96", "uint160"), ("liquidity", "uint128"), ("tick", "int24"))


def now() -> datetime:
    return datetime.now(timezone.utc)


def failed(amount_in: int, source: str, exc: Exception | str) -> Quote:
    """A quote that could not be obtained. Never usable as a price and never
    read as 'safe': callers treat ok=False as UNKNOWN."""
    msg = str(exc)[:240]
    if isinstance(exc, EvmRpcUnavailableError):
        msg = f"RPC unavailable: {msg}"
    elif isinstance(exc, EvmRpcError):
        msg = f"reverted / rejected: {msg}"
    return Quote(ok=False, amount_in=amount_in, amount_out=None, source=source, error=msg, at=now())


async def call(rpc: EvmRpc, to: str, signature: str, out_types: list[str], *args: Any, **kw: Any) -> tuple:
    return decode_output(out_types, await rpc.eth_call(to, encode_call(signature, *args), **kw))


async def erc20_meta(rpc: EvmRpc, token: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, sig, typ in (("name", "name()", "string"), ("symbol", "symbol()", "string"),
                          ("decimals", "decimals()", "uint8"), ("total_supply", "totalSupply()", "uint256")):
        try:
            out[key] = (await call(rpc, token, sig, [typ]))[0]
        except EvmRpcError:
            out[key] = None
    return out


async def erc20_balance(rpc: EvmRpc, token: str, owner: str) -> int:
    return (await call(rpc, token, "balanceOf(address)", ["uint256"], owner))[0]


def v3_token0(a: str, b: str) -> str:
    """Uniswap orders a pool's tokens by address."""
    return a if int(a, 16) < int(b, 16) else b


async def v3_pool_fee(rpc: EvmRpc, pool: str) -> int:
    return (await call(rpc, pool, "fee()", ["uint24"]))[0]


async def v3_quote(rpc: EvmRpc, quoter: str, token_in: str, token_out: str, amount_in: int, fee: int,
                   source: str) -> Quote:
    """QuoterV2.quoteExactInputSingle (non-view; simulated through eth_call)."""
    try:
        out, _sqrt_after, _ticks, gas = await call(
            rpc, quoter, "quoteExactInputSingle((address,address,uint256,uint24,uint160))",
            ["uint256", "uint160", "uint32", "uint256"], (token_in, token_out, amount_in, fee, 0))
    except (EvmRpcError, EvmRpcUnavailableError) as exc:
        return failed(amount_in, source, exc)
    return Quote(ok=out > 0, amount_in=amount_in, amount_out=out, fee=amount_in * fee // 1_000_000,
                 route=f"uniswap_v3:{fee}", source=source, at=now(), error=None if out > 0 else "zero output")


async def v2_quote(rpc: EvmRpc, router: str, path: list[str], amount_in: int, source: str) -> Quote:
    try:
        amounts = (await call(rpc, router, "getAmountsOut(uint256,address[])", ["uint256[]"], amount_in, path))[0]
    except (EvmRpcError, EvmRpcUnavailableError) as exc:
        return failed(amount_in, source, exc)
    out = amounts[-1] if amounts else 0
    return Quote(ok=out > 0, amount_in=amount_in, amount_out=out, route="v2_router", source=source, at=now(),
                 error=None if out > 0 else "zero output")


def v3_swap_to_trade(args: dict[str, Any], token: str, quote_token: str) -> dict[str, Any] | None:
    """Pool-perspective V3 amounts → a trade on `token`. The pool pays out the
    side the trader receives (negative amount). Returns None for a swap that
    does not move the token (cannot happen in a two-token pool, kept defensive)."""
    token_is_0 = v3_token0(token, quote_token).lower() == token.lower()
    a_tok, a_quote = (args["amount0"], args["amount1"]) if token_is_0 else (args["amount1"], args["amount0"])
    if a_tok == 0:
        return None
    is_buy = a_tok < 0
    tokens, quote_amt = abs(a_tok), abs(a_quote)
    return {"is_buy": is_buy, "token_amount": tokens, "quote_amount": quote_amt,
            "price": (Decimal(quote_amt) / Decimal(tokens)) if tokens else None}


def sqrt_price_to_price(sqrt_price_x96: int, token_is_0: bool) -> Decimal:
    """Quote per token (both 18 decimals) from a V3 sqrtPriceX96."""
    p = (Decimal(sqrt_price_x96) / Decimal(2 ** 96)) ** 2  # token1 per token0
    return p if token_is_0 else (Decimal(1) / p if p else Decimal(0))
