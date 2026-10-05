"""Meteora Dynamic Bonding Curve: account decoding and swap quotes (master §7).

A read path only: decode a pool's PoolConfig and VirtualPool accounts and
quote an exact-input swap in either direction, so a DBC launch can be
observed and, once the research pipeline moves the venue to PAPER, paper
traded at a quoted price. Nothing here builds, signs or sends a transaction,
and the venue stays OBSERVE ONLY in the registry.

Port of the official TypeScript SDK (MeteoraAg dynamic-bonding-curve-sdk
1.5.13, src/math: swapQuote.ts getSwapResult / calculate*FromAmountIn /
calculate*FromAmountOut,
curve.ts, feeMath.ts, poolFees/*), integer for integer: Python ints stand in
for BN, `//` is BN's truncating division on non-negative values, and every
rounding direction is the SDK's. The tests compare it with vectors produced
by the SDK itself (tests/fixtures/dbc_sdk/fixtures.json, generator
tests/fixtures/dbc_sdk/generate.cjs). Layouts come from the program's IDL
(dbc_layout); tools/dbc_verify replays real swaps against the chain.

Quoting assumptions, stated rather than hidden:
  - currentPoint is the slot or unix time per the config's activation_type;
  - the "first swap with minimum fee" discount is not assumed (it applies
    only to a pool's very first swap), so the full fee is quoted;
  - no referral account.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from yonixalpha_core.solana import anchor_codec, dbc_layout

RESOLUTION = 64
ONE_Q64 = 1 << RESOLUTION
FEE_DENOMINATOR = 1_000_000_000
MAX_BASIS_POINT = 10_000
MAX_FEE_NUMERATOR = 990_000_000
PROTOCOL_FEE_PERCENT = 20
HOST_FEE_PERCENT = 20
U16_MAX = 65_535
U64_MAX = (1 << 64) - 1
U128_MAX = (1 << 128) - 1
DYNAMIC_FEE_SCALING_FACTOR = 100_000_000_000
DYNAMIC_FEE_ROUNDING_OFFSET = 99_999_999_999

BASE_TO_QUOTE, QUOTE_TO_BASE = 0, 1  # TradeDirection
COLLECT_QUOTE_TOKEN, COLLECT_OUTPUT_TOKEN = 0, 1  # CollectFeeMode
FEE_SCHEDULER_LINEAR, FEE_SCHEDULER_EXPONENTIAL, RATE_LIMITER = 0, 1, 2  # BaseFeeMode
UP, DOWN = "up", "down"


class DbcError(ValueError):
    """The SDK would throw here (insufficient liquidity, completed pool...)."""


# --- decoding (IDL, fixed layout) ---------------------------------------------------------

def decode_account(name: str, data: bytes) -> dict[str, Any]:
    """PoolConfig or VirtualPool account data -> dict (snake_case fields)."""
    try:
        return anchor_codec.decode_account(dbc_layout, name, data)
    except anchor_codec.LayoutError as exc:
        raise DbcError(str(exc)) from exc


def decode_event(data: bytes) -> tuple[str, dict[str, Any]] | None:
    """An EvtSwap / EvtSwap2 event, with or without the emit_cpi! tag."""
    return anchor_codec.decode_event(dbc_layout, data)


def swap_events(tx: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Swap events of one getTransaction result (encoding "json"): the DBC
    program emits them as self-invoked inner instructions (emit_cpi!)."""
    return anchor_codec.cpi_events(dbc_layout, tx)


def _swap_key(ev: dict[str, Any]) -> tuple:
    res = ev["swap_result"]
    return ev["pool"], ev["trade_direction"], res["output_amount"], res["next_sqrt_price"], ev["current_timestamp"]


