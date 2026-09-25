"""Decision-pipeline steps shared by every engine (Solana gate path, futures
strategy runner): load the operator's controls, resolve approvals and
modes, record provenance, and emit events/notifications after a decision.
Keeping them in one place is what makes paper and every engine go through
the same safety pipeline."""

import hashlib
import json
import uuid
from datetime import datetime, timedelta
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core import events, kill_switch
from yonixalpha_core.db.models import BlacklistEntry, CustomRuleEntry, MLFeatureSnapshot, RiskAssessment
from yonixalpha_core.safety import store
from yonixalpha_core.safety.gate import Assessment
from yonixalpha_core.safety.models import FinalDecision, StrategyMode
from yonixalpha_core.solana.assembler import Controls

APPROVAL_VALID_SECONDS = 10 * 60
FEATURE_VERSION = "gate-features-v2"
EXECUTION_ADAPTER_VERSIONS = {"pump_curve": "1", "jupiter": "1", "binance_book": "1", "bybit_book": "1", "hyperliquid_book": "1"}
_MODE_RANK = {StrategyMode.OFF: 0, StrategyMode.MANUAL: 1, StrategyMode.PAPER: 2, StrategyMode.AUTO: 3}


def effective_mode(*modes: StrategyMode) -> StrategyMode:
    """Most restrictive of several modes (e.g. a strategy and its venue)."""
    return min(modes, key=lambda m: _MODE_RANK[m])


async def approval_granted(session: AsyncSession, now: datetime, candidate_id: uuid.UUID | None = None,
                           engine: str | None = None, asset_id: str | None = None, strategy: str | None = None) -> bool:
    """An APPROVED assessment for this candidate (or, for candidate-less
    engines, this engine+asset+strategy) within the approval window."""
    q = select(RiskAssessment.approved_at).where(RiskAssessment.approval_state == "APPROVED")
    if candidate_id is not None:
        q = q.where(RiskAssessment.candidate_id == candidate_id)
    else:
        q = q.where(RiskAssessment.engine == engine, RiskAssessment.asset_id == asset_id)
        if strategy:
            q = q.where(RiskAssessment.strategy == strategy)
    at = (await session.execute(q.order_by(RiskAssessment.approved_at.desc()).limit(1))).scalar_one_or_none()
    return at is not None and now - at <= timedelta(seconds=APPROVAL_VALID_SECONDS)


async def rules_version(session: AsyncSession) -> str:
    """Short hash of the enabled blacklist and custom rules, so a decision
    records exactly which rule set it was evaluated against."""
    bl = (await session.execute(select(BlacklistEntry.id, BlacklistEntry.updated_at).where(BlacklistEntry.enabled.is_(True)))).all()
    cr = (await session.execute(select(CustomRuleEntry.id, CustomRuleEntry.updated_at).where(CustomRuleEntry.enabled.is_(True)))).all()
    raw = json.dumps(sorted([f"b{r[0]}{r[1]}" for r in bl] + [f"c{r[0]}{r[1]}" for r in cr]))
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


async def load_controls(session: AsyncSession, redis: Redis, app_settings: Any, engine: str, strategy_mode: StrategyMode,
                        asset_id: str, now: datetime, approval: bool) -> tuple[Controls, Any, dict]:
    safety, settings_meta = await store.load_settings(session, engine)
    account = await store.get_paper_account(session, store.ENGINE_ACCOUNT[engine])
    controls = Controls(
        settings=safety,
        account=await store.account_state(session, account, asset_id, now, await kill_switch.is_engaged(redis)),
        blacklist=await store.load_blacklist(session),
        custom_rules=await store.load_custom_rules(session),
        global_mode=await store.load_global_mode(session),
        strategy_mode=strategy_mode,
        live_trading_permitted=store.live_trading_permitted(app_settings),
        manual_approval_granted=approval,
    )
    return controls, account, settings_meta


