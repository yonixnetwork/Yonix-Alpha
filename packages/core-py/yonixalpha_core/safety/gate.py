from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from yonixalpha_core.safety.liquidity import BPS, close_fill, open_fill
from yonixalpha_core.safety.models import (
    DECISION_PRECEDENCE,
    RISK_ENGINE_VERSION,
    RISK_LEVEL_ORDER,
    AssessmentInput,
    DataStatus,
    ExecutionTarget,
    FinalDecision,
    Finding,
    GlobalMode,
    Observation,
    RiskCategory,
    RiskLevel,
    StrategyMode,
    max_level,
)
from yonixalpha_core.safety.planning import TradePlan, plan_trade
from yonixalpha_core.safety.settings import SafetySettings, settings_to_dict

SOFT_WARNING_SIZE_MULTIPLIER = Decimal("0.5")

# Which inputs an engine can't trade without. Anything listed here that the
# caller couldn't obtain is a NO_TRADE — the gate never assumes it's fine.
REQUIREMENTS: dict[str, set[str]] = {
    "solana_fresh": {"market", "token", "holders", "flow", "execution"},
    "solana_migration": {"market", "token", "holders", "flow", "execution"},
    "solana_momentum": {"market", "token", "holders", "flow", "execution"},
    "binance_futures": {"market", "execution"},
    "bybit_futures": {"market", "execution"},
    "hyperliquid_perps": {"market", "execution"},
}

# Engines that trade derivatives and may therefore open shorts. Spot engines
# (Solana) can only buy what they later sell.
SHORTABLE_ENGINES = {"binance_futures", "bybit_futures", "hyperliquid_perps"}

# Token-2022 extensions whose mere presence gives an authority power over
# holders' ability to sell. Presence alone is a REJECT: this codebase can't
# verify what a transfer-hook program does, or promise a delegate won't act.
_BLOCKING_EXTENSIONS = {
    "permanentDelegate": "permanent delegate can transfer or burn any holder's tokens",
    "nonTransferable": "token is non-transferable — it can never be sold",
    "transferHook": "transfer hook runs an arbitrary program on every transfer and can block sells",
}


def requirements_for(engine: str) -> set[str]:
    return REQUIREMENTS.get(engine, {"market", "execution"})


@dataclass
class Assessment:
    decision: FinalDecision
    status_label: str
    executable: bool
    qualified: bool
    execution_target: ExecutionTarget
    overall_risk: RiskLevel
    category_risk: dict[str, str]
    findings: list[Finding]
    reasons: list[str]
    plan: TradePlan
    data_status: dict[str, str]
    engine: str
    strategy: str
    asset_id: str
    symbol: str
    evaluated_at: datetime
    versions: dict[str, Any]
    settings_snapshot: dict[str, Any]
    inputs_snapshot: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision.value,
            "status_label": self.status_label,
            "executable": self.executable,
            "qualified": self.qualified,
            "execution_target": self.execution_target.value,
            "overall_risk": self.overall_risk.value,
            "category_risk": self.category_risk,
            "findings": [f.to_dict() for f in self.findings],
            "reasons": self.reasons,
            "plan": self.plan.to_dict(),
            "data_status": self.data_status,
            "engine": self.engine,
            "strategy": self.strategy,
            "asset_id": self.asset_id,
            "symbol": self.symbol,
            "evaluated_at": self.evaluated_at.isoformat(),
            "versions": self.versions,
            "settings_snapshot": self.settings_snapshot,
            "inputs_snapshot": self.inputs_snapshot,
        }


def _finding(category, code, level, message, action, hard=False) -> Finding:
    return Finding(category, code, level, message, action, hard_block=hard)


def data_status_of(obs: Observation | None, now: datetime, max_age_seconds: int) -> DataStatus:
    if obs is None or obs.observed_at is None:
        return DataStatus.UNAVAILABLE
    age = (now - obs.observed_at).total_seconds()
    if age > max_age_seconds:
        return DataStatus.STALE
    return DataStatus.LIVE


