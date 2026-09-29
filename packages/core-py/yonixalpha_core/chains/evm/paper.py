"""EVM paper trading (BSC, Robinhood Chain) through the shared pipeline.

Entry: every blocker is explicit and recorded on the token
(evm_tokens.extra.entry_decision): kill switch, operator controls, the
launchpad's evidence-based status (paper needs PAPER ONLY or LIVE), the
token category and its trade signal, a fresh safety PASS, liquidity, and
account limits. A token that passes is sized by the same plan_trade the
Solana gate uses, with the chain's own amounts (BNB / ETH, never SOL) and
the executable round trip measured on the launchpad's contracts.

Fill: the executable buy quote at the planned size (tokens out), so the
entry price is the all-in cost per token. Management: every tick marks the
position at the executable SELL quote of its remaining tokens (net of fees
and taxes) and runs the shared paper_engine.apply_step (stop, take-profits,
breakeven move, trailing) with exit costs already in that price.

A migration is followed automatically: the adapter quotes the DEX once the
token leaves its curve, the position row stays the same, and a
venue_switched timeline event records the change. A position whose exit
cannot be quoted is never marked at a made-up price: it stays open,
UNPRICED, and is retried.

LIVE is never used here: EVM execution is not implemented in this phase.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core import kill_switch, paper_engine
from yonixalpha_core.chains import controls, verification
from yonixalpha_core.chains.base import Quote
from yonixalpha_core.chains.evm.settings import EvmTradingSettings
from yonixalpha_core.chains.evm.settings import load as evm_settings_load
from yonixalpha_core.db.models import EvmToken, PaperAccount, PaperPosition
from yonixalpha_core.safety.models import AccountState, ExecutionQuote, FinalDecision, ManualOverrides, Observation, Venue
from yonixalpha_core.safety.planning import plan_trade
from yonixalpha_core.safety.store import add_timeline_event, load_settings, settings_block_reason

NATIVE = {"bsc": "BNB", "robinhood": "ETH"}
STARTING_BALANCE = {"bsc": Decimal("1"), "robinhood": Decimal("0.3")}
SAFETY_MAX_AGE = timedelta(minutes=5)
E18 = Decimal(10) ** 18


def engine_for(chain: str) -> str:
    return f"evm_{chain}"


def is_evm_engine(engine: str | None) -> bool:
    return bool(engine) and engine.startswith("evm_")


async def ensure_account(session: AsyncSession, chain: str, name: str | None = None) -> PaperAccount:
    name = name or engine_for(chain)
    acct = (await session.execute(select(PaperAccount).where(PaperAccount.name == name))).scalar_one_or_none()
    if acct is None:
        acct = PaperAccount(name=name, quote_currency=NATIVE[chain], starting_balance=STARTING_BALANCE[chain],
                            cash_balance=STARTING_BALANCE[chain])
        session.add(acct)
        await session.flush()
    return acct


@dataclass
class EntryDecision:
    chain: str
    token: str
    launchpad: str
    category: str
    at: datetime
    blockers: list[dict[str, Any]] = field(default_factory=list)
    plan: Any = None
    buy: Quote | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.blockers and self.plan is not None

    def block(self, code: str, message: str) -> None:
        self.blockers.append({"code": code, "message": message})

    def to_dict(self) -> dict[str, Any]:
        return {"at": self.at.isoformat(), "decision": "PAPER_BUY" if self.ok else "NO_TRADE",
                "category": self.category, "blockers": self.blockers,
                "plan": self.plan.to_dict() if self.plan is not None else None,
                "detail": {k: (str(v) if isinstance(v, Decimal) else v) for k, v in self.detail.items()}}


def _signal(d: EntryDecision, row: EvmToken, s: EvmTradingSettings) -> None:
    st = row.stats or {}
    ratio = Decimal(st["net_buy_ratio"]) if st.get("net_buy_ratio") else None
    need = {"FRESH": (s.fresh_min_buys, s.fresh_min_unique_buyers),
            "MOMENTUM": (s.momentum_min_buys, s.momentum_min_unique_buyers),
            "MIGRATED": (s.fresh_min_buys, s.fresh_min_unique_buyers)}.get(row.category)
    if need is None:
        d.block("CATEGORY_NOT_TRADED", f"category {row.category} has no entry strategy")
        return
    if st.get("buys", 0) < need[0]:
        d.block("TOO_FEW_BUYS", f"{st.get('buys', 0)} buys in {st.get('window_s')}s < {need[0]}")
    if st.get("unique_buyers", 0) < need[1]:
        d.block("TOO_FEW_BUYERS", f"{st.get('unique_buyers', 0)} distinct buyers < {need[1]}")
    if ratio is None or ratio < s.min_net_buy_ratio:
        d.block("SELLING_PRESSURE", f"buy share of volume {ratio} < {s.min_net_buy_ratio}")


async def account_state(session: AsyncSession, redis, chain: str, token: str, now: datetime,
                        blocked_by: str | None, engine: str | None = None) -> tuple[AccountState, PaperAccount]:
    eng = engine or engine_for(chain)
    acct = await ensure_account(session, chain, eng)
    pp = PaperPosition
    open_rows = (await session.execute(select(pp).where(pp.engine == eng, pp.status == "open"))).scalars().all()
    exposure = sum((p.entry_cost_quote or Decimal(0)) for p in open_rows)
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    daily = (await session.execute(select(func.coalesce(func.sum(pp.realized_pnl), 0)).where(
        pp.engine == eng, pp.status == "closed", pp.exit_at >= day))).scalar_one()
    last_loss = (await session.execute(select(func.max(pp.exit_at)).where(
        pp.engine == eng, pp.status == "closed", pp.realized_pnl < 0))).scalar_one()
    token_exp = sum((p.entry_cost_quote or Decimal(0)) for p in open_rows if (p.asset_id or "").lower() == token.lower())
    return AccountState(equity=acct.cash_balance + exposure, available_balance=acct.cash_balance,
                        open_positions=len(open_rows), current_exposure=exposure, daily_realized_pnl=Decimal(daily),
                        last_loss_at=last_loss, token_exposure=token_exp,
                        kill_switch_engaged=await kill_switch.is_engaged(redis) if redis is not None else False,
                        trading_blocked_by=blocked_by), acct


async def evaluate_entry(session: AsyncSession, redis, adapter, row: EvmToken, s: EvmTradingSettings, now: datetime,
                         source: str = "sniper") -> EntryDecision:
    spec, chain = adapter.spec, adapter.spec.chain.value
    cs = s.chain(chain)
    d = EntryDecision(chain, row.token, spec.key, row.category, now)
    ctl = await controls.load(session)
    blocked = controls.blocked_by(ctl, spec.chain, source)
    acct_state, _acct = await account_state(session, redis, chain, row.token, now, blocked)
    if acct_state.kill_switch_engaged:
        d.block("KILL_SWITCH", "global kill switch engaged")
    if blocked:
        d.block("TRADING_CONTROL_OFF", blocked)
    mode = controls.launchpad_mode(ctl, spec.key, spec.chain)
    st = await verification.status_for(session, redis, spec, mode, now)
    d.detail["launchpad_status"] = st["status"]
    if not st["paper_allowed"]:
        d.block("LAUNCHPAD_NOT_VERIFIED", f"{spec.name} is {st['status']}: {st['why']}")
    if not cs.paper_entries_enabled:
        d.block("PAPER_ENTRIES_OFF", f"paper entries disabled for {chain}")
    if row.category not in s.entry_categories:
        d.block("CATEGORY_DISABLED", f"entries for {row.category} are switched off")
    _signal(d, row, s)
    ok_verdicts = ("PASS", "WARN") if s.allow_safety_warn else ("PASS",)
    if row.safety_verdict not in ok_verdicts:
        d.block("SAFETY_NOT_PASSED", f"safety verdict {row.safety_verdict or 'not checked'}")
    elif row.safety_at is None or now - row.safety_at > SAFETY_MAX_AGE:
        d.block("SAFETY_STALE", "safety check older than 5 minutes")
    liq = Decimal(str((row.state or {}).get("liquidity_quote") or 0))
    if liq < cs.min_liquidity:
        d.block("LIQUIDITY_TOO_LOW", f"{liq} {NATIVE[chain]} < {cs.min_liquidity}")
    if acct_state.open_positions >= cs.max_open_positions:
        d.block("MAX_OPEN_POSITIONS", f"{acct_state.open_positions} open on {chain}")
    if acct_state.current_exposure + cs.position_size > cs.max_total_exposure:
        d.block("MAX_EXPOSURE", f"exposure {acct_state.current_exposure} + {cs.position_size} > {cs.max_total_exposure}")
    if acct_state.daily_realized_pnl <= -cs.max_daily_loss:
        d.block("DAILY_LOSS_LIMIT", f"realized today {acct_state.daily_realized_pnl} <= -{cs.max_daily_loss}")
    recent = (await session.execute(select(func.count()).where(
        PaperPosition.engine == engine_for(chain), func.lower(PaperPosition.asset_id) == row.token.lower(),
        PaperPosition.entry_at >= now - timedelta(hours=s.reentry_cooldown_hours)))).scalar_one()
    if recent:
        d.block("ALREADY_TRADED", f"a position on this token was opened in the last {s.reentry_cooldown_hours}h")
    if d.blockers:
        return d  # no quote is spent on a token that cannot be entered anyway

    await build_plan(session, adapter, row, cs.position_size, acct_state, liq, now, d)
    return d


async def build_plan(session: AsyncSession, adapter, row: EvmToken, size: Decimal, acct_state: AccountState,
                     liq: Decimal, now: datetime, d: EntryDecision, max_total_exposure: Decimal | None = None) -> None:
    """Quotes the executable round trip at `size` (native units) and sizes
    the trade with the shared plan_trade. Fills d.plan / d.buy, or adds
    blockers. Shared by automatic entries and copy trading."""
    chain = adapter.spec.chain.value
    cs = (await evm_settings_load(session)).chain(chain)
    size_wei = int(size * E18)
    buy = await adapter.quote_buy(row.token, size_wei)
    sell = await adapter.quote_sell(row.token, buy.amount_out) if buy.ok and buy.amount_out else None
    if not buy.ok or sell is None or not sell.ok:
        d.block("NO_EXECUTABLE_ROUND_TRIP", f"buy: {buy.error or 'ok'}; sell: {(sell.error if sell else 'not quoted') or 'ok'}")
        return
    spent = Decimal(buy.amount_in) / E18  # a curve may fill less than offered
    tokens = Decimal(buy.amount_out) / E18
    entry_price = spent / tokens
    back = Decimal(sell.amount_out) / E18
    rt_bps = (1 - back / spent) * 10_000
    d.detail.update(entry_price=entry_price, round_trip_loss_bps=rt_bps.quantize(Decimal("0.1")), buy_source=buy.source,
                    sell_source=sell.source, quotes_exact=buy.exact and sell.exact)
    vol = Decimal((row.stats or {})["volatility"]) if (row.stats or {}).get("volatility") else None

    settings, _meta = await load_settings(session, engine_for(chain))
    invalid = settings_block_reason(engine_for(chain), _meta)
    if invalid:
        d.block("RISK_SETTINGS_INVALID", invalid)
        return
    settings = replace(settings, max_position_size_quote=spent, min_position_size_quote=spent / 10,
                       max_total_exposure_quote=max_total_exposure or cs.max_total_exposure, max_token_exposure_quote=spent,
                       max_daily_loss_quote=cs.max_daily_loss, min_liquidity_quote=cs.min_liquidity)
    q = ExecutionQuote(observation=Observation(buy.source, now), venue=Venue.UNKNOWN, size_quote=spent,
                       buy_route_available=True, sell_route_available=True, entry_impact_bps=Decimal(0),
                       exit_impact_bps=rt_bps, round_trip_loss_bps=rt_bps, fee_bps_per_side=Decimal(0),
                       expected_entry_price=entry_price, expected_exit_price=back / tokens)
    plan = plan_trade(entry_price=entry_price, volatility=vol, liquidity_quote=liq, settings=settings,
                      account=acct_state, overrides=ManualOverrides(), model=None, quote=q, transfer_fee_bps=None)
    for f in plan.findings:
        if f.action == FinalDecision.NO_TRADE or f.hard_block:
            d.block(f.code, f.message)
    if not plan.complete:
        if not d.blockers:
            d.block("PLAN_INCOMPLETE", "risk plan could not be completed")
        return
    if plan.stop_distance_pct is not None and rt_bps / 10_000 >= plan.stop_distance_pct / 2:
        d.block("ROUND_TRIP_EXCEEDS_STOP_BUDGET",
                f"round-trip cost {rt_bps / 100:.2f}% uses half or more of the {plan.stop_distance_pct:.2%} stop")
        return
    if plan.position_size.value < spent:  # the planner sized it down: fill at that size
        buy = await adapter.quote_buy(row.token, int(plan.position_size.value * E18))
        if not buy.ok:
            d.block("NO_EXECUTABLE_QUOTE", buy.error or "buy quote failed at the planned size")
            return
    d.plan, d.buy = plan, buy


async def open_position(session: AsyncSession, d: EntryDecision, row: EvmToken, now: datetime,
                        engine: str | None = None) -> PaperPosition:
    """Opens the paper position a passing decision describes. The partial
    unique index uq_paper_positions_evm_open makes a duplicate impossible.
    Caller commits."""
    assert d.ok and d.buy is not None
    engine = engine or engine_for(d.chain)
    acct = await ensure_account(session, d.chain, engine)
    cost = Decimal(d.buy.amount_in) / E18
    qty = Decimal(d.buy.amount_out) / E18
    if cost > acct.cash_balance:
        raise paper_engine.FillError(f"required {cost} exceeds paper cash {acct.cash_balance}")
    acct.cash_balance -= cost
    plan = d.plan
    price = cost / qty
    venue = {"kind": "spot", "chain": d.chain, "launchpad": d.launchpad, "token": row.token, "route": d.buy.route,
             "quote_source": d.buy.source, "simulator": paper_engine.PAPER_SIMULATOR_VERSION,
             "pricing": "executable sell quote of the remaining tokens (net of fees and taxes)"}
    p = PaperPosition(
        symbol=(row.symbol or row.token)[:64], provider="paper", side="LONG", entry_price=price, quantity=qty,
        stop_loss=plan.stop_loss.value, take_profit=[str(tp.price.value) for tp in plan.take_profits], entry_at=now,
        status="open", account_id=acct.id, engine=engine, asset_id=row.token,
        initial_quantity=qty, remaining_quantity=qty, entry_cost_quote=cost, proceeds_quote=Decimal(0),
        fees_paid_quote=Decimal(d.buy.fee or 0) / E18, max_loss_quote=plan.max_loss.value,
        plan={**plan.to_dict(), "venue": venue, "exit_cost_bps": "0", "category": row.category},
        tp_hits=[], highest_price=price, lowest_price=price, last_price=price, last_marked_at=now,
    )
    session.add(p)
    await session.flush()
    await add_timeline_event(session, "paper_entry", now, {
        "chain": d.chain, "launchpad": d.launchpad, "category": row.category, "size": str(cost), "quantity": str(qty),
        "fill_price": str(price), "route": d.buy.route, "quote_source": d.buy.source,
        "stop": str(plan.stop_loss.value), "round_trip_loss_bps": str(d.detail.get("round_trip_loss_bps"))},
        position_id=p.id)
    return p


async def mark_price(adapter, p: PaperPosition) -> tuple[Decimal | None, Quote]:
    """Effective per-token price of selling the remaining tokens now."""
    remaining = p.remaining_quantity if p.remaining_quantity is not None else p.quantity
    q = await adapter.quote_sell(p.asset_id, int(remaining * E18))
    if not q.ok or not q.amount_out or remaining <= 0:
        return None, q
    return (Decimal(q.amount_out) / E18) / remaining, q


async def manage_position(session: AsyncSession, adapter, p: PaperPosition, now: datetime,
                          extra_exit: tuple[Decimal, str] | None = None) -> dict[str, Any]:
    """One management tick for an open EVM paper position (`extra_exit`: an
    additional partial exit, e.g. a mirrored copy-target sell). Caller commits."""
    price, q = await mark_price(adapter, p)
    if price is None:
        return {"status": "UNPRICED", "error": q.error, "source": q.source}
    venue = dict((p.plan or {}).get("venue") or {})
    if q.route and venue.get("route") and q.route != venue["route"]:
        await add_timeline_event(session, "venue_switched", now, {"from": venue["route"], "to": q.route,
                                                                  "source": q.source}, position_id=p.id)
        venue["route"] = q.route
        p.plan = {**(p.plan or {}), "venue": venue}
    acct = await session.get(PaperAccount, p.account_id)
    result = await paper_engine.apply_step(session, p, acct, price, None, None, now, exit_cost_bps=Decimal(0),
                                           extra_exit=extra_exit)
    return {"status": "CLOSED" if result.closed else "OPEN", "price": price, "exits": [r for _, r in result.exits],
            "route": q.route}
