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