def _check_data(inp: AssessmentInput, s: SafetySettings, required: set[str], out: list[Finding]) -> dict[str, str]:
    sources = {
        "market": inp.market.observation if inp.market else None,
        "token": inp.token.observation if inp.token else None,
        "holders": inp.holders.observation if inp.holders else None,
        "flow": inp.flow.observation if inp.flow else None,
        "execution": inp.quote.observation if inp.quote else (inp.market.observation if inp.liquidity_model and inp.market else None),
    }
    statuses: dict[str, str] = {}
    for name, obs in sources.items():
        status = data_status_of(obs, inp.now, s.max_data_age_seconds)
        if name == "flow" and status == DataStatus.LIVE and inp.flow and not inp.flow.wallet_level:
            status = DataStatus.DEGRADED
        statuses[name] = status.value
        if name not in required:
            continue
        if status == DataStatus.UNAVAILABLE:
            out.append(_finding(RiskCategory.DATA, f"{name.upper()}_UNAVAILABLE", RiskLevel.CRITICAL,
                                f"required {name} data unavailable", FinalDecision.NO_TRADE, True))
        elif status == DataStatus.STALE:
            age = (inp.now - obs.observed_at).total_seconds()
            out.append(_finding(RiskCategory.DATA, f"{name.upper()}_STALE", RiskLevel.CRITICAL,
                                f"{name} data is {age:.0f}s old (max {s.max_data_age_seconds}s) from {obs.source}",
                                FinalDecision.NO_TRADE, True))
    if inp.market is not None and inp.market.price is None and "market" in required:
        out.append(_finding(RiskCategory.DATA, "PRICE_UNAVAILABLE", RiskLevel.CRITICAL, "market price unavailable",
                            FinalDecision.NO_TRADE, True))
    return statuses


def _check_token(inp: AssessmentInput, s: SafetySettings, out: list[Finding]) -> None:
    if inp.blacklisted_by:
        out.append(_finding(RiskCategory.TOKEN, "BLACKLISTED", RiskLevel.CRITICAL,
                            f"matches blacklist rule: {inp.blacklisted_by}", FinalDecision.REJECT, True))
    t = inp.token
    if t is None:
        return
    if t.freeze_authority:
        out.append(_finding(RiskCategory.TOKEN, "FREEZE_AUTHORITY", RiskLevel.CRITICAL,
                            f"freeze authority {t.freeze_authority} is active — holder accounts can be frozen, blocking sells",
                            FinalDecision.REJECT, True))
    if t.mint_authority:
        if s.reject_active_mint_authority:
            out.append(_finding(RiskCategory.TOKEN, "MINT_AUTHORITY", RiskLevel.CRITICAL,
                                f"mint authority {t.mint_authority} is active — supply can be inflated without limit",
                                FinalDecision.REJECT, True))
        else:
            out.append(_finding(RiskCategory.TOKEN, "MINT_AUTHORITY", RiskLevel.HIGH,
                                f"mint authority {t.mint_authority} is active", FinalDecision.REQUIRE_MANUAL_APPROVAL))
    for ext, why in _BLOCKING_EXTENSIONS.items():
        if ext in t.extensions:
            out.append(_finding(RiskCategory.TOKEN, f"EXT_{ext.upper()}", RiskLevel.CRITICAL, why, FinalDecision.REJECT, True))
    if t.default_account_state == "frozen":
        out.append(_finding(RiskCategory.TOKEN, "DEFAULT_FROZEN", RiskLevel.CRITICAL,
                            "new token accounts start frozen — purchased tokens may be unsellable", FinalDecision.REJECT, True))
    if t.paused:
        out.append(_finding(RiskCategory.TOKEN, "PAUSED", RiskLevel.CRITICAL, "token transfers are paused",
                            FinalDecision.REJECT, True))
    elif "pausableConfig" in t.extensions:
        out.append(_finding(RiskCategory.TOKEN, "PAUSABLE", RiskLevel.HIGH,
                            "an authority can pause all transfers", FinalDecision.REQUIRE_MANUAL_APPROVAL))
    if t.transfer_fee_bps is not None and t.transfer_fee_bps > 0:
        if t.transfer_fee_bps > s.max_transfer_fee_bps:
            out.append(_finding(RiskCategory.TOKEN, "TRANSFER_FEE_EXCESSIVE", RiskLevel.CRITICAL,
                                f"transfer fee {t.transfer_fee_bps}bps exceeds max {s.max_transfer_fee_bps}bps",
                                FinalDecision.REJECT, True))
        else:
            out.append(_finding(RiskCategory.TOKEN, "TRANSFER_FEE", RiskLevel.MODERATE,
                                f"transfer fee {t.transfer_fee_bps}bps is charged on every sell (included in exit costs)",
                                FinalDecision.EXECUTE))
        if t.transfer_fee_authority:
            out.append(_finding(RiskCategory.TOKEN, "TRANSFER_FEE_MUTABLE", RiskLevel.HIGH,
                                "an authority can raise the transfer fee", FinalDecision.REQUIRE_MANUAL_APPROVAL))
    if "mintCloseAuthority" in t.extensions:
        out.append(_finding(RiskCategory.TOKEN, "MINT_CLOSE_AUTHORITY", RiskLevel.MODERATE,
                            "mint close authority present", FinalDecision.EXECUTE))
    if t.unparseable_extension:
        out.append(_finding(RiskCategory.TOKEN, "UNPARSEABLE_EXTENSION", RiskLevel.HIGH,
                            "mint has an extension the RPC couldn't parse — behaviour unknown", FinalDecision.REQUIRE_MANUAL_APPROVAL))


