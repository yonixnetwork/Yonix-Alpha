"""Decision-pipeline steps shared by every engine (Solana gate path, futures
strategy runner): load the operator's controls, resolve approvals and
modes, record provenance, and emit events/notifications after a decision.
Keeping them in one place is what makes paper and every engine go through
the same safety pipeline."""

import hashlib
from dataclasses import replace
import json
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core import events, kill_switch
from yonixalpha_core.db.models import (
    BlacklistEntry, CustomRuleEntry, MLFeatureSnapshot, ModelVersion, PaperPosition, RiskAssessment,
)
from yonixalpha_core.ml.gate_features import DRIFT_FLAG_PREFIX, FEATURE_VERSION, MODEL_FOR_ENGINE, explain, vector
from yonixalpha_core.safety import store
from yonixalpha_core.safety.gate import Assessment
from yonixalpha_core.safety.models import FinalDecision, ManualOverrides, MLInput, StrategyMode
from yonixalpha_core.solana.assembler import Controls

APPROVAL_VALID_SECONDS = 10 * 60
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
                        asset_id: str, now: datetime, approval: bool, live: bool = False,
                        live_venue: str | None = None, source: str = "sniper") -> tuple[Controls, Any, dict]:
    """`live=True` sizes against the live wallet's book (synced from chain)
    instead of the engine's paper book; `live_venue` (binance / bybit /
    hyperliquid / mt5) against that exchange account's live book (synced
    from the exchange by services/execution-futures)."""
    safety, settings_meta = await store.load_settings(session, engine)
    if live_venue:
        # Exchange (futures / FX) books were removed with the legacy venues.
        raise ValueError(f"live venue {live_venue!r} is not supported")
    if live:
        from yonixalpha_core.live_trading import get_live_account

        account = await get_live_account(session)
    else:
        account = await store.get_paper_account(session, store.ENGINE_ACCOUNT[engine])
    state = await store.account_state(session, account, asset_id, now, await kill_switch.is_engaged(redis))
    # Operator switches (chain / sniper / copy / new entries); `source` is
    # sniper for autonomous entries, manual for operator requests.
    from yonixalpha_core.chains import controls as trading_controls

    blocked = trading_controls.blocked_by(await trading_controls.load(session),
                                          trading_controls.ENGINE_CHAIN.get(engine), source)
    invalid = store.settings_block_reason(engine, settings_meta)
    if invalid:
        from yonixalpha_core.notify import alert_error

        await alert_error("safety-gate", f"risk_settings_invalid:{engine}", invalid)
        blocked = f"{blocked}; {invalid}" if blocked else invalid
    if blocked:
        state = replace(state, trading_blocked_by=blocked)
    if live and not live_venue and state.available_balance is not None:
        # The live wallet keeps min_sol_reserve for fees and exits; enter_live
        # refuses any size above wallet - reserve, so size against that
        # instead of planning a trade the execution step must refuse.
        from yonixalpha_core.live_trading import load_live_settings

        reserve = (await load_live_settings(session)).min_sol_reserve
        state = replace(state, available_balance=max(Decimal(0), state.available_balance - reserve))
    fixed_cost = fixed_detail = None
    if live and not live_venue:
        from yonixalpha_core.live_trading import fixed_trade_costs, load_live_settings

        live_settings = await load_live_settings(session)
        fixed_cost, fixed_detail = fixed_trade_costs(live_settings)
        # Master §57: verified before trading, never discovered after signing.
        wallet = account.cash_balance
        need = live_settings.min_sol_reserve + fixed_cost
        if wallet is not None and wallet < need:
            state = replace(state, insufficient_gas=(
                f"wallet {wallet} SOL < fee reserve {live_settings.min_sol_reserve} SOL + this round trip's fixed "
                f"costs {fixed_cost} SOL"))
    controls = Controls(
        settings=safety,
        account=state,
        blacklist=await store.load_blacklist(session),
        custom_rules=await store.load_custom_rules(session),
        global_mode=await store.load_global_mode(session),
        strategy_mode=strategy_mode,
        live_trading_permitted=store.live_trading_permitted(app_settings),
        manual_approval_granted=approval,
        fixed_cost_quote=fixed_cost, fixed_cost_detail=fixed_detail,
    )
    return controls, account, settings_meta


HISTORY_LIMIT = 500


