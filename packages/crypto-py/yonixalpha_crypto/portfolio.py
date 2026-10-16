"""Portfolio-wide risk: limits over EVERY strategy and mode at once, checked
before any new entry (paper or exchange-demo), so several strategies that
signal together cannot multiply the risk or pile into the same coin.

Measured from the records, not estimated:
- open risk = sum over open positions of qty x (entry - stop): paper
  positions (demo_positions) plus active exchange-demo trades (planned loss);
- exposure = open entry cost, in total and per BASE asset (BTC, ETH, ...),
  so three BTC strategies count as one BTC exposure;
- daily loss = realized P&L closed today (UTC), all strategies;
- drawdown = fall of the paper account equity (starting balance + realized
  P&L of closed positions, in time order) from its high-water mark.
When the drawdown limit is reached the Crypto Trading emergency stop is set
(an automatic risk intervention, audited and raised as an ERROR event): no
new entries; open positions keep their protection. The operator releases it.

Also stored here: the switches for optional heavy work (backtests and
parameter optimisation), off unless the operator turns them on.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_crypto.db import (EXEC_ACTIVE, ConfigAudit, DemoAccount, DemoPosition, Event, ExecTrade,
                                  PortfolioSettings)
from yonixalpha_crypto.exchanges.base import split_symbol

D = Decimal
# key -> (default, lo, hi); booleans are (default, None, None)
RULES: dict[str, tuple[Any, Any, Any]] = {
    "max_portfolio_risk_quote": (D("50"), D("1"), D("100000")),
    "max_open_positions": (D("5"), D("1"), D("50")),
    "max_total_exposure_quote": (D("600"), D("1"), D("1000000")),
    "max_exposure_per_asset_quote": (D("300"), D("1"), D("1000000")),
    "max_daily_loss_quote": (D("40"), D("1"), D("100000")),
    "max_drawdown_pct": (D("20"), D("1"), D("90")),
    "backtests_enabled": (False, None, None),
    "optimization_enabled": (True, None, None),
}


class PortfolioError(ValueError):
    pass


def defaults() -> dict[str, Any]:
    return {k: (str(v[0]) if isinstance(v[0], Decimal) else v[0]) for k, v in RULES.items()}


async def _row(s: AsyncSession, lock: bool = False) -> PortfolioSettings:
    q = select(PortfolioSettings).where(PortfolioSettings.id == 1)
    row = (await s.execute(q.with_for_update() if lock else q)).scalar_one_or_none()
    if row is None:
        row = PortfolioSettings(id=1, limits=defaults(), version=1, updated_at=datetime.now(timezone.utc))
        s.add(row)
        await s.flush()
    return row


async def load(s: AsyncSession) -> dict[str, Any]:
    row = await _row(s)
    return {**defaults(), **(row.limits or {}), "version": row.version}


def validate(change: dict[str, Any]) -> dict[str, Any]:
    out, errors = {}, []
    for k, v in change.items():
        rule = RULES.get(k)
        if rule is None:
            errors.append(f"{k}: unknown setting")
            continue
        if rule[1] is None:
            if not isinstance(v, bool):
                errors.append(f"{k}: true or false")
            else:
                out[k] = v
            continue
        try:
            x = D(str(v))
            if not x.is_finite() or not rule[1] <= x <= rule[2]:
                raise ValueError
            if k == "max_open_positions" and x != x.to_integral_value():
                raise ValueError
            out[k] = str(x)
        except (InvalidOperation, ValueError):
            errors.append(f"{k}: must be between {rule[1]} and {rule[2]}")
    if errors:
        raise PortfolioError("; ".join(errors))
    return out


async def save(s: AsyncSession, change: dict[str, Any], expected_version: int, actor: str | None) -> dict[str, Any]:
    row = await _row(s, lock=True)
    if row.version != expected_version:
        raise PortfolioError(f"changed elsewhere (version {row.version}, you sent {expected_version}); reload")
    before = {**(row.limits or {})}
    row.limits = {**defaults(), **before, **validate(change)}
    row.version += 1
    row.updated_at, row.updated_by = datetime.now(timezone.utc), actor
    s.add(ConfigAudit(actor=actor, scope="portfolio", before=before, after=row.limits))
    await s.flush()
    return {**row.limits, "version": row.version}


async def state(s: AsyncSession, now: datetime) -> dict[str, Any]:
    """The live portfolio picture the limits are checked against."""
    P = DemoPosition
    open_paper = (await s.execute(select(P).where(P.status == "OPEN"))).scalars().all()
    open_exec = (await s.execute(select(ExecTrade).where(ExecTrade.status.in_(EXEC_ACTIVE)))).scalars().all()
    per_asset: dict[str, D] = {}
    risk = expo = D(0)
    positions = []
    for p in open_paper:
        base, _ = split_symbol(p.symbol)
        r = max(D(0), D(p.qty) * (D(p.entry_price) - D(p.stop_price))) if p.stop_price is not None else D(p.entry_cost_quote)
        risk += r
        expo += D(p.entry_cost_quote)
        per_asset[base] = per_asset.get(base, D(0)) + D(p.entry_cost_quote)
        positions.append({"mode": "PAPER", "strategy_id": p.strategy_id, "exchange": p.exchange, "symbol": p.symbol,
                          "qty": str(p.qty), "entry": str(p.entry_price), "stop": str(p.stop_price), "risk": str(r),
                          "exposure": str(p.entry_cost_quote)})
    for t in open_exec:
        base, _ = split_symbol(t.symbol)
        cost = D(t.qty or t.qty_planned) * D(t.entry_price or t.stop)
        risk += D(t.planned_loss)
        expo += cost
        per_asset[base] = per_asset.get(base, D(0)) + cost
        positions.append({"mode": f"EXCHANGE_DEMO ({t.environment})", "strategy_id": t.strategy_id,
                          "exchange": t.exchange, "symbol": t.symbol, "qty": str(t.qty), "entry": str(t.entry_price),
                          "stop": str(t.stop), "risk": str(t.planned_loss), "exposure": str(cost)})
    day = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    realized_today = D(await s.scalar(select(func.coalesce(func.sum(P.realized_pnl_quote), 0)).where(
        P.status == "CLOSED", P.closed_at >= day)) or 0)
    realized_today += D(await s.scalar(select(func.coalesce(func.sum(ExecTrade.realized_quote), 0)).where(
        ExecTrade.closed_at >= day)) or 0)
    start = D(await s.scalar(select(func.coalesce(func.sum(DemoAccount.starting_balance), 0))) or 0)
    closed = (await s.execute(select(P.realized_pnl_quote).where(P.status == "CLOSED").order_by(P.closed_at))).scalars()
    eq = peak = start
    dd_pct = D(0)
    for pnl in closed:
        eq += D(pnl or 0)
        peak = max(peak, eq)
        if peak > 0:
            dd_pct = max(dd_pct, (peak - eq) / peak * 100) if eq < peak else dd_pct
    current_dd = (peak - eq) / peak * 100 if peak > 0 else D(0)
    return {"open_positions": len(positions), "open_risk_quote": str(risk), "total_exposure_quote": str(expo),
            "exposure_per_asset_quote": {k: str(v) for k, v in sorted(per_asset.items())},
            "realized_today_quote": str(realized_today), "paper_equity_quote": str(eq), "paper_equity_peak": str(peak),
            "current_drawdown_pct": str(round(current_dd, 4)), "max_drawdown_pct_seen": str(round(dd_pct, 4)),
            "positions": positions}


async def check_entry(s: AsyncSession, *, symbol: str, planned_loss: D, notional: D, now: datetime,
                      actor: str = "risk:portfolio") -> list[str]:
    """Reasons the portfolio refuses this entry ([] = allowed). Sets the
    emergency stop when the drawdown limit is reached."""
    lim = await load(s)
    st = await state(s, now)
    base, _ = split_symbol(symbol)
    reasons = []
    if st["open_positions"] + 1 > int(D(lim["max_open_positions"])):
        reasons.append(f"portfolio: {st['open_positions']} open positions, max {lim['max_open_positions']}")
    if D(st["open_risk_quote"]) + planned_loss > D(lim["max_portfolio_risk_quote"]):
        reasons.append(f"portfolio: open risk {D(st['open_risk_quote']):.2f} + {planned_loss:.2f} above "
                       f"{lim['max_portfolio_risk_quote']}")
    if D(st["total_exposure_quote"]) + notional > D(lim["max_total_exposure_quote"]):
        reasons.append(f"portfolio: exposure {D(st['total_exposure_quote']):.2f} + {notional:.2f} above "
                       f"{lim['max_total_exposure_quote']}")
    asset = D(st["exposure_per_asset_quote"].get(base, "0"))
    if asset + notional > D(lim["max_exposure_per_asset_quote"]):
        reasons.append(f"portfolio: {base} exposure {asset:.2f} + {notional:.2f} above "
                       f"{lim['max_exposure_per_asset_quote']} (all strategies together)")
    if -D(st["realized_today_quote"]) >= D(lim["max_daily_loss_quote"]):
        reasons.append(f"portfolio: today's loss {-D(st['realized_today_quote']):.2f} reached "
                       f"{lim['max_daily_loss_quote']}")
    if D(st["current_drawdown_pct"]) >= D(lim["max_drawdown_pct"]):
        reasons.append(f"portfolio: drawdown {st['current_drawdown_pct']}% reached {lim['max_drawdown_pct']}%: "
                       "emergency stop set")
        from yonixalpha_crypto import config_store
        await config_store.set_engine(s, enabled=None, emergency_stop=True,
                                      note=f"automatic: drawdown {st['current_drawdown_pct']}%", actor=actor)
        s.add(Event(at=now, level="ERROR", source="risk",
                    message=f"drawdown shutdown: {st['current_drawdown_pct']}% >= {lim['max_drawdown_pct']}%; "
                            "Crypto Trading emergency stop set (open positions keep their protection)"))
    return reasons