def _check_liquidity(inp: AssessmentInput, s: SafetySettings, out: list[Finding]) -> None:
    m = inp.market
    if m is None:
        return
    if m.curve_complete and not m.migrated:
        out.append(_finding(RiskCategory.LIQUIDITY, "MIGRATION_PENDING", RiskLevel.HIGH,
                            "bonding curve complete but no migrated pool yet — no venue to trade on",
                            FinalDecision.WAIT))
        return
    if m.liquidity_quote is None:
        out.append(_finding(RiskCategory.LIQUIDITY, "LIQUIDITY_UNKNOWN", RiskLevel.CRITICAL,
                            "pool liquidity unknown", FinalDecision.NO_TRADE, True))
        return
    if m.liquidity_quote < s.min_liquidity_quote:
        young = m.age_seconds is not None and m.age_seconds < s.wait_for_liquidity_max_age_seconds
        out.append(_finding(RiskCategory.LIQUIDITY, "WAITING_FOR_LIQUIDITY" if young else "INSUFFICIENT_LIQUIDITY",
                            RiskLevel.HIGH if young else RiskLevel.CRITICAL,
                            f"liquidity {m.liquidity_quote:.4f} below minimum {s.min_liquidity_quote}",
                            FinalDecision.WAIT if young else FinalDecision.NO_TRADE, not young))


def _check_execution(inp: AssessmentInput, s: SafetySettings, plan: TradePlan, out: list[Finding]) -> None:
    q = inp.quote
    model = inp.liquidity_model
    size = plan.position_size.value if plan.position_size else None
    if q is not None:
        if q.buy_route_available is False:
            out.append(_finding(RiskCategory.EXECUTION, "NO_BUY_ROUTE", RiskLevel.CRITICAL, f"no buy route on {q.venue.value}",
                                FinalDecision.NO_TRADE, True))
        if q.sell_route_available is False:
            out.append(_finding(RiskCategory.EXECUTION, "NO_SELL_ROUTE", RiskLevel.CRITICAL,
                                f"no sell route on {q.venue.value} — position could not be exited", FinalDecision.NO_TRADE, True))
        if q.buy_route_available is None or q.sell_route_available is None:
            out.append(_finding(RiskCategory.EXECUTION, "ROUTE_UNVERIFIED", RiskLevel.CRITICAL,
                                "buy/sell route availability could not be established", FinalDecision.NO_TRADE, True))
        rt = q.round_trip_loss_bps
        entry_impact, exit_impact = q.entry_impact_bps, q.exit_impact_bps
    elif model is not None and size is not None:
        o = open_fill(model, size, inp.side)
        c = close_fill(model, o.quantity, inp.side)
        if not (o.complete and c.complete):
            out.append(_finding(RiskCategory.LIQUIDITY, "BOOK_TOO_THIN", RiskLevel.CRITICAL,
                                "visible order book cannot absorb the planned size in both directions",
                                FinalDecision.NO_TRADE, True))
        entry_impact, exit_impact = o.impact_bps, c.impact_bps
        # Round trip including both fees, as a fraction of the notional.
        if inp.side == "LONG":
            spent, back = o.quote + o.fee, c.quote - c.fee
            rt = (1 - back / spent) * BPS if spent > 0 else None
        else:
            got, paid = o.quote - o.fee, c.quote + c.fee
            rt = (paid / got - 1) * BPS if got > 0 else None
    else:
        rt = entry_impact = exit_impact = None
    # A measured impact over the limit blocks at this size. It is never a
    # REDUCE_SIZE: the smaller size's impact hasn't been measured, and
    # executing the planned size would breach the limit.
    if entry_impact is not None and entry_impact > s.max_entry_impact_bps:
        out.append(_finding(RiskCategory.EXECUTION, "ENTRY_IMPACT", RiskLevel.HIGH,
                            f"entry price impact {entry_impact / 100:.2f}% exceeds {s.max_entry_impact_bps / 100:.2f}% — re-quote a smaller size",
                            FinalDecision.NO_TRADE, True))
    if exit_impact is not None and exit_impact > s.max_exit_impact_bps:
        out.append(_finding(RiskCategory.EXECUTION, "EXIT_IMPACT", RiskLevel.HIGH,
                            f"exit price impact {exit_impact / 100:.2f}% exceeds {s.max_exit_impact_bps / 100:.2f}% — exit liquidity insufficient for this size",
                            FinalDecision.NO_TRADE, True))
    exit_bps = plan.exit_cost_bps
    if rt is not None and rt > s.max_round_trip_loss_bps:
        out.append(_finding(RiskCategory.EXECUTION, "ROUND_TRIP_LOSS", RiskLevel.CRITICAL,
                            f"entering and exiting at current liquidity loses {rt / 100:.2f}% (max {s.max_round_trip_loss_bps / 100:.2f}%)",
                            FinalDecision.NO_TRADE, True))
    if exit_bps is not None and rt is not None:
        out.append(_finding(RiskCategory.EXECUTION, "EXIT_COSTS", RiskLevel.LOW,
                            f"estimated exit cost {exit_bps / 100:.2f}% incl. slippage allowance; round trip {rt / 100:.2f}%",
                            FinalDecision.EXECUTE))


