"""Database access for the safety gate: runtime settings, modes, operator
rules, paper account state, and persisted assessments.

Every read fails safe: a missing settings row means the code defaults, a
missing mode row means PAPER, and nothing read from the database can grant
live execution — that needs the environment flags too (see
live_trading_permitted).
"""

import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core.db.models import (
    AuditLog,
    BlacklistEntry,
    CustomRuleEntry,
    PaperAccount,
    PaperPosition,
    PlatformSetting,
    RiskAssessment,
    RiskSettingsVersion,
    StrategyConfig,
    TradeTimelineEvent,
)
from yonixalpha_core.safety.gate import Assessment
from yonixalpha_core.safety.models import AccountState, GlobalMode, StrategyMode
from yonixalpha_core.safety.rules import BlacklistRule, CustomRule
from yonixalpha_core.safety.settings import (
    SafetySettings,
    clamp,
    default_settings_for,
    settings_from_dict,
    settings_to_dict,
    validate,
)

GLOBAL_SCOPE = "GLOBAL"
GLOBAL_MODE_KEY = "global_mode"

# One paper book per quote currency. Starting balances are simulation
# parameters chosen by the operator (editable via the API), not market data.
DEFAULT_PAPER_ACCOUNTS = {
    "solana": ("SOL", Decimal("10")),
    "binance_futures": ("USDT", Decimal("1000")),
}
ENGINE_ACCOUNT = {
    "solana_fresh": "solana",
    "solana_migration": "solana",
    "solana_momentum": "solana",
    "binance_futures": "binance_futures",
}


class SettingsError(ValueError):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


def live_trading_permitted(app_settings: Any) -> bool:
    """All three environment locks must agree. The database has no say."""
    return bool(
        getattr(app_settings, "TRADING_ENABLED", False)
        and getattr(app_settings, "LIVE_TRADING_ENABLED", False)
        and not getattr(app_settings, "PAPER_TRADING", True)
    )


async def _latest_settings_row(session: AsyncSession, scope: str) -> RiskSettingsVersion | None:
    result = await session.execute(
        select(RiskSettingsVersion).where(RiskSettingsVersion.scope == scope).order_by(RiskSettingsVersion.version.desc()).limit(1)
    )
    return result.scalar_one_or_none()


async def load_settings(session: AsyncSession, engine: str) -> tuple[SafetySettings, dict[str, Any]]:
    """Effective settings for `engine`: its own latest version if one exists,
    else the GLOBAL latest, else the engine's code defaults. Always re-clamped, so a row
    written before a hard limit was tightened can't bypass it."""
    row = await _latest_settings_row(session, engine) or await _latest_settings_row(session, GLOBAL_SCOPE)
    if row is None:
        settings, notes = clamp(default_settings_for(engine))
        return settings, {"scope": "DEFAULT", "version": 0, "clamp_notes": notes}
    settings, notes = clamp(settings_from_dict(row.settings))
    errors = validate(settings)
    if errors:
        # A stored row that no longer validates is not silently used.
        settings, _ = clamp(default_settings_for(engine))
        return settings, {"scope": "DEFAULT", "version": 0, "rejected_row": str(row.id), "errors": errors}
    return settings, {"scope": row.scope, "version": row.version, "id": str(row.id), "clamp_notes": notes}


async def save_settings(
    session: AsyncSession, scope: str, data: dict[str, Any], user_id: uuid.UUID | None, note: str | None = None
) -> tuple[RiskSettingsVersion, list[str]]:
    """Validates, clamps, and appends a new version. Returns the row and the
    clamp notes (values that were bounded). Raises SettingsError when the
    settings are inconsistent — nothing is written then."""
    try:
        parsed = settings_from_dict(data)
    except (ValueError, TypeError, ArithmeticError) as exc:
        raise SettingsError([f"invalid value: {exc}"]) from exc
    settings, notes = clamp(parsed)
    errors = validate(settings)
    if errors:
        raise SettingsError(errors)
    latest = await _latest_settings_row(session, scope)
    previous = latest.settings if latest else None
    row = RiskSettingsVersion(
        scope=scope,
        version=(latest.version + 1) if latest else 1,
        settings=settings_to_dict(settings),
        note=note,
        created_by=user_id,
    )
    session.add(row)
    session.add(
        AuditLog(
            user_id=user_id,
            event_type="risk_settings.updated",
            detail={
                "scope": scope,
                "version": row.version,
                "changed": _diff(previous, row.settings),
                "clamp_notes": notes,
                "note": note,
            },
        )
    )
    await session.flush()
    return row, notes


def _diff(before: dict | None, after: dict) -> dict[str, Any]:
    if before is None:
        return {k: {"from": None, "to": v} for k, v in after.items()}
    return {k: {"from": before.get(k), "to": v} for k, v in after.items() if before.get(k) != v}


