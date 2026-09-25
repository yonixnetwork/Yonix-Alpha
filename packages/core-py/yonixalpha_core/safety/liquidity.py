from dataclasses import dataclass
from decimal import Decimal

BPS = Decimal("10000")


@dataclass(frozen=True)
class SimulatedSwap:
    amount_in: Decimal
    amount_out: Decimal
    fee_paid: Decimal
    # Price impact excluding fees, in bps, relative to the pre-trade marginal
    # price. Fees are reported separately so neither hides the other.
    impact_bps: Decimal


@dataclass(frozen=True)
class ConstantProductModel:
    """x*y=k pool with a fee taken on the quote side, which is how pump.fun's
    bonding curve prices trades against its *virtual* reserves (official
    pump-public-docs: buys/sells move virtual_sol_reserves and
    virtual_token_reserves in lockstep with the real ones) and how PumpSwap's
    constant-product pools price against real reserves.

    The math is scale-invariant, so callers pass reserves in the same units
    they size positions in (SOL and whole tokens, after dividing raw amounts
    by 10**decimals). Fees: pump.fun charges its protocol + creator fee on
    the SOL leg, deducted from SOL paid on buys and from SOL received on
    sells.
    """

    quote_reserve: Decimal
    token_reserve: Decimal
    fee_bps: Decimal
    # SOL actually withdrawable (real reserves). For the bonding curve this
    # is smaller than quote_reserve, which includes the virtual offset.
    real_quote_reserve: Decimal | None = None

    def __post_init__(self) -> None:
        if self.quote_reserve <= 0 or self.token_reserve <= 0:
            raise ValueError("reserves must be positive")
        if not (Decimal(0) <= self.fee_bps < BPS):
            raise ValueError("fee_bps must be within [0, 10000)")

    @property
    def marginal_price(self) -> Decimal:
        """Quote per token, pre-trade."""
        return self.quote_reserve / self.token_reserve

    @property
    def liquidity_quote(self) -> Decimal:
        return self.real_quote_reserve if self.real_quote_reserve is not None else self.quote_reserve

    def simulate_buy(self, quote_in: Decimal) -> SimulatedSwap:
        if quote_in <= 0:
            raise ValueError("quote_in must be positive")
        fee = quote_in * self.fee_bps / BPS
        net = quote_in - fee
        tokens_out = self.token_reserve * net / (self.quote_reserve + net)
        avg_price = net / tokens_out
        impact = (avg_price / self.marginal_price - 1) * BPS
        return SimulatedSwap(amount_in=quote_in, amount_out=tokens_out, fee_paid=fee, impact_bps=impact)

    def simulate_sell(self, tokens_in: Decimal) -> SimulatedSwap:
        """Sells against the *current* reserves: what exiting would cost if
        the pool were unchanged at exit time. The honest pre-entry estimate —
        the pool after other traders' activity is unknowable."""
        if tokens_in <= 0:
            raise ValueError("tokens_in must be positive")
        gross = self.quote_reserve * tokens_in / (self.token_reserve + tokens_in)
        if self.real_quote_reserve is not None and gross > self.real_quote_reserve:
            gross = self.real_quote_reserve
        fee = gross * self.fee_bps / BPS
        quote_out = gross - fee
        avg_price = gross / tokens_in
        impact = (1 - avg_price / self.marginal_price) * BPS
        return SimulatedSwap(amount_in=tokens_in, amount_out=quote_out, fee_paid=fee, impact_bps=impact)

    def round_trip(self, quote_in: Decimal) -> tuple[SimulatedSwap, SimulatedSwap, Decimal]:
        """(buy, exit-at-current-reserves, loss_bps). loss_bps combines both
        fees and both impacts — the cost of entering and immediately
        exiting at today's liquidity."""
        buy = self.simulate_buy(quote_in)
        sell = self.simulate_sell(buy.amount_out)
        loss = (1 - sell.amount_out / quote_in) * BPS
        return buy, sell, loss

    def max_size_within(
        self, max_entry_impact_bps: Decimal, max_exit_impact_bps: Decimal, upper_bound: Decimal
    ) -> Decimal:
        """Largest quote size <= upper_bound whose entry and exit impacts both
        stay within limits. Bisection over the model rather than a closed
        form, so any monotone model can reuse the same contract."""
        if upper_bound <= 0:
            return Decimal(0)

        def ok(size: Decimal) -> bool:
            buy = self.simulate_buy(size)
            sell = self.simulate_sell(buy.amount_out)
            return buy.impact_bps <= max_entry_impact_bps and sell.impact_bps <= max_exit_impact_bps

        if ok(upper_bound):
            return upper_bound
        lo, hi = Decimal(0), upper_bound
        for _ in range(60):
            mid = (lo + hi) / 2
            if mid <= 0:
                break
            if ok(mid):
                lo = mid
            else:
                hi = mid
        return lo