def _check_holders(inp: AssessmentInput, s: SafetySettings, out: list[Finding]) -> None:
    h = inp.holders
    if h is None:
        return
    if h.top1_share >= s.reject_top1_share:
        out.append(_finding(RiskCategory.HOLDER, "TOP1_CRITICAL", RiskLevel.CRITICAL,
                            f"one wallet holds {h.top1_share:.1%} of supply (reject at {s.reject_top1_share:.0%})", FinalDecision.REJECT, True))
    elif h.top1_share > s.max_top1_share:
        out.append(_finding(RiskCategory.HOLDER, "TOP1_HIGH", RiskLevel.HIGH,
                            f"largest holder has {h.top1_share:.1%} of supply", FinalDecision.REDUCE_SIZE))
    if h.top10_share >= s.reject_top10_share:
        out.append(_finding(RiskCategory.HOLDER, "TOP10_CRITICAL", RiskLevel.CRITICAL,
                            f"top 10 wallets hold {h.top10_share:.1%} (reject at {s.reject_top10_share:.0%})", FinalDecision.REJECT, True))
    elif h.top10_share > s.max_top10_share:
        out.append(_finding(RiskCategory.HOLDER, "TOP10_HIGH", RiskLevel.HIGH,
                            f"top 10 wallets hold {h.top10_share:.1%}", FinalDecision.REDUCE_SIZE))
    if h.creator_share is not None and h.creator_share > s.max_creator_share:
        out.append(_finding(RiskCategory.HOLDER, "CREATOR_CONCENTRATION", RiskLevel.HIGH,
                            f"creator holds {h.creator_share:.1%}", FinalDecision.REQUIRE_MANUAL_APPROVAL))