async def load_global_mode(session: AsyncSession) -> GlobalMode:
    row = await session.get(PlatformSetting, GLOBAL_MODE_KEY)
    try:
        return GlobalMode((row.value or {}).get("mode")) if row else GlobalMode.PAPER
    except ValueError:
        return GlobalMode.PAPER


async def set_global_mode(session: AsyncSession, mode: GlobalMode, user_id: uuid.UUID | None) -> None:
    before = await load_global_mode(session)
    stmt = (
        insert(PlatformSetting)
        .values(key=GLOBAL_MODE_KEY, value={"mode": mode.value}, updated_by=user_id)
        .on_conflict_do_update(
            index_elements=["key"], set_={"value": {"mode": mode.value}, "updated_by": user_id, "updated_at": func.now()}
        )
    )
    await session.execute(stmt)
    session.add(AuditLog(user_id=user_id, event_type="global_mode.changed", detail={"from": before.value, "to": mode.value}))


async def load_strategy_mode(session: AsyncSession, strategy: str) -> StrategyMode:
    row = await session.get(StrategyConfig, strategy)
    try:
        return StrategyMode(row.mode) if row else StrategyMode.PAPER
    except ValueError:
        return StrategyMode.PAPER


async def set_strategy_mode(session: AsyncSession, strategy: str, mode: StrategyMode, user_id: uuid.UUID | None) -> None:
    before = await load_strategy_mode(session, strategy)
    stmt = (
        insert(StrategyConfig)
        .values(strategy=strategy, mode=mode.value, config={}, updated_by=user_id)
        .on_conflict_do_update(
            index_elements=["strategy"], set_={"mode": mode.value, "updated_by": user_id, "updated_at": func.now()}
        )
    )
    await session.execute(stmt)
    session.add(
        AuditLog(user_id=user_id, event_type="strategy_mode.changed", detail={"strategy": strategy, "from": before.value, "to": mode.value})
    )


async def load_blacklist(session: AsyncSession) -> list[BlacklistRule]:
    rows = (await session.execute(select(BlacklistEntry).where(BlacklistEntry.enabled.is_(True)))).scalars().all()
    return [BlacklistRule(str(r.id), r.scope, r.field, r.value, r.match_type, r.enabled) for r in rows]


async def load_custom_rules(session: AsyncSession) -> list[CustomRule]:
    rows = (await session.execute(select(CustomRuleEntry).where(CustomRuleEntry.enabled.is_(True)))).scalars().all()
    return [CustomRule(str(r.id), r.name, r.field, r.op, r.threshold, r.action, r.scope, r.enabled) for r in rows]


async def get_paper_account(session: AsyncSession, name: str) -> PaperAccount:
    """Returns the named paper book, creating it at its default starting
    balance on first use. Race-safe: concurrent creators converge on one row."""
    row = (await session.execute(select(PaperAccount).where(PaperAccount.name == name))).scalar_one_or_none()
    if row is not None:
        return row
    currency, balance = DEFAULT_PAPER_ACCOUNTS[name]
    await session.execute(
        insert(PaperAccount)
        .values(name=name, quote_currency=currency, starting_balance=balance, cash_balance=balance)
        .on_conflict_do_nothing(index_elements=["name"])
    )
    return (await session.execute(select(PaperAccount).where(PaperAccount.name == name))).scalar_one()


def venue_kind(p: PaperPosition) -> str:
    return ((p.plan or {}).get("venue") or {}).get("kind", "spot")


def marked_value(p: PaperPosition, price: Decimal | None = None) -> Decimal:
    """Current value of the open remainder, before exit costs: tokens at
    price for spot; remaining margin plus unrealized PnL for futures."""
    qty = p.remaining_quantity if p.remaining_quantity is not None else p.quantity
    px = price if price is not None else (p.last_price if p.last_price is not None else p.entry_price)
    if venue_kind(p) != "futures":
        return qty * px
    venue = (p.plan or {}).get("venue") or {}
    margin = Decimal(venue.get("margin", "0"))
    initial = p.initial_quantity or p.quantity
    remaining_margin = margin * qty / initial if initial else Decimal(0)
    sign = Decimal(-1) if p.side == "SHORT" else Decimal(1)
    return remaining_margin + sign * (px - p.entry_price) * qty


def notional_value(p: PaperPosition) -> Decimal:
    """Exposure of the open remainder at the last price (same as the marked
    value for spot; for futures the full notional, not the margin)."""
    qty = p.remaining_quantity if p.remaining_quantity is not None else p.quantity
    px = p.last_price if p.last_price is not None else p.entry_price
    return qty * px