@dataclass(frozen=True)
class Fill:
    """One simulated order fill. `quote` is the quote amount exchanged before
    fees (notional); `fee` is charged on top (entry) or deducted (exit)."""

    quantity: Decimal
    quote: Decimal
    fee: Decimal
    avg_price: Decimal
    impact_bps: Decimal
    complete: bool = True


def _curve_open(model: "ConstantProductModel", size: Decimal) -> Fill:
    swap = model.simulate_buy(size)
    net = size - swap.fee_paid
    return Fill(swap.amount_out, net, swap.fee_paid, net / swap.amount_out, swap.impact_bps)


def _curve_close(model: "ConstantProductModel", qty: Decimal) -> Fill:
    swap = model.simulate_sell(qty)
    gross = swap.amount_out + swap.fee_paid
    return Fill(qty, gross, swap.fee_paid, gross / qty, swap.impact_bps)


def open_fill(model, size_quote: Decimal, side: str = "LONG") -> Fill:
    if isinstance(model, ConstantProductModel):
        if side != "LONG":
            raise ValueError("a spot curve can only be bought")
        return _curve_open(model, size_quote)
    return model.open_fill(size_quote, side)


def close_fill(model, qty: Decimal, side: str = "LONG") -> Fill:
    if isinstance(model, ConstantProductModel):
        if side != "LONG":
            raise ValueError("a spot curve can only be sold")
        return _curve_close(model, qty)
    return model.close_fill(qty, side)


def side_costs(model, size_quote: Decimal, side: str = "LONG") -> tuple[Decimal, Decimal]:
    """(entry_cost_bps, exit_cost_bps): impact + fee for opening `size_quote`
    and immediately closing the resulting quantity at today's liquidity."""
    o = open_fill(model, size_quote, side)
    c = close_fill(model, o.quantity, side)
    return o.impact_bps + model.fee_bps, c.impact_bps + model.fee_bps


def max_size_within_side(model, max_entry_bps: Decimal, max_exit_bps: Decimal, upper: Decimal, side: str) -> Decimal:
    if isinstance(model, ConstantProductModel):
        return model.max_size_within(max_entry_bps, max_exit_bps, upper)
    if upper <= 0:
        return Decimal(0)

    def ok(size: Decimal) -> bool:
        o = model.open_fill(size, side)
        if not o.complete:
            return False
        c = model.close_fill(o.quantity, side)
        return c.complete and o.impact_bps <= max_entry_bps and c.impact_bps <= max_exit_bps

    if ok(upper):
        return upper
    lo, hi = Decimal(0), upper
    for _ in range(50):
        mid = (lo + hi) / 2
        if mid <= 0:
            break
        if ok(mid):
            lo = mid
        else:
            hi = mid
    return lo


# Impact reported when the visible book can't absorb the order at all.
BOOK_EXHAUSTED_BPS = BPS