def unique_swaps(events: list[tuple[str, dict[str, Any]]]) -> list[tuple[str, dict[str, Any]]]:
    """One entry per swap: one swap can be reported by both an EvtSwap and an
    EvtSwap2 (next to each other, same pool, direction, output and next sqrt
    price); the pair is kept once, as the EvtSwap2 (it carries the swap mode
    and both input amounts). Order is kept."""
    out: list[tuple[str, dict[str, Any]]] = []
    for name, ev in events:
        if out and {out[-1][0], name} == {"EvtSwap", "EvtSwap2"} and _swap_key(out[-1][1]) == _swap_key(ev):
            if name == "EvtSwap2":
                out[-1] = (name, ev)
            continue
        out.append((name, ev))
    return out


# --- math (safeMath / utilsMath / curve) ----------------------------------------------------

def _sub(a: int, b: int) -> int:
    if b > a:
        raise DbcError("SafeMath: subtraction overflow")
    return a - b


def _div(a: int, b: int) -> int:
    if b == 0:
        raise DbcError("SafeMath: division by zero")
    q = abs(a) // abs(b)  # BN division truncates toward zero
    return q if (a >= 0) == (b > 0) else -q


def mul_div(x: int, y: int, d: int, rounding: str) -> int:
    if d == 0:
        raise DbcError("MulDiv: division by zero")
    if d == 1 or x == 0 or y == 0:
        return x * y
    prod = x * y
    return (prod + d - 1) // d if rounding == UP else prod // d