async def account_state(
    session: AsyncSession, account: PaperAccount, asset_id: str | None, now: datetime, kill_switch_engaged: bool
) -> AccountState:
    """Equity is cash plus open positions marked at their last observed
    price (entry price until the first mark; futures: margin + unrealized
    PnL). Exposure is notional. Daily PnL counts positions
    fully closed since 00:00 UTC; unrealized losses are not in it, but they
    are in equity, which is what position sizing uses."""
    open_positions = (
        (
            await session.execute(
                select(PaperPosition).where(PaperPosition.account_id == account.id, PaperPosition.status == "open")
            )
        )
        .scalars()
        .all()
    )
    exposure = sum((notional_value(p) for p in open_positions), Decimal(0))
    marked = sum((marked_value(p) for p in open_positions), Decimal(0))
    token_exposure = sum((notional_value(p) for p in open_positions if asset_id and p.asset_id == asset_id), Decimal(0))
    day_start = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    daily = (
        await session.execute(
            select(func.coalesce(func.sum(PaperPosition.realized_pnl), 0)).where(
                PaperPosition.account_id == account.id, PaperPosition.status == "closed", PaperPosition.exit_at >= day_start
            )
        )
    ).scalar_one()
    last_loss = (
        await session.execute(
            select(func.max(PaperPosition.exit_at)).where(
                PaperPosition.account_id == account.id, PaperPosition.status == "closed", PaperPosition.realized_pnl < 0
            )
        )
    ).scalar_one()
    return AccountState(
        equity=account.cash_balance + marked,
        available_balance=account.cash_balance,
        open_positions=len(open_positions),
        current_exposure=exposure,
        daily_realized_pnl=Decimal(daily),
        last_loss_at=last_loss,
        token_exposure=token_exposure,
        kill_switch_engaged=kill_switch_engaged,
    )


def assessment_key(engine: str, asset_id: str, decision_point: str) -> str:
    """Idempotency key for one evaluation: the same candidate at the same
    decision point (e.g. the stream sequence or bucketed timestamp the
    inputs were taken at) must map to one row."""
    return f"{engine}:{asset_id}:{decision_point}"[:128]


async def persist_assessment(
    session: AsyncSession, assessment: Assessment, candidate_id: uuid.UUID | None, idempotency_key: str
) -> tuple[RiskAssessment, bool]:
    """Stores the assessment. Returns (row, created); a repeated key returns
    the existing row with created=False and writes nothing."""
    # Inputs snapshots can carry Decimals/datetimes; JSONB needs plain JSON.
    data = json.loads(json.dumps(assessment.to_dict(), default=str))
    approval = "PENDING" if assessment.decision.value == "REQUIRE_MANUAL_APPROVAL" else "NONE"
    stmt = (
        insert(RiskAssessment)
        .values(
            idempotency_key=idempotency_key,
            candidate_id=candidate_id,
            engine=assessment.engine,
            strategy=assessment.strategy,
            asset_id=assessment.asset_id,
            symbol=(assessment.symbol or None) and assessment.symbol[:64],
            decision=assessment.decision.value,
            status_label=assessment.status_label[:64],
            executable=assessment.executable,
            execution_target=assessment.execution_target.value,
            overall_risk=assessment.overall_risk.value,
            risk_engine_version=str(assessment.versions.get("risk_engine", ""))[:16],
            assessment=data,
            approval_state=approval,
            evaluated_at=assessment.evaluated_at,
        )
        .on_conflict_do_nothing(index_elements=["idempotency_key"])
        .returning(RiskAssessment.id)
    )
    new_id = (await session.execute(stmt)).scalar_one_or_none()
    row = (await session.execute(select(RiskAssessment).where(RiskAssessment.idempotency_key == idempotency_key))).scalar_one()
    if new_id is not None and candidate_id is not None:
        # Only the newest assessment of a candidate can be approved; older
        # pending ones describe data that no longer holds.
        await session.execute(
            update(RiskAssessment)
            .where(RiskAssessment.candidate_id == candidate_id, RiskAssessment.approval_state == "PENDING",
                   RiskAssessment.id != new_id)
            .values(approval_state="EXPIRED")
        )
    if new_id is not None:
        session.add(
            TradeTimelineEvent(
                candidate_id=candidate_id,
                assessment_id=row.id,
                event_type=f"assessed.{assessment.decision.value.lower()}",
                detail={"status": assessment.status_label, "reasons": assessment.reasons[:10]},
                occurred_at=assessment.evaluated_at,
            )
        )
    return row, new_id is not None


async def add_timeline_event(
    session: AsyncSession,
    event_type: str,
    occurred_at: datetime,
    detail: dict | None = None,
    candidate_id: uuid.UUID | None = None,
    assessment_id: uuid.UUID | None = None,
    position_id: uuid.UUID | None = None,
) -> None:
    session.add(
        TradeTimelineEvent(
            candidate_id=candidate_id,
            assessment_id=assessment_id,
            position_id=position_id,
            event_type=event_type,
            detail=detail,
            occurred_at=occurred_at,
        )
    )