def _check_flow(inp: AssessmentInput, s: SafetySettings, out: list[Finding]) -> None:
    fl = inp.flow
    if fl is None:
        return
    if fl.trade_count < s.min_trades_in_window:
        out.append(_finding(RiskCategory.TRADING, "LOW_ACTIVITY", RiskLevel.MODERATE,
                            f"only {fl.trade_count} trades in {fl.window_seconds}s", FinalDecision.WAIT))
    if not fl.wallet_level:
        out.append(_finding(RiskCategory.TRADING, "NO_WALLET_DATA", RiskLevel.HIGH,
                            "flow counts come from an aggregator without wallet identities — manipulation can't be assessed",
                            FinalDecision.REQUIRE_MANUAL_APPROVAL))
        return
    if fl.unique_buyers is not None and fl.unique_buyers < s.min_unique_buyers:
        out.append(_finding(RiskCategory.TRADING, "FEW_BUYERS", RiskLevel.HIGH,
                            f"only {fl.unique_buyers} unique buyers in window (min {s.min_unique_buyers})", FinalDecision.WAIT))
    if fl.top3_wallet_volume_share is not None and fl.top3_wallet_volume_share > s.max_top3_volume_share:
        out.append(_finding(RiskCategory.TRADING, "CONCENTRATED_VOLUME", RiskLevel.HIGH,
                            f"3 wallets account for {fl.top3_wallet_volume_share:.0%} of volume — possible wash/coordinated trading",
                            FinalDecision.REQUIRE_MANUAL_APPROVAL))
    if fl.creator_sold:
        out.append(_finding(RiskCategory.TRADING, "CREATOR_SELLING", RiskLevel.HIGH,
                            "creator wallet sold in the observation window", FinalDecision.REQUIRE_MANUAL_APPROVAL))
    total_volume = fl.buy_volume_quote + fl.sell_volume_quote
    if s.min_window_volume_quote > 0 and total_volume < s.min_window_volume_quote:
        out.append(_finding(RiskCategory.TRADING, "LOW_VOLUME", RiskLevel.MODERATE,
                            f"window volume {total_volume:.4f} below minimum {s.min_window_volume_quote}", FinalDecision.WAIT))
    if fl.early_buy_share is not None and fl.early_buy_share > s.max_early_buy_share:
        out.append(_finding(RiskCategory.HOLDER, "SNIPER_CONCENTRATION", RiskLevel.HIGH,
                            f"{fl.early_buy_share:.0%} of supply was bought in the first 30 s after launch — "
                            "RELATED-WALLET INDICATOR (possible coordinated sniping)", FinalDecision.REQUIRE_MANUAL_APPROVAL))
    if fl.sync_buy_cluster is not None and fl.sync_buy_cluster >= s.max_sync_buy_cluster:
        out.append(_finding(RiskCategory.TRADING, "SUSPICIOUS_CLUSTER", RiskLevel.HIGH,
                            f"{fl.sync_buy_cluster} distinct wallets bought near-identical amounts in the same second — "
                            "SUSPICIOUS CLUSTER (possible scripted buying)", FinalDecision.REQUIRE_MANUAL_APPROVAL))
    if fl.round_trip_share is not None and fl.round_trip_share > s.max_round_trip_share:
        out.append(_finding(RiskCategory.TRADING, "ROUND_TRIP_VOLUME", RiskLevel.HIGH,
                            f"{fl.round_trip_share:.0%} of volume came from wallets that both bought and sold in the window — "
                            "possible wash trading", FinalDecision.REQUIRE_MANUAL_APPROVAL))
    if fl.creator_launches_24h is not None and fl.creator_launches_24h > s.max_creator_launches_24h:
        out.append(_finding(RiskCategory.HOLDER, "SERIAL_CREATOR", RiskLevel.HIGH,
                            f"creator launched {fl.creator_launches_24h} tokens in the last 24 h", FinalDecision.REQUIRE_MANUAL_APPROVAL))


def _check_account(inp: AssessmentInput, s: SafetySettings, out: list[Finding]) -> None:
    a = inp.account
    if a.kill_switch_engaged:
        out.append(_finding(RiskCategory.ACCOUNT, "KILL_SWITCH", RiskLevel.CRITICAL, "kill switch engaged", FinalDecision.NO_TRADE, True))
    if a.open_positions >= s.max_open_positions:
        out.append(_finding(RiskCategory.ACCOUNT, "MAX_OPEN_POSITIONS", RiskLevel.HIGH,
                            f"{a.open_positions} open positions (max {s.max_open_positions})", FinalDecision.NO_TRADE, True))
    if a.daily_realized_pnl is None:
        out.append(_finding(RiskCategory.ACCOUNT, "DAILY_PNL_UNAVAILABLE", RiskLevel.CRITICAL,
                            "today's realized PnL unavailable — daily loss limit can't be enforced", FinalDecision.NO_TRADE, True))
    elif a.daily_realized_pnl <= -s.max_daily_loss_quote:
        out.append(_finding(RiskCategory.ACCOUNT, "DAILY_LOSS_LIMIT", RiskLevel.CRITICAL,
                            f"daily realized PnL {a.daily_realized_pnl} hit max_daily_loss {s.max_daily_loss_quote}",
                            FinalDecision.NO_TRADE, True))
    if a.last_loss_at is not None and s.cooldown_after_loss_seconds > 0:
        elapsed = (inp.now - a.last_loss_at).total_seconds()
        if elapsed < s.cooldown_after_loss_seconds:
            out.append(_finding(RiskCategory.ACCOUNT, "LOSS_COOLDOWN", RiskLevel.MODERATE,
                                f"cooldown after loss: {s.cooldown_after_loss_seconds - elapsed:.0f}s remaining", FinalDecision.WAIT))