def isqrt(v: int) -> int:
    if v == 0:
        return 0
    if v == 1:
        return 1
    x, y = v, (v + 1) // 2
    while y < x:
        x, y = y, (y + v // y) // 2
    return x


def pow_q64(base: int, exponent: int) -> int:
    if exponent == 0:
        return ONE_Q64
    if base == 0:
        return 0
    if base == ONE_Q64:
        return ONE_Q64
    neg = exponent < 0
    exp = -exponent if neg else exponent
    result, cur = ONE_Q64, base
    while exp:
        if exp & 1:
            result = _div(result * cur, ONE_Q64)
        cur = _div(cur * cur, ONE_Q64)
        exp >>= 1
    return _div(ONE_Q64 * ONE_Q64, result) if neg else result


def delta_base(lower: int, upper: int, liquidity: int, rounding: str) -> int:
    den = lower * upper
    if den == 0:
        raise DbcError("Denominator cannot be zero")
    return mul_div(liquidity, _sub(upper, lower), den, rounding)


def delta_quote(lower: int, upper: int, liquidity: int, rounding: str) -> int:
    prod = liquidity * _sub(upper, lower)
    if rounding == UP:
        den = 1 << (RESOLUTION * 2)
        return (prod + den - 1) // den
    return prod >> (RESOLUTION * 2)


def next_sqrt_price_from_input(sqrt_price: int, liquidity: int, amount_in: int, base_for_quote: bool) -> int:
    if sqrt_price == 0:
        raise DbcError("sqrt_price must be greater than 0")
    if liquidity == 0:
        raise DbcError("liquidity must be greater than 0")
    if base_for_quote:
        if amount_in == 0:
            return sqrt_price
        product = amount_in * sqrt_price
        if product > U128_MAX:
            return _div(liquidity, _div(liquidity, sqrt_price) + amount_in)
        return mul_div(liquidity, sqrt_price, liquidity + product, UP)
    return sqrt_price + _div(amount_in << (RESOLUTION * 2), liquidity)


def next_sqrt_price_from_output(sqrt_price: int, liquidity: int, amount_out: int, base_for_quote: bool) -> int:
    if sqrt_price == 0:
        raise DbcError("sqrt_price must be greater than 0")
    if liquidity == 0:
        raise DbcError("liquidity must be greater than 0")
    if base_for_quote:  # quote out, rounding down
        return _sub(sqrt_price, _div((amount_out << (RESOLUTION * 2)) + liquidity - 1, liquidity))
    if amount_out == 0:  # base out, rounding up
        return sqrt_price
    den = liquidity - amount_out * sqrt_price
    if den <= 0:
        raise DbcError("Invalid denominator: liquidity must be greater than amount * sqrt_price")
    return mul_div(liquidity, sqrt_price, den, UP)


# --- fees (feeMath / poolFees) --------------------------------------------------------------

def to_numerator(bps: int, fee_denominator: int) -> int:
    return mul_div(bps, fee_denominator, MAX_BASIS_POINT, DOWN)


def fee_mode(collect_fee_mode: int, direction: int) -> bool:
    """feesOnInput (the SDK's getFeeMode): only quote -> base with fees in the quote token."""
    return collect_fee_mode != COLLECT_OUTPUT_TOKEN and direction == QUOTE_TO_BASE


def _rate_limiter_max_index(cliff: int, inc_bps: int) -> int:
    if cliff > MAX_FEE_NUMERATOR:
        raise DbcError("Cliff fee numerator exceeds maximum fee numerator")
    inc = to_numerator(inc_bps, FEE_DENOMINATOR)
    if inc == 0:
        raise DbcError("Fee increment numerator cannot be zero")
    return (MAX_FEE_NUMERATOR - cliff) // inc


def rate_limiter_fee_numerator_from_included(cliff: int, reference: int, inc_bps: int, included: int) -> int:
    if included <= reference:
        return cliff
    diff = included - reference
    a, b = diff // reference, diff % reference
    max_index = _rate_limiter_max_index(cliff, inc_bps)
    i = to_numerator(inc_bps, FEE_DENOMINATOR)
    x0 = reference
    if a < max_index:
        num1 = cliff + cliff * a + i * a * (a + 1) // 2
        num2 = cliff + i * (a + 1)
        trading_fee_num = x0 * num1 + b * num2
    else:
        num1 = cliff + cliff * max_index + i * max_index * (max_index + 1) // 2
        left = (a - max_index) * x0 + b
        trading_fee_num = x0 * num1 + left * MAX_FEE_NUMERATOR
    trading_fee = (trading_fee_num + FEE_DENOMINATOR - 1) // FEE_DENOMINATOR
    return mul_div(trading_fee, FEE_DENOMINATOR, included, UP)


def scheduler_fee_numerator(cliff: int, number_of_period: int, period_frequency: int, reduction_factor: int,
                            mode: int, current_point: int, activation_point: int) -> int:
    if period_frequency == 0:
        return cliff
    period = min(_div(current_point - activation_point, period_frequency), number_of_period)
    if period > U16_MAX:
        raise DbcError("Math overflow")
    if mode == FEE_SCHEDULER_LINEAR:
        return _sub(cliff, period * reduction_factor) if period >= 0 else cliff - period * reduction_factor
    if mode == FEE_SCHEDULER_EXPONENTIAL:
        if period == 0:
            return cliff
        base = _sub(ONE_Q64, _div(reduction_factor << 64, MAX_BASIS_POINT))
        return _div(cliff * pow_q64(base, period), ONE_Q64)
    raise DbcError("Invalid fee scheduler mode")


def base_fee_numerator(base_fee: dict[str, Any], current_point: int, activation_point: int, direction: int,
                       included: int) -> int:
    cliff, mode = base_fee["cliff_fee_numerator"], base_fee["base_fee_mode"]
    first, second, third = base_fee["first_factor"], base_fee["second_factor"], base_fee["third_factor"]
    if mode in (FEE_SCHEDULER_LINEAR, FEE_SCHEDULER_EXPONENTIAL):
        return scheduler_fee_numerator(cliff, first, second, third, mode, current_point, activation_point)
    if mode == RATE_LIMITER:  # first = fee increment bps, second = max duration, third = reference amount
        zero = third == 0 and second == 0 and first == 0
        if zero or direction == BASE_TO_QUOTE or current_point > activation_point + second:
            return cliff
        return rate_limiter_fee_numerator_from_included(cliff, third, first, included)
    raise DbcError("Invalid base fee mode")


def variable_fee_numerator(dynamic_fee: dict[str, Any], volatility_accumulator: int) -> int:
    if dynamic_fee["initialized"] == 0:
        return 0
    vb = volatility_accumulator * dynamic_fee["bin_step"]
    v_fee = vb * vb * dynamic_fee["variable_fee_control"]
    return (v_fee + DYNAMIC_FEE_ROUNDING_OFFSET) // DYNAMIC_FEE_SCALING_FACTOR


def total_fee_numerator(config: dict[str, Any], pool: dict[str, Any], current_point: int, included: int,
                        direction: int) -> int:
    fees = config["pool_fees"]
    state = pool["pool_state"]
    base = base_fee_numerator(fees["base_fee"], current_point, state["activation_point"], direction, included)
    total = variable_fee_numerator(fees["dynamic_fee"], state["volatility_tracker"]["volatility_accumulator"]) + base
    return min(total, MAX_FEE_NUMERATOR)


def fee_on_amount(fee_numerator: int, amount: int) -> dict[str, int]:
    trading = mul_div(amount, fee_numerator, FEE_DENOMINATOR, UP)
    after = _sub(amount, trading)
    protocol = mul_div(trading, PROTOCOL_FEE_PERCENT, 100, DOWN)
    return {"amount": after, "trading_fee": trading - protocol, "protocol_fee": protocol, "referral_fee": 0}


# --- curve walk (swapQuote) ------------------------------------------------------------------

def _points(config: dict[str, Any]) -> list[tuple[int, int]]:
    return [(p["sqrt_price"], p["liquidity"]) for p in config["curve"]]


def base_to_quote_from_amount_in(config: dict[str, Any], current: int, amount_in: int) -> tuple[int, int, int]:
    """(output quote, next sqrt price, amount left)."""
    curve = _points(config)
    total, price, left = 0, current, amount_in
    for i in range(len(curve) - 2, -1, -1):
        sp, liq = curve[i]
        if sp == 0 or liq == 0:
            continue
        if sp < price:
            liq_next = curve[i + 1][1]
            max_in = delta_base(sp, price, liq_next, UP)
            if left < max_in:
                nxt = next_sqrt_price_from_input(price, liq_next, left, True)
                total += delta_quote(nxt, price, liq_next, DOWN)
                price, left = nxt, 0
                break
            total += delta_quote(sp, price, liq_next, DOWN)
            price, left = sp, _sub(left, max_in)
    if left:
        liq0 = curve[0][1]
        nxt = next_sqrt_price_from_input(price, liq0, left, True)
        start = config["sqrt_start_price"]
        if nxt < start:
            nxt = start
            left = _sub(left, delta_base(nxt, price, liq0, UP))
        else:
            left = 0
        total += delta_quote(nxt, price, liq0, DOWN)
        price = nxt
    return total, price, left


def quote_to_base_from_amount_in(config: dict[str, Any], current: int, amount_in: int, stop: int) -> tuple[int, int, int]:
    """(output base, next sqrt price, amount left)."""
    if amount_in == 0:
        return 0, current, 0
    total, price, left = 0, current, amount_in
    for sp, liq in _points(config):
        if sp == 0 or liq == 0:
            break
        ref = min(stop, sp)
        if ref > price:
            max_in = delta_quote(price, ref, liq, UP)
            if left < max_in:
                nxt = next_sqrt_price_from_input(price, liq, left, False)
                total += delta_base(price, nxt, liq, DOWN)
                price, left = nxt, 0
                break
            total += delta_base(price, ref, liq, DOWN)
            price, left = ref, _sub(left, max_in)
            if ref == stop:
                break
    return total, price, left


def base_to_quote_from_amount_out(config: dict[str, Any], current: int, amount_out: int) -> tuple[int, int]:
    """(base input needed, next sqrt price) for an exact quote output (swap2 ExactOut)."""
    curve = _points(config)
    total, price, left = 0, current, amount_out
    for i in range(len(curve) - 2, -1, -1):
        sp, liq = curve[i]
        if sp == 0 or liq == 0:
            continue
        if sp < price:
            liq_next = curve[i + 1][1]
            max_out = delta_quote(sp, price, liq_next, DOWN)
            if left < max_out:
                nxt = next_sqrt_price_from_output(price, liq_next, left, True)
                total += delta_base(nxt, price, liq_next, UP)
                price, left = nxt, 0
                break
            total += delta_base(sp, price, liq_next, UP)
            price, left = sp, _sub(left, max_out)
    if left:
        liq0 = curve[0][1]
        start = config["sqrt_start_price"]
        if left > delta_quote(start, price, liq0, DOWN):
            raise DbcError("Insufficient Liquidity")
        nxt = next_sqrt_price_from_output(price, liq0, left, True)
        if nxt < start:
            raise DbcError("Insufficient Liquidity")
        total += delta_base(nxt, price, liq0, UP)
        price = nxt
    return total, price


def quote_to_base_from_amount_out(config: dict[str, Any], current: int, amount_out: int) -> tuple[int, int]:
    """(quote input needed, next sqrt price) for an exact base output (swap2 ExactOut)."""
    total, price, left = 0, current, amount_out
    for sp, liq in _points(config):
        if sp == 0 or liq == 0:
            break
        if sp > price:
            max_out = delta_base(price, sp, liq, DOWN)
            if left < max_out:
                nxt = next_sqrt_price_from_output(price, liq, left, False)
                total += delta_quote(price, nxt, liq, UP)
                price, left = nxt, 0
                break
            total += delta_quote(price, sp, liq, UP)
            price, left = sp, _sub(left, max_out)
    if left:
        raise DbcError("Not enough liquidity")
    return total, price


@dataclass
class Quote:
    amount_in: int  # what the trader pays (fee included)
    actual_input_amount: int  # after an input fee
    output_amount: int  # what the trader receives
    next_sqrt_price: int
    trading_fee: int
    protocol_fee: int
    referral_fee: int


def swap_quote(pool: dict[str, Any], config: dict[str, Any], base_for_quote: bool, amount_in: int,
               current_point: int) -> Quote:
    """Exact-input quote (the SDK's swapQuote, v1, no referral, no first-swap
    discount). Raises DbcError where the SDK throws."""
    state = pool["pool_state"]
    if state["quote_reserve"] >= config["migration_quote_threshold"]:
        raise DbcError("Virtual pool is completed")
    if amount_in == 0:
        raise DbcError("Amount is zero")
    direction = BASE_TO_QUOTE if base_for_quote else QUOTE_TO_BASE
    fees_on_input = fee_mode(config["collect_fee_mode"], direction)
    fee_num = total_fee_numerator(config, pool, current_point, amount_in, direction)
    fee = {"trading_fee": 0, "protocol_fee": 0, "referral_fee": 0}
    actual_in = amount_in
    if fees_on_input:
        fee = fee_on_amount(fee_num, amount_in)
        actual_in = fee["amount"]
    if direction == BASE_TO_QUOTE:
        out, nxt, left = base_to_quote_from_amount_in(config, state["sqrt_price"], actual_in)
    else:
        out, nxt, left = quote_to_base_from_amount_in(config, state["sqrt_price"], actual_in, config["migration_sqrt_price"])
    if left:
        raise DbcError("Insufficient Liquidity")
    if not fees_on_input:
        fee = fee_on_amount(fee_num, out)
        out = fee["amount"]
    return Quote(amount_in, actual_in, out, nxt, fee["trading_fee"], fee["protocol_fee"], fee["referral_fee"])


def price_quote_per_base(sqrt_price: int, base_decimals: int, quote_decimals: int) -> float:
    """Marginal price (quote per whole base token) from a Q64.64 sqrt price."""
    return (sqrt_price / ONE_Q64) ** 2 * 10 ** (base_decimals - quote_decimals)