@dataclass(frozen=True)
class OrderBookModel:
    """A central limit order book snapshot: bids descending, asks ascending,
    each (price, base quantity). Fills walk the book level by level. Impact
    is measured against the mid price, so it includes half the spread; the
    fee is the venue's taker rate (market orders) and reported separately.

    A fill larger than the visible depth is marked incomplete and priced at
    BOOK_EXHAUSTED_BPS impact: the gate must treat "the book can't absorb
    this" as a block, never extrapolate beyond what was observed."""

    bids: tuple[tuple[Decimal, Decimal], ...]
    asks: tuple[tuple[Decimal, Decimal], ...]
    fee_bps: Decimal
    band_pct: Decimal = Decimal("0.02")

    def __post_init__(self) -> None:
        if not self.bids or not self.asks:
            raise ValueError("order book side empty")
        if self.bids[0][0] >= self.asks[0][0]:
            raise ValueError("crossed order book")

    @property
    def mid(self) -> Decimal:
        return (self.bids[0][0] + self.asks[0][0]) / 2

    @property
    def marginal_price(self) -> Decimal:
        return self.mid

    @property
    def spread_bps(self) -> Decimal:
        return (self.asks[0][0] - self.bids[0][0]) / self.mid * BPS

    @property
    def liquidity_quote(self) -> Decimal:
        """Executable notional within band_pct of mid on the thinner side."""
        lo, hi = self.mid * (1 - self.band_pct), self.mid * (1 + self.band_pct)
        bid = sum((p * q for p, q in self.bids if p >= lo), Decimal(0))
        ask = sum((p * q for p, q in self.asks if p <= hi), Decimal(0))
        return min(bid, ask)

    @staticmethod
    def _walk_quote(levels, quote_amount: Decimal) -> tuple[Decimal, Decimal, bool]:
        """Spend up to quote_amount; returns (base_qty, quote_spent, complete)."""
        qty = spent = Decimal(0)
        for price, size in levels:
            level_quote = price * size
            if spent + level_quote >= quote_amount:
                take = (quote_amount - spent) / price
                return qty + take, quote_amount, True
            qty += size
            spent += level_quote
        return qty, spent, False

    @staticmethod
    def _walk_base(levels, base_amount: Decimal) -> tuple[Decimal, Decimal, bool]:
        """Trade up to base_amount; returns (base_filled, quote, complete)."""
        qty = quote = Decimal(0)
        for price, size in levels:
            if qty + size >= base_amount:
                take = base_amount - qty
                return base_amount, quote + take * price, True
            qty += size
            quote += size * price
        return qty, quote, False

    def _fill(self, qty: Decimal, quote: Decimal, complete: bool, buying: bool) -> Fill:
        if qty <= 0:
            return Fill(Decimal(0), Decimal(0), Decimal(0), self.mid, BOOK_EXHAUSTED_BPS, False)
        avg = quote / qty
        impact = (avg / self.mid - 1) * BPS if buying else (1 - avg / self.mid) * BPS
        if not complete:
            impact = BOOK_EXHAUSTED_BPS
        return Fill(qty, quote, quote * self.fee_bps / BPS, avg, max(impact, Decimal(0)), complete)

    def open_fill(self, size_quote: Decimal, side: str = "LONG") -> Fill:
        if size_quote <= 0:
            raise ValueError("size must be positive")
        if side == "LONG":
            qty, quote, ok = self._walk_quote(self.asks, size_quote)
            return self._fill(qty, quote, ok, buying=True)
        qty, quote, ok = self._walk_base(self.bids, size_quote / self.mid)
        return self._fill(qty, quote, ok, buying=False)

    def close_fill(self, qty: Decimal, side: str = "LONG") -> Fill:
        if qty <= 0:
            raise ValueError("quantity must be positive")
        if side == "LONG":
            filled, quote, ok = self._walk_base(self.bids, qty)
            return self._fill(filled, quote, ok, buying=False)
        filled, quote, ok = self._walk_base(self.asks, qty)
        return self._fill(filled, quote, ok, buying=True)


def book_from_levels(bids, asks, fee_bps, depth: int = 200) -> OrderBookModel:
    """Builds a model from raw [[price, qty], ...] string/number levels,
    dropping zero-size levels and sorting defensively."""
    b = sorted(((Decimal(str(p)), Decimal(str(q))) for p, q in bids if Decimal(str(q)) > 0), key=lambda x: -x[0])[:depth]
    a = sorted(((Decimal(str(p)), Decimal(str(q))) for p, q in asks if Decimal(str(q)) > 0), key=lambda x: x[0])[:depth]
    return OrderBookModel(tuple(b), tuple(a), Decimal(str(fee_bps)))