def _check_market(inp: AssessmentInput, out: list[Finding]) -> None:
    m = inp.market
    if m is None or m.volatility is None:
        return
    if m.volatility >= Decimal("0.10"):
        out.append(_finding(RiskCategory.MARKET, "HIGH_VOLATILITY", RiskLevel.HIGH,
                            f"window volatility {m.volatility:.1%}", FinalDecision.REDUCE_SIZE))
    elif m.volatility >= Decimal("0.05"):
        out.append(_finding(RiskCategory.MARKET, "ELEVATED_VOLATILITY", RiskLevel.MODERATE,
                            f"window volatility {m.volatility:.1%}", FinalDecision.EXECUTE))


def _check_strategy_and_ml(inp: AssessmentInput, s: SafetySettings, out: list[Finding]) -> None:
    sig = inp.signal
    if sig is None:
        out.append(_finding(RiskCategory.STRATEGY, "NO_SIGNAL", RiskLevel.LOW, "no strategy signal evaluated", FinalDecision.WAIT))
    elif not sig.qualified:
        out.append(_finding(RiskCategory.STRATEGY, "SIGNAL_NOT_QUALIFIED", RiskLevel.LOW,
                            f"{sig.name} v{sig.version}: " + ("; ".join(sig.reasons) or "no qualifying signal"), FinalDecision.WAIT))
    ml = inp.ml
    if ml is not None and s.min_ml_confidence is not None and ml.confidence < s.min_ml_confidence:
        out.append(_finding(RiskCategory.ML, "ML_BELOW_MIN", RiskLevel.MODERATE,
                            f"ML {ml.model_name} v{ml.model_version} confidence {ml.confidence:.2f} below minimum {s.min_ml_confidence}",
                            FinalDecision.WAIT))
    for rule_name, action in inp.rule_actions:
        if action == FinalDecision.EXECUTE:
            continue  # an ALLOW rule can never lift a block
        level = RiskLevel.CRITICAL if action in (FinalDecision.REJECT, FinalDecision.NO_TRADE) else RiskLevel.MODERATE
        out.append(_finding(RiskCategory.STRATEGY, "CUSTOM_RULE", level, f"custom rule '{rule_name}' → {action.value}", action,
                            action in (FinalDecision.REJECT, FinalDecision.NO_TRADE)))


def _mode_findings(inp: AssessmentInput, out: list[Finding]) -> None:
    if inp.strategy_mode == StrategyMode.OFF:
        out.append(_finding(RiskCategory.STRATEGY, "STRATEGY_OFF", RiskLevel.LOW, "strategy is OFF", FinalDecision.NO_TRADE, True))
    live_requested = inp.global_mode == GlobalMode.LIVE and inp.strategy_mode in (StrategyMode.AUTO, StrategyMode.MANUAL)
    if live_requested and not inp.live_trading_permitted:
        out.append(_finding(RiskCategory.ACCOUNT, "LIVE_NOT_PERMITTED", RiskLevel.CRITICAL,
                            "LIVE mode requested but TRADING_ENABLED and LIVE_TRADING_ENABLED are not both true",
                            FinalDecision.NO_TRADE, True))


def _resolve(findings: list[Finding]) -> FinalDecision:
    actions = {f.action for f in findings}
    for d in DECISION_PRECEDENCE:
        if d in actions:
            return d
    return FinalDecision.EXECUTE


