"""Risk summary for the dashboard: every value is what the risk engine itself
computes from the database right now, never a copy of what the page shows.

Per Solana account (PAPER book "solana"; LIVE wallet when configured):
realized PnL today (UTC), the account daily limit (GLOBAL max_daily_loss_quote)
and what is left of it, open-position PnL and planned risk shown apart from
the realized figure, and the account status. Per strategy: its effective
sizing and limits, its own realized PnL today, the blockers that apply before
any token data is fetched, the last decision's sizing outcome, and the size
actually applied to its last trade.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core import kill_switch, rpc_usage
from yonixalpha_core.db.models import PaperPosition, RiskAssessment, RiskSettingsVersion
from yonixalpha_core.safety import store
from yonixalpha_core.safety.gate import LIVE_SUSPENDED_ENGINES, account_blockers
from yonixalpha_core.safety.settings import SafetySettings

SOLANA_ENGINES = ("solana_fresh", "solana_migration", "solana_momentum")

# Hard-block codes -> the status the dashboard shows for a strategy's last decision.
DECISION_STATUS = {
    "TRADE_SIZE_EXCEEDS_RISK": "TRADE SIZE EXCEEDS RISK",
    "FIXED_SIZE_REDUCTION_REQUIRED": "TRADE SIZE EXCEEDS RISK",
    "INSUFFICIENT_BALANCE": "INSUFFICIENT BALANCE",
    "BALANCE_UNAVAILABLE": "INSUFFICIENT BALANCE",
    "RISK_CALCULATION_UNAVAILABLE": "RISK CALCULATION UNAVAILABLE",
    "EQUITY_UNAVAILABLE": "RISK CALCULATION UNAVAILABLE",
    "COSTS_UNDEFINED": "RISK CALCULATION UNAVAILABLE",
    "AUTO_SL_NO_VOLATILITY": "RISK CALCULATION UNAVAILABLE",
    "ENTRY_PRICE_UNAVAILABLE": "RISK CALCULATION UNAVAILABLE",
    "DAILY_PNL_UNAVAILABLE": "RISK CALCULATION UNAVAILABLE",
    "LOSS_UNDEFINED": "RISK CALCULATION UNAVAILABLE",
    "STOP_INSIDE_COSTS": "NO TRADE — UNSAFE EXECUTION CONDITIONS",
    "FIXED_COSTS_TOO_HIGH": "NO TRADE — UNSAFE EXECUTION CONDITIONS",
    "FIXED_COSTS_EXCEED_RISK": "NO TRADE — UNSAFE EXECUTION CONDITIONS",
    "NO_SELL_ROUTE": "NO TRADE — UNSAFE EXECUTION CONDITIONS",
    "VOLATILITY_EXCEEDS_MAX_STOP": "NO TRADE — UNSAFE EXECUTION CONDITIONS",
    "INSUFFICIENT_LIQUIDITY": "NO TRADE — UNSAFE EXECUTION CONDITIONS",
    "INSUFFICIENT_MIGRATED_LIQUIDITY": "NO TRADE — UNSAFE EXECUTION CONDITIONS",
    "DAILY_LOSS_LIMIT": "DAILY LIMIT REACHED",
    "STRATEGY_DAILY_LOSS_LIMIT": "STRATEGY DAILY LIMIT REACHED",
}
SIZING_KEYS = ("trade_size_mode", "fixed_trade_size_sol", "max_loss_per_trade_sol", "fixed_size_policy",
               "risk_per_trade_pct", "max_position_size_quote", "min_position_size_quote", "max_daily_loss_quote",
               "max_open_positions")


def _s(v: Any) -> str | None:
    return None if v is None else str(v)


def _decision_status(codes: list[str]) -> str | None:
    for c in codes:
        if c in DECISION_STATUS:
            return DECISION_STATUS[c]
    return None


async def _last_update(session: AsyncSession, scope: str) -> datetime | None:
    return (await session.execute(select(func.max(RiskSettingsVersion.created_at))
                                  .where(RiskSettingsVersion.scope == scope))).scalar_one()


async def _open_positions(session: AsyncSession, account_id) -> list[PaperPosition]:
    return list((await session.execute(select(PaperPosition).where(
        PaperPosition.account_id == account_id, PaperPosition.status == "open"))).scalars().all())


async def account_summary(session: AsyncSession, redis: Any, account, mode: str, now: datetime,
                          global_settings: SafetySettings) -> dict[str, Any]:
    killed = await kill_switch.is_engaged(redis) if redis is not None else False
    state = await store.account_state(session, account, None, now, killed)
    limit = global_settings.max_daily_loss_quote
    realized = state.daily_realized_pnl or Decimal(0)
    loss_today = max(Decimal(0), -realized)
    opens = await _open_positions(session, account.id)
    # Open positions: marked value + what partial sales already returned - what they cost.
    open_pnl = sum((store.marked_value(p) + (p.proceeds_quote or 0) - (p.entry_cost_quote or 0) for p in opens), Decimal(0))
    planned_open_risk = sum((p.max_loss_quote or Decimal(0) for p in opens), Decimal(0))
    if killed:
        status = "KILL SWITCH ENGAGED"
    elif state.daily_realized_pnl is None:
        status = "RISK CALCULATION UNAVAILABLE"
    elif realized <= -limit:
        status = "DAILY LIMIT REACHED"
    else:
        status = "WITHIN DAILY LIMIT"
    return {
        "account": account.name, "mode": mode, "status": status,
        "cash_sol": _s(account.cash_balance), "available_sol": _s(state.available_balance), "equity_sol": _s(state.equity),
        "realized_pnl_today_sol": _s(realized), "realized_loss_today_sol": _s(loss_today),
        "max_daily_loss_sol": _s(limit), "remaining_daily_loss_budget_sol": _s(max(Decimal(0), limit - loss_today)),
        "open_positions": len(opens), "open_positions_pnl_sol": _s(open_pnl), "open_positions_planned_risk_sol": _s(planned_open_risk),
        "day_starts_at": now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat(),
        "counts_toward_daily_limit": ("realized PnL (net of every fee and cost charged) of positions on this account "
                                      "fully closed since 00:00 UTC, every strategy together; open positions and "
                                      "partial sales of still-open positions do not count until the position closes"),
    }


async def strategy_summary(session: AsyncSession, redis: Any, engine: str, account, now: datetime,
                           account_limit: Decimal) -> dict[str, Any]:
    settings, meta = await store.load_settings(session, engine)
    killed = await kill_switch.is_engaged(redis) if redis is not None else False
    state = await store.account_state(session, account, None, now, killed, engine=engine)
    state = replace(state, account_daily_loss_limit=account_limit, token_exposure=Decimal(0))
    blockers = account_blockers(state, settings, now)
    own = state.strategy_daily_realized_pnl or Decimal(0)
    overrides = await store.engine_overrides(session, engine)
    last = (await session.execute(select(RiskAssessment.evaluated_at, RiskAssessment.decision, RiskAssessment.assessment)
                                  .where(RiskAssessment.engine == engine)
                                  .order_by(RiskAssessment.evaluated_at.desc()).limit(1))).first()
    last_codes = [f.get("code") for f in ((last.assessment or {}).get("findings") or []) if f.get("hard_block")] if last else []
    pos = (await session.execute(select(PaperPosition.entry_at, PaperPosition.entry_cost_quote, PaperPosition.plan,
                                        PaperPosition.execution_mode)
                                 .where(PaperPosition.engine == engine).order_by(PaperPosition.entry_at.desc()).limit(1))).first()
    applied = None
    if pos is not None:
        ps = (pos.plan or {}).get("position_size") or {}
        applied = {"at": pos.entry_at.isoformat(), "mode": pos.execution_mode, "size_sol": ps.get("value"),
                   "method": ps.get("method"), "entry_cost_sol": _s(pos.entry_cost_quote),
                   "planned_loss_at_stop_sol": (pos.plan or {}).get("planned_loss_at_stop"),
                   "token_category": (pos.plan or {}).get("token_category")}
    mode = (await store.load_strategy_mode(session, engine)).value
    errors = meta.get("errors")
    if errors:
        status = "RISK CALCULATION UNAVAILABLE"
    elif blockers:
        status = _decision_status([b.code for b in blockers]) or "NEW ENTRIES BLOCKED"
    else:
        status = "WITHIN DAILY LIMIT"
    eff = {k: _s(getattr(settings, k)) for k in SIZING_KEYS}
    updated = await _last_update(session, engine) or await _last_update(session, store.GLOBAL_SCOPE)
    return {
        "engine": engine, "strategy_mode": mode, "status": status,
        "live_eligible": engine not in LIVE_SUSPENDED_ENGINES,
        "effective": eff,
        "trade_size": (f"{settings.fixed_trade_size_sol} SOL fixed (policy {settings.fixed_size_policy})"
                       if settings.trade_size_mode == "FIXED_SOL"
                       else f"risk-based: equity x {settings.risk_per_trade_pct} at the stop, max {settings.max_position_size_quote} SOL"),
        "own_daily_limit_sol": _s(settings.max_daily_loss_quote), "own_realized_pnl_today_sol": _s(own),
        "own_remaining_daily_loss_budget_sol": _s(max(Decimal(0), settings.max_daily_loss_quote - max(Decimal(0), -own))),
        "blockers": [{"code": b.code, "message": b.message} for b in blockers],
        "engine_values": overrides.get("values") or {}, "settings_source": {k: v for k, v in meta.items() if k != "clamp_notes"},
        "settings_errors": errors,
        "last_settings_update": updated.isoformat() if updated else None,
        "last_decision": ({"at": last.evaluated_at.isoformat(), "decision": last.decision, "blocking_codes": last_codes,
                           "status": _decision_status(last_codes)} if last else None),
        "last_applied_trade": applied,
    }


async def build(session: AsyncSession, redis: Any, now: datetime | None = None, live_configured: bool = False) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    glob, glob_meta = await store.load_settings(session, store.GLOBAL_SCOPE)
    paper = await store.get_paper_account(session, "solana")
    accounts = [await account_summary(session, redis, paper, "PAPER", now, glob)]
    if live_configured:
        from yonixalpha_core.live_trading import get_live_account

        accounts.append(await account_summary(session, redis, await get_live_account(session), "LIVE", now, glob))
    strategies = [await strategy_summary(session, redis, e, paper, now, glob.max_daily_loss_quote) for e in SOLANA_ENGINES]
    blocked = await rpc_usage.entry_blocked_today(redis, now) if redis is not None else {}
    last_global = await _last_update(session, store.GLOBAL_SCOPE)
    return {
        "at": now.isoformat(),
        "global": {k: _s(getattr(glob, k)) for k in SIZING_KEYS} | {"version": glob_meta.get("version"),
                                                                     "last_update": _s(last_global and last_global.isoformat())},
        "accounts": accounts,
        "strategies": strategies,
        "entries_blocked_today": {"total": sum(v for k, v in blocked.items() if k.endswith("|*")),
                                  "by_engine_and_reason": {k: v for k, v in blocked.items() if not k.endswith("|*")}},
        "notes": ["A stop is not a guaranteed maximum loss: gaps, liquidity loss and failed sells can lose more.",
                  "Blocked entries are evaluations refused before any token data was fetched (not unique tokens)."],
    }