async def versions(session: AsyncSession, settings_meta: dict, signal, adapter: str | None) -> dict[str, Any]:
    from yonixalpha_core.paper_engine import PAPER_SIMULATOR_VERSION

    return {
        "settings": settings_meta,
        "strategy": f"{signal.name} v{signal.version}" if signal else None,
        "feature_set": FEATURE_VERSION,
        "rules": await rules_version(session),
        "paper_simulator": PAPER_SIMULATOR_VERSION,
        "execution_adapter": f"{adapter} v{EXECUTION_ADAPTER_VERSIONS.get(adapter or '', '?')}" if adapter else None,
    }


def record_ml_sample(session: AsyncSession, a: Assessment, assessment_id: uuid.UUID, candidate_id: uuid.UUID | None,
                     features: dict, ml_score: float | None = None, model_version_id: uuid.UUID | None = None) -> None:
    """Feature snapshot taken at decision time (no later data can be in it:
    the features were computed from inputs observed up to `a.evaluated_at`)."""
    from decimal import Decimal

    session.add(MLFeatureSnapshot(
        candidate_id=candidate_id, assessment_id=assessment_id, engine=a.engine, symbol=a.symbol[:64],
        features={**(features or {}), "decision_at": a.evaluated_at.isoformat()}, feature_version=FEATURE_VERSION,
        ml_score=Decimal(str(round(ml_score, 4))) if ml_score is not None else None, model_version_id=model_version_id,
    ))


async def after_decision(session: AsyncSession, redis: Redis | None, app_settings: Any, a: Assessment, row: RiskAssessment,
                         dedupe_key: str) -> None:
    """Events for every decision; notifications only for what an operator
    needs to see, de-duplicated per candidate/asset for 10 minutes."""
    await events.publish(redis, "risk.updated", {
        "assessment_id": str(row.id), "engine": a.engine, "asset_id": a.asset_id, "symbol": a.symbol,
        "decision": a.decision.value, "status": a.status_label, "risk": a.overall_risk.value,
    }, a.engine)
    if a.qualified:
        await events.publish(redis, "signal.created", {"assessment_id": str(row.id), "engine": a.engine, "symbol": a.symbol,
                                                       "strategy": a.strategy}, a.engine)

    async def once(kind: str) -> bool:
        if redis is None:
            return True
        return bool(await redis.set(f"yx:notified:{kind}:{dedupe_key}", "1", nx=True, ex=APPROVAL_VALID_SECONDS))

    if a.decision == FinalDecision.REQUIRE_MANUAL_APPROVAL and await once("approval"):
        await events.notify(session, redis, app_settings, "approval_required", f"Approval needed: {a.symbol} ({a.engine})",
                            "; ".join(a.reasons)[:500], "warning", {"assessment_id": str(row.id)})
    elif a.decision == FinalDecision.REJECT and await once("reject"):
        codes = {f.code for f in a.findings if f.action == FinalDecision.REJECT}
        kind = "blacklist_rejection" if "BLACKLISTED" in codes else "risk_rejection"
        await events.notify(session, redis, app_settings, kind, f"Rejected: {a.symbol} ({a.engine})",
                            "; ".join(a.reasons)[:500], "info", {"assessment_id": str(row.id)})


async def after_entry(session: AsyncSession, redis: Redis | None, app_settings: Any, a: Assessment, position) -> None:
    await events.publish(redis, "trade.created", {
        "position_id": str(position.id), "engine": a.engine, "symbol": a.symbol, "side": position.side,
        "size": str(a.plan.position_size.value), "entry": str(position.entry_price),
    }, "paper")
    await events.publish(redis, "balance.updated", {"account_id": str(position.account_id)}, "paper")
    await events.notify(session, redis, app_settings, "entry", f"Paper entry: {a.symbol} {position.side}",
                        f"size {a.plan.position_size.value}, stop {a.plan.stop_loss.value}", "info",
                        {"position_id": str(position.id)})