def _status_label(decision: FinalDecision, findings: list[Finding], qualified: bool) -> str:
    codes = {f.code for f in findings if f.action == decision}
    if decision in (FinalDecision.EXECUTE, FinalDecision.REDUCE_SIZE):
        return "EXECUTABLE" if decision == FinalDecision.EXECUTE else "EXECUTABLE — REDUCED SIZE"
    if decision == FinalDecision.WAIT and ("WAITING_FOR_LIQUIDITY" in codes or "MIGRATION_PENDING" in codes):
        return "WAITING_FOR_LIQUIDITY"
    if decision == FinalDecision.REQUIRE_MANUAL_APPROVAL:
        return "REQUIRE_MANUAL_APPROVAL"
    if decision == FinalDecision.REJECT:
        cats = {f.category for f in findings if f.action == FinalDecision.REJECT}
        return "REJECTED — " + " / ".join(sorted(c.value for c in cats)) + " RISK"
    if qualified:
        return "QUALIFIED — NOT EXECUTABLE"
    return decision.value


def assess(inp: AssessmentInput, settings: SafetySettings, versions: dict[str, Any] | None = None) -> Assessment:
    required = requirements_for(inp.engine)
    findings: list[Finding] = []

    _mode_findings(inp, findings)
    if inp.side not in ("LONG", "SHORT") or (inp.side == "SHORT" and inp.engine not in SHORTABLE_ENGINES):
        findings.append(_finding(RiskCategory.STRATEGY, "SHORT_NOT_SUPPORTED", RiskLevel.CRITICAL,
                                 f"{inp.engine} cannot open a {inp.side} position", FinalDecision.NO_TRADE, True))
    data_status = _check_data(inp, settings, required, findings)
    _check_token(inp, settings, findings)
    _check_liquidity(inp, settings, findings)
    _check_holders(inp, settings, findings)
    _check_flow(inp, settings, findings)
    _check_market(inp, findings)
    _check_account(inp, settings, findings)

    soft_reduce = any(f.action == FinalDecision.REDUCE_SIZE for f in findings)
    m = inp.market
    plan = plan_trade(
        entry_price=m.price if m else None,
        volatility=m.volatility if m else None,
        liquidity_quote=m.liquidity_quote if m else None,
        settings=settings,
        account=inp.account,
        overrides=inp.overrides,
        model=inp.liquidity_model,
        quote=inp.quote,
        transfer_fee_bps=inp.token.transfer_fee_bps if inp.token else None,
        size_multiplier=SOFT_WARNING_SIZE_MULTIPLIER if soft_reduce else Decimal(1),
        side=inp.side,
        strategy_levels=inp.strategy_levels,
        leverage=settings.max_leverage,
    )
    findings.extend(plan.findings)
    _check_execution(inp, settings, plan, findings)
    _check_strategy_and_ml(inp, settings, findings)

    category_levels: dict[str, list[RiskLevel]] = {c.value: [] for c in RiskCategory}
    for f in findings:
        category_levels[f.category.value].append(f.level)
    category_risk = {c: max_level(levels).value for c, levels in category_levels.items()}
    safety_categories = [c for c in RiskCategory if c not in (RiskCategory.STRATEGY, RiskCategory.ML)]
    overall = max_level([RiskLevel(category_risk[c.value]) for c in safety_categories])

    decision = _resolve(findings)

    # A single blended score must never hide a critical individual risk: any
    # CRITICAL finding is already a hard block above. Above that, overall
    # risk higher than the AUTO ceiling needs a human.
    auto_ceiling = RiskLevel(settings.max_risk_level_for_auto)
    if decision in (FinalDecision.EXECUTE, FinalDecision.REDUCE_SIZE) and RISK_LEVEL_ORDER.index(overall) > RISK_LEVEL_ORDER.index(auto_ceiling):
        findings.append(_finding(RiskCategory.ACCOUNT, "RISK_ABOVE_AUTO_CEILING", overall,
                                 f"overall risk {overall.value} exceeds automatic ceiling {auto_ceiling.value}",
                                 FinalDecision.REQUIRE_MANUAL_APPROVAL))
        decision = FinalDecision.REQUIRE_MANUAL_APPROVAL

    if decision in (FinalDecision.EXECUTE, FinalDecision.REDUCE_SIZE) and not plan.complete:
        # Defensive: planning should always have produced a finding if it
        # couldn't finish, but an entry with undefined risk must be impossible.
        findings.append(_finding(RiskCategory.ACCOUNT, "PLAN_INCOMPLETE", RiskLevel.CRITICAL,
                                 "risk plan incomplete — refusing to trade with undefined risk", FinalDecision.NO_TRADE, True))
        decision = FinalDecision.NO_TRADE

    manual_needed = inp.global_mode == GlobalMode.MANUAL or inp.strategy_mode == StrategyMode.MANUAL
    if decision in (FinalDecision.EXECUTE, FinalDecision.REDUCE_SIZE) and manual_needed and not inp.manual_approval_granted:
        findings.append(_finding(RiskCategory.STRATEGY, "MANUAL_MODE", RiskLevel.LOW,
                                 "strategy or global mode requires operator approval", FinalDecision.REQUIRE_MANUAL_APPROVAL))
        decision = FinalDecision.REQUIRE_MANUAL_APPROVAL
    elif decision == FinalDecision.REQUIRE_MANUAL_APPROVAL and inp.manual_approval_granted:
        # Approval only lifts approval-level findings; anything stricter would
        # have resolved to WAIT/NO_TRADE/REJECT above and is untouched.
        remaining = [f for f in findings if f.action != FinalDecision.REQUIRE_MANUAL_APPROVAL]
        decision = _resolve(remaining)
        findings.append(_finding(RiskCategory.STRATEGY, "MANUAL_APPROVAL_GRANTED", RiskLevel.LOW,
                                 "operator approved; all safety gates re-checked", decision))

    executable = decision in (FinalDecision.EXECUTE, FinalDecision.REDUCE_SIZE)
    safety_blocking = any(
        f.action in (FinalDecision.REJECT, FinalDecision.NO_TRADE, FinalDecision.WAIT)
        and f.category not in (RiskCategory.STRATEGY, RiskCategory.ML)
        for f in findings
    )
    signal_ok = inp.signal is not None and inp.signal.qualified
    qualified = signal_ok and not safety_blocking

    if not executable:
        target = ExecutionTarget.NONE
    elif inp.global_mode == GlobalMode.LIVE and inp.strategy_mode != StrategyMode.PAPER and inp.live_trading_permitted:
        target = ExecutionTarget.LIVE
    else:
        target = ExecutionTarget.PAPER

    reasons = [f.message for f in findings if f.action == decision] or ["all safety gates passed"]
    return Assessment(
        decision=decision,
        status_label=_status_label(decision, findings, qualified),
        executable=executable,
        qualified=qualified,
        execution_target=target,
        overall_risk=overall,
        category_risk=category_risk,
        findings=findings,
        reasons=reasons,
        plan=plan,
        data_status=data_status,
        engine=inp.engine,
        strategy=inp.strategy_name,
        asset_id=inp.asset_id,
        symbol=inp.symbol,
        evaluated_at=inp.now,
        versions={"risk_engine": RISK_ENGINE_VERSION, **(versions or {})},
        settings_snapshot=settings_to_dict(settings),
    )