async def historical_excursion(session: AsyncSession, engine: str) -> tuple[Decimal | None, int]:
    """(75th-percentile maximum favourable excursion, sample count) over the
    engine's most recent closed LONG positions, paper and live. The
    excursion is highest price reached / entry price - 1. Returns (None, n)
    when there are fewer than MIN_HISTORY_SAMPLES closed trades."""
    from yonixalpha_core.safety.planning import MIN_HISTORY_SAMPLES

    rows = (await session.execute(
        select(PaperPosition.entry_price, PaperPosition.highest_price)
        .where(PaperPosition.engine == engine, PaperPosition.status == "closed", PaperPosition.side == "LONG",
               PaperPosition.entry_price > 0, PaperPosition.highest_price.is_not(None))
        .order_by(PaperPosition.exit_at.desc()).limit(HISTORY_LIMIT))).all()
    mfe = sorted(max(Decimal(0), hi / entry - 1) for entry, hi in rows)
    if len(mfe) < MIN_HISTORY_SAMPLES:
        return None, len(mfe)
    return mfe[min(len(mfe) - 1, (len(mfe) * 3) // 4)], len(mfe)


def manual_overrides(config: dict[str, Any], price: Decimal | None, side: str = "LONG") -> ManualOverrides:
    """The operator's percentage exit plan for a Pump.fun strategy (strategy
    config, validated by strategies.catalog) as prices around the entry
    reference. Unset values stay None and are calculated automatically; the
    planner validates every value it receives."""
    def d(key):
        v = config.get(key)
        return Decimal(str(v)) if v not in (None, "") else None

    o = ManualOverrides(position_size_quote=d("manual_position_size_sol"), max_risk_quote=d("manual_max_risk_sol"),
                        trailing_distance_pct=d("manual_trailing_pct"))
    if price is None or price <= 0:
        return o  # price-relative values cannot be placed; the planner reports the missing price
    sign = Decimal(1) if side == "LONG" else Decimal(-1)
    if (sl := d("manual_stop_loss_pct")) is not None:
        o.stop_loss = price * (1 - sign * sl)
    tps = [d(f"manual_tp{i}_pct") for i in (1, 2, 3)]
    if any(t is not None for t in tps):
        o.take_profits = [price * (1 + sign * t) for t in tps if t is not None]
    return o


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


_ESTIMATORS: dict[uuid.UUID, Any] = {}


async def champion_prediction(session: AsyncSession, redis: Redis | None, engine: str,
                              features: dict) -> tuple[MLInput | None, dict[str, Any]]:
    """Score this decision with the operator-promoted champion for the
    engine's model, if there is one. Returns (MLInput for the gate, record
    for the decision's inputs snapshot). The model is skipped - decisions
    fall back to rules only - when none is promoted, when drift was
    detected (services/ml sets the flag), or when any feature is missing.
    The gate only lets ML add caution (ML_BELOW_MIN -> WAIT); it can never
    lift a risk block."""
    name = MODEL_FOR_ENGINE.get(engine)
    if name is None:
        return None, {"status": "no model for this engine"}
    row = (await session.execute(
        select(ModelVersion.id, ModelVersion.version, ModelVersion.feature_names)
        .where(ModelVersion.name == name, ModelVersion.status == "active")
    )).first()
    if row is None:
        return None, {"model": name, "status": "no champion - rules only"}
    info: dict[str, Any] = {"model": name, "version": row.version, "model_version_id": str(row.id)}
    if redis is not None and await redis.exists(f"{DRIFT_FLAG_PREFIX}{name}"):
        return None, {**info, "status": "ignored - drift detected"}
    vec = vector(name, features)
    if vec is None or any(n not in vec for n in row.feature_names):
        return None, {**info, "status": "ignored - missing features"}
    est = _ESTIMATORS.get(row.id)
    if est is None:
        import io

        import joblib

        artifact = (await session.execute(select(ModelVersion.artifact).where(ModelVersion.id == row.id))).scalar_one()
        est = _ESTIMATORS[row.id] = joblib.load(io.BytesIO(artifact))
    values = [vec[n] for n in row.feature_names]
    score = float(est.predict_proba([values])[0][1])
    info.update(status="scored", score=round(score, 4), explanation=explain(est, list(row.feature_names), vec))
    return MLInput(name, row.version, score), info


def ml_influenced(a: Assessment) -> bool:
    return any(f.code == "ML_BELOW_MIN" and f.action == a.decision for f in a.findings)


async def after_ml(redis: Redis | None, a: Assessment, row: RiskAssessment, info: dict[str, Any]) -> None:
    if info.get("status") == "scored":
        await events.publish(redis, "ml.prediction.updated", {
            "assessment_id": str(row.id), "engine": a.engine, "symbol": a.symbol, "model": info["model"],
            "version": info["version"], "score": info["score"], "influenced": info.get("influenced", False),
        }, "decision-engine")


def ml_sample_args(ml: MLInput | None, info: dict[str, Any]) -> tuple[float | None, uuid.UUID | None]:
    if ml is None:
        return None, None
    return ml.confidence, uuid.UUID(info["model_version_id"])


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


def settings_source(meta: dict | None) -> str:
    """Which saved risk settings a decision used, so a rejection by a
    setting names where to change it."""
    if not meta:
        return ""
    src = f"{meta.get('scope')} v{meta.get('version')}"
    if meta.get("overrides") and meta.get("global_version"):
        src += f" over GLOBAL v{meta['global_version']}; this engine's own values: {', '.join(meta['overrides'])[:120]}"
    elif meta.get("legacy_full_copy"):
        src += " (full copy, ignores GLOBAL)"
    return f"\n[risk settings: {src} — Risk Settings page]"


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
                            ("; ".join(a.reasons)[:420] + settings_source(a.versions.get("settings")))[:600], "info",
                            {"assessment_id": str(row.id)})


async def after_entry(session: AsyncSession, redis: Redis | None, app_settings: Any, a: Assessment, position) -> None:
    await events.publish(redis, "trade.created", {
        "position_id": str(position.id), "engine": a.engine, "symbol": a.symbol, "side": position.side,
        "size": str(a.plan.position_size.value), "entry": str(position.entry_price),
    }, "paper")
    await events.publish(redis, "balance.updated", {"account_id": str(position.account_id)}, "paper")
    if getattr(position, "execution_mode", None) == "LIVE":
        # Only the order exists yet: the wallet signs and sends it next, and a
        # separate message reports the fill or the failure.
        title = f"LIVE BUY submitted: {a.symbol}"
        body = (f"size {a.plan.position_size.value:.6f} SOL, stop {a.plan.stop_loss.value} — real order, waiting to be "
                "signed and confirmed")
    else:
        title = f"Paper entry: {a.symbol} {position.side}"
        body = f"size {a.plan.position_size.value}, stop {a.plan.stop_loss.value}"
    await events.notify(session, redis, app_settings, "entry", title, body, "info", {"position_id": str(position.id)})
