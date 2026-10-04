"""Raydium LaunchLab: account decoding and swap quotes (master §7).

A read path only: decode a pool's PoolState, its GlobalConfig (curve type,
protocol fee) and PlatformConfig (platform and creator fees), and quote a
buy or a sell, so a LaunchLab launch (LetsBONK, StonkFun and the other sites
on it) can be observed and, once the research pipeline moves the venue to
PAPER, paper traded at a quoted price. Nothing here builds, signs or sends a
transaction, and the venue stays OBSERVE ONLY in the registry.

Port of the official TypeScript SDK (raydium-io raydium-sdk-v2 0.2.73-alpha,
src/raydium/launchpad/curve: curve.ts Curve.buyExactIn / buyExactOut /
sellExactIn / sellExactOut, constantProductCurve.ts, fixedPriceCurve.ts,
linearPriceCurve.ts), integer for integer. The tests compare it with vectors
produced by the SDK itself (tests/fixtures/launchlab_sdk). Layouts come from
the program's IDL (launchlab_layout); tools/launchlab_verify replays real
trades against the chain.

What is refused rather than guessed (LaunchLabError, so no quote, no trade):
  - the linear price curve: the SDK takes its square root through
    decimal.js (20 significant digits, rounded), so it cannot be the
    program's exact integer result; the math is here (integer square root)
    for the server replay to test, but quote_* refuse it until verified;
  - a Token-2022 base or quote mint (token_program_flag): a transfer fee
    extension would change the amounts and is not modelled;
  - a pool no longer on its curve (status not FUND).
No share (referral) fee is ever assumed: this system passes none.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isqrt
from typing import Any

from yonixalpha_core.solana import anchor_codec, launchlab_layout

FEE_RATE_DENOMINATOR = 1_000_000
Q64 = 1 << 64
CONSTANT_PRODUCT, FIXED_PRICE, LINEAR_PRICE = 0, 1, 2  # GlobalConfig.curve_type
FUND, MIGRATE, TRADE = 0, 1, 2  # PoolStatus: on the curve / waiting for migration / migrated
BUY, SELL = 0, 1  # TradeDirection
CURVE_NAMES = {CONSTANT_PRODUCT: "constant product", FIXED_PRICE: "fixed price", LINEAR_PRICE: "linear price"}


class LaunchLabError(ValueError):
    """No quote: the SDK would throw, or the case is refused (see module doc)."""


# --- decoding --------------------------------------------------------------------------------

def decode_account(name: str, data: bytes) -> dict[str, Any]:
    """PoolState, GlobalConfig or PlatformConfig account data -> dict."""
    try:
        return anchor_codec.decode_account(launchlab_layout, name, data)
    except anchor_codec.LayoutError as exc:
        raise LaunchLabError(str(exc)) from exc


def trade_events(tx: dict[str, Any]) -> list[dict[str, Any]]:
    """TradeEvents of one getTransaction result (encoding "json"), emitted by
    the program as self-invoked inner instructions (emit_cpi!)."""
    return [ev for name, ev in anchor_codec.cpi_events(launchlab_layout, tx) if name == "TradeEvent"]


# --- curves (amounts before fees) ------------------------------------------------------------

def _ceil_div(a: int, b: int) -> int:
    """ceilDivBN on the non-negative values the curves pass."""
    if b <= 0:
        raise LaunchLabError("Insufficient liquidity")
    return -(-a // b) if a else 0


def _pool_amounts(pool: dict[str, Any]) -> tuple[int, int, int, int]:
    return pool["virtual_base"], pool["virtual_quote"], pool["real_base"], pool["real_quote"]


def curve_buy_exact_in(curve_type: int, pool: dict[str, Any], amount: int) -> int:
    """Base out for `amount` quote in (fee already taken)."""
    vb, vq, rb, rq = _pool_amounts(pool)
    if curve_type == CONSTANT_PRODUCT:
        return amount * (vb - rb) // (vq + rq + amount)
    if curve_type == FIXED_PRICE:
        return vb * amount // vq
    if curve_type == LINEAR_PRICE:
        return isqrt(2 * (rq + amount) * Q64 // vb) - rb
    raise LaunchLabError(f"unknown curve type {curve_type}")


def curve_buy_exact_out(curve_type: int, pool: dict[str, Any], amount: int) -> int:
    """Quote in (before fee) for exactly `amount` base out."""
    vb, vq, rb, rq = _pool_amounts(pool)
    if curve_type == CONSTANT_PRODUCT:
        return _ceil_div((vq + rq) * amount, (vb - rb) - amount)
    if curve_type == FIXED_PRICE:
        return _ceil_div(vq * amount, vb)
    if curve_type == LINEAR_PRICE:
        new_base = rb + amount
        return _ceil_div(vb * new_base * new_base, 2 * Q64) - rq
    raise LaunchLabError(f"unknown curve type {curve_type}")


def curve_sell_exact_in(curve_type: int, pool: dict[str, Any], amount: int) -> int:
    """Quote out (before fee) for `amount` base in."""
    vb, vq, rb, rq = _pool_amounts(pool)
    if curve_type == CONSTANT_PRODUCT:
        return amount * (vq + rq) // ((vb - rb) + amount)
    if curve_type == FIXED_PRICE:
        return vq * amount // vb
    if curve_type == LINEAR_PRICE:
        new_base = rb - amount
        return rq - _ceil_div(vb * new_base * new_base, 2 * Q64)
    raise LaunchLabError(f"unknown curve type {curve_type}")


def curve_sell_exact_out(curve_type: int, pool: dict[str, Any], amount: int) -> int:
    """Base in for exactly `amount` quote out (fee included in `amount`)."""
    vb, vq, rb, rq = _pool_amounts(pool)
    if curve_type == CONSTANT_PRODUCT:
        return _ceil_div((vb - rb) * amount, (vq + rq) - amount)
    if curve_type == FIXED_PRICE:
        return _ceil_div(vb * amount, vq)
    if curve_type == LINEAR_PRICE:
        return rb - isqrt(2 * (rq - amount) * Q64 // vb)
    raise LaunchLabError(f"unknown curve type {curve_type}")


# --- fees ------------------------------------------------------------------------------------

@dataclass(frozen=True)
class FeeRates:
    """Per million: protocol (GlobalConfig.trade_fee_rate), platform and
    creator (PlatformConfig.fee_rate / creator_fee_rate), share (referral)."""
    protocol: int
    platform: int
    creator: int
    share: int = 0

    @property
    def total(self) -> int:
        total = self.protocol + self.platform + self.share + self.creator
        if total > FEE_RATE_DENOMINATOR:
            raise LaunchLabError("total fee rate gt 1_000_000")
        return total


def fee_rates(config: dict[str, Any], platform: dict[str, Any]) -> FeeRates:
    return FeeRates(config["trade_fee_rate"], platform["fee_rate"], platform["creator_fee_rate"])


def calculate_fee(amount: int, rate: int) -> int:
    return (amount * rate + FEE_RATE_DENOMINATOR - 1) // FEE_RATE_DENOMINATOR


def calculate_pre_fee(post_fee_amount: int, rate: int) -> int:
    if rate == 0:
        return post_fee_amount
    den = FEE_RATE_DENOMINATOR - rate
    return (post_fee_amount * FEE_RATE_DENOMINATOR + den - 1) // den


def split_fee(total_fee: int, rates: FeeRates) -> dict[str, int]:
    total_rate = rates.total
    platform = total_fee * rates.platform // total_rate if total_rate else 0
    share = total_fee * rates.share // total_rate if total_rate else 0
    creator = total_fee * rates.creator // total_rate if total_rate else 0
    return {"protocol_fee": total_fee - platform - share - creator, "platform_fee": platform,
            "share_fee": share, "creator_fee": creator}


# --- trades (Curve.*, no transfer fee) -------------------------------------------------------

@dataclass
class Trade:
    base_amount: int  # bought (buy) or sold (sell)
    quote_amount: int  # paid, fees included (buy) or received, fees taken (sell)
    fees: dict[str, int]

    @property
    def total_fee(self) -> int:
        return sum(self.fees.values())


def buy_exact_in(curve_type: int, pool: dict[str, Any], amount_quote: int, rates: FeeRates) -> Trade:
    """Curve.buyExactIn: spend `amount_quote` (fees included). A buy larger
    than what is left on the curve fills the rest and pays only for that."""
    fee_rate = rates.total
    total_fee = calculate_fee(amount_quote, fee_rate)
    less_fee = amount_quote - total_fee
    base = curve_buy_exact_in(curve_type, pool, less_fee)
    remaining = pool["total_base_sell"] - pool["real_base"]
    paid = amount_quote
    if base > remaining:
        base = remaining
        less_fee = curve_buy_exact_out(curve_type, pool, base)
        paid = calculate_pre_fee(less_fee, fee_rate)
        total_fee = paid - less_fee
    return Trade(base, paid, split_fee(total_fee, rates))


def buy_exact_out(curve_type: int, pool: dict[str, Any], amount_base: int, rates: FeeRates) -> Trade:
    """Curve.buyExactOut: buy `amount_base` (capped to what is left on the
    curve; the trade reports the capped amount)."""
    remaining = pool["total_base_sell"] - pool["real_base"]
    base = min(amount_base, remaining)
    less_fee = curve_buy_exact_out(curve_type, pool, base)
    paid = calculate_pre_fee(less_fee, rates.total)
    return Trade(base, paid, split_fee(paid - less_fee, rates))


def sell_exact_in(curve_type: int, pool: dict[str, Any], amount_base: int, rates: FeeRates) -> Trade:
    """Curve.sellExactIn: sell `amount_base`. More than the curve has sold
    (real_base) is refused: no holder can have it."""
    if amount_base > pool["real_base"]:
        raise LaunchLabError("Insufficient liquidity")
    gross = curve_sell_exact_in(curve_type, pool, amount_base)
    total_fee = calculate_fee(gross, rates.total)
    return Trade(amount_base, gross - total_fee, split_fee(total_fee, rates))


def sell_exact_out(curve_type: int, pool: dict[str, Any], amount_quote: int, rates: FeeRates) -> Trade:
    """Curve.sellExactOut: receive exactly `amount_quote` (after fees)."""
    gross = calculate_pre_fee(amount_quote, rates.total)
    if pool["real_quote"] < gross:
        raise LaunchLabError("Insufficient liquidity")
    base = curve_sell_exact_out(curve_type, pool, gross)
    if base > pool["real_base"]:
        raise LaunchLabError("Insufficient liquidity")
    return Trade(base, amount_quote, split_fee(gross - amount_quote, rates))


# --- quoting a live pool ---------------------------------------------------------------------

def check_quotable(pool: dict[str, Any], config: dict[str, Any]) -> None:
    """Raises LaunchLabError when this pool must not be quoted (module doc)."""
    if pool["status"] != FUND:
        raise LaunchLabError(f"pool is not on its curve (status {pool['status']})")
    flag = pool["token_program_flag"]  # bit 0: base mint, bit 1: quote mint on Token-2022
    if flag:
        which = " and ".join(n for bit, n in ((1, "base"), (2, "quote")) if flag & bit) or f"flag {flag}"
        raise LaunchLabError(f"Token-2022 {which} mint: transfer fees are not modelled")
    if config["curve_type"] == LINEAR_PRICE:
        raise LaunchLabError("linear price curve: the program's rounding is NOT VERIFIED")
    if config["curve_type"] not in CURVE_NAMES:
        raise LaunchLabError(f"unknown curve type {config['curve_type']}")


def quote_buy(pool: dict[str, Any], config: dict[str, Any], platform: dict[str, Any], amount_quote: int) -> Trade:
    check_quotable(pool, config)
    if amount_quote <= 0:
        raise LaunchLabError("Amount is zero")
    return buy_exact_in(config["curve_type"], pool, amount_quote, fee_rates(config, platform))


def quote_sell(pool: dict[str, Any], config: dict[str, Any], platform: dict[str, Any], amount_base: int) -> Trade:
    check_quotable(pool, config)
    if amount_base <= 0:
        raise LaunchLabError("Amount is zero")
    return sell_exact_in(config["curve_type"], pool, amount_base, fee_rates(config, platform))


def price_quote_per_base(curve_type: int, pool: dict[str, Any]) -> float:
    """Marginal price (quote per whole base token), as the SDK's getPoolPrice."""
    vb, vq, rb, rq = _pool_amounts(pool)
    scale = 10 ** (pool["base_decimals"] - pool["quote_decimals"])
    if curve_type == CONSTANT_PRODUCT:
        return (vq + rq) / (vb - rb) * scale
    if curve_type == FIXED_PRICE:
        return vq / vb * scale
    if curve_type == LINEAR_PRICE:
        return vb * rb / Q64 * scale
    raise LaunchLabError(f"unknown curve type {curve_type}")


def curve_progress(pool: dict[str, Any]) -> float | None:
    """Share of the curve's base already sold (1.0: ready to migrate)."""
    total = pool["total_base_sell"]
    return pool["real_base"] / total if total else None