def format_summary(a: Assessment) -> list[str]:
    """Human-readable decision in the shape of the spec's section 98 example."""
    lines = [a.status_label, f"Risk: {a.overall_risk.value}"]
    p = a.plan
    if p.max_loss:
        lines.append(f"Maximum Risk: {p.max_loss.value:.6f} [{p.max_loss.provenance.value}]")
    if p.position_size:
        lines.append(f"Position Size: {p.position_size.value:.6f} [{p.position_size.provenance.value}]")
    if p.stop_loss:
        lines.append(f"Stop Loss: {p.stop_loss.provenance.value} — {p.stop_loss.value:.10g}")
    for i, tp in enumerate(p.take_profits, 1):
        lines.append(f"TP{i}: {tp.price.provenance.value} — {tp.price.value:.10g} ({tp.exit_fraction:.0%})")
    if p.trailing:
        lines.append(f"Trailing: {p.trailing.provenance.value}" + ("" if p.trailing.enabled else " (disabled)"))
    if p.entry_cost_bps is not None:
        lines.append(f"Expected Entry Cost: {p.entry_cost_bps / 100:.2f}%")
    if p.exit_cost_bps is not None:
        lines.append(f"Expected Exit Cost: {p.exit_cost_bps / 100:.2f}%")
    lines.append("Data: " + ", ".join(f"{k}={v}" for k, v in a.data_status.items()))
    if not a.executable:
        lines.append("Reasons:")
        lines.extend(f"- {r}" for r in a.reasons)
    return lines
