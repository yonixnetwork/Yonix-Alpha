from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from yonixalpha_core.safety.liquidity import BPS, round_trip_fills
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
    "mt5_fx": {"market", "execution"},
}

# Engines whose pre-migration venue is the pump.fun bonding curve.
CURVE_ENGINES = {"solana_fresh", "solana_momentum"}

# Engines that trade derivatives and may therefore open shorts. Spot engines
# (Solana) can only buy what they later sell.
SHORTABLE_ENGINES = {"binance_futures", "bybit_futures", "hyperliquid_perps", "mt5_fx"}

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
    reports: dict[str, Any] = field(default_factory=dict)  # tax, sellability, liquidity

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
            "reports": self.reports,
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


def tax_report(t, s: SafetySettings) -> dict[str, Any]:
    """Token-specific buy/sell tax. On Solana the only protocol-level token
    tax is the Token-2022 TransferFeeConfig extension, withheld on every
    transfer — so it applies to both the buy (pool → wallet) and the sell
    (wallet → pool). Pump.fun / PumpSwap trading fees are protocol fees,
    costed in execution, and are NOT token tax. A transfer hook or an
    extension the RPC couldn't parse makes the tax unknowable."""
    limits = {"buy_limit_pct": str(s.max_buy_tax_pct), "sell_limit_pct": str(s.max_sell_tax_pct)}
    if t is None:
        return {"buy_tax_pct": None, "sell_tax_pct": None, "source": "mint account unavailable",
                "confidence": "UNKNOWN", **limits}
    if "transferHook" in t.extensions or t.unparseable_extension:
        why = "transfer hook program can charge or block on any transfer" if "transferHook" in t.extensions \
            else "unparseable Token-2022 extension"
        return {"buy_tax_pct": None, "sell_tax_pct": None, "source": why, "confidence": "UNKNOWN", **limits}
    if t.transfer_fee_bps:
        pct = str(Decimal(t.transfer_fee_bps) / 100)
        return {"buy_tax_pct": pct, "sell_tax_pct": pct, "source": "Token-2022 TransferFeeConfig (on-chain, max of current/next epoch)",
                "confidence": "HIGH", **limits}
    program = "Token-2022 without a transfer fee" if t.token_program == "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb" \
        else "SPL Token program (no transfer fee possible)"
    return {"buy_tax_pct": "0", "sell_tax_pct": "0", "source": program, "confidence": "HIGH", **limits}


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
    tax = tax_report(t, s)
    if tax["buy_tax_pct"] is not None and Decimal(tax["buy_tax_pct"]) > s.max_buy_tax_pct:
        out.append(_finding(RiskCategory.TOKEN, "BUY_TAX_EXCESSIVE", RiskLevel.CRITICAL,
                            f"buy tax {tax['buy_tax_pct']}% exceeds limit {s.max_buy_tax_pct}% ({tax['source']})",
                            FinalDecision.NO_TRADE, True))
    if tax["sell_tax_pct"] is not None and Decimal(tax["sell_tax_pct"]) > s.max_sell_tax_pct:
        out.append(_finding(RiskCategory.TOKEN, "SELL_TAX_EXCESSIVE", RiskLevel.CRITICAL,
                            f"sell tax {tax['sell_tax_pct']}% exceeds limit {s.max_sell_tax_pct}% ({tax['source']})",
                            FinalDecision.NO_TRADE, True))
    if tax["confidence"] == "UNKNOWN":
        out.append(_finding(RiskCategory.TOKEN, "TAX_UNKNOWN", RiskLevel.HIGH,
                            f"token tax cannot be established: {tax['source']}", FinalDecision.REQUIRE_MANUAL_APPROVAL))
    elif t.transfer_fee_bps:
        out.append(_finding(RiskCategory.TOKEN, "TRANSFER_FEE", RiskLevel.MODERATE,
                            f"token tax {tax['sell_tax_pct']}% on every buy and sell (included in costs)", FinalDecision.EXECUTE))
    if t.transfer_fee_bps and t.transfer_fee_authority:
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
    if inp.engine in CURVE_ENGINES and not m.migrated:
        # No DEX pool yet is not "no market": the pump.fun curve is the venue.
        # Whether it can take this trade is decided by the exact fill
        # simulation in _check_execution (impact, round trip, both ways);
        # without a curve model the execution data check already blocks.
        progress = f", {m.curve_progress:.0%} of the curve sold" if m.curve_progress is not None else ""
        executable = "execution simulated on the curve" if inp.liquidity_model is not None else "curve NOT executable (fee unknown)"
        out.append(_finding(RiskCategory.LIQUIDITY, "BONDING_CURVE_MARKET", RiskLevel.LOW,
                            f"NO DEX POOL YET — trading on the pump.fun bonding curve: real reserve {m.liquidity_quote:.4f} SOL"
                            f"{progress}; {executable}", FinalDecision.EXECUTE))
        if s.min_curve_liquidity_quote > 0 and m.liquidity_quote < s.min_curve_liquidity_quote:
            young = m.age_seconds is not None and m.age_seconds < s.wait_for_liquidity_max_age_seconds
            out.append(_finding(RiskCategory.LIQUIDITY, "WAITING_FOR_LIQUIDITY" if young else "INSUFFICIENT_LIQUIDITY",
                                RiskLevel.HIGH if young else RiskLevel.CRITICAL,
                                f"curve reserve {m.liquidity_quote:.4f} SOL below min_curve_liquidity_quote "
                                f"{s.min_curve_liquidity_quote}", FinalDecision.WAIT if young else FinalDecision.NO_TRADE, not young))
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
        o, c = round_trip_fills(model, size, inp.side)
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
    who = f" ({_short(h.top1_owner)})" if h.top1_owner else ""
    is_creator = bool(h.top1_owner) and h.top1_owner == inp.creator
    label = "TOP HOLDER (creator wallet)" if is_creator else "TOP HOLDER (wallet)"
    scope = "of supply; bonding curve/pool and program-owned accounts excluded"
    if h.top1_share >= s.reject_top1_share:
        out.append(_finding(RiskCategory.HOLDER, "TOP1_CRITICAL", RiskLevel.CRITICAL,
                            f"{label}{who}: {h.top1_share:.1%} {scope} (reject at {s.reject_top1_share:.0%})",
                            FinalDecision.REJECT, True))
    elif h.top1_share > s.max_top1_share:
        out.append(_finding(RiskCategory.HOLDER, "TOP1_HIGH", RiskLevel.HIGH,
                            f"{label}{who}: {h.top1_share:.1%} {scope}", FinalDecision.REDUCE_SIZE))
    if h.top10_share >= s.reject_top10_share:
        out.append(_finding(RiskCategory.HOLDER, "TOP10_CRITICAL", RiskLevel.CRITICAL,
                            f"TOP 10 WALLETS: {h.top10_share:.1%} {scope} (reject at {s.reject_top10_share:.0%})",
                            FinalDecision.REJECT, True))
    elif h.top10_share > s.max_top10_share:
        out.append(_finding(RiskCategory.HOLDER, "TOP10_HIGH", RiskLevel.HIGH,
                            f"TOP 10 WALLETS: {h.top10_share:.1%} {scope}", FinalDecision.REDUCE_SIZE))
    if h.creator_share is not None and h.creator_share > s.max_creator_share:
        out.append(_finding(RiskCategory.HOLDER, "CREATOR_CONCENTRATION", RiskLevel.HIGH,
                            f"CREATOR WALLET holding: {h.creator_share:.1%} of supply (the launch creator's own wallet)",
                            FinalDecision.REQUIRE_MANUAL_APPROVAL))
    if h.protocol_agent_share > 0:
        over = h.protocol_agent_share > s.max_protocol_agent_share
        out.append(_finding(RiskCategory.HOLDER, "PROTOCOL_AGENT_HOLDING", RiskLevel.MODERATE if not over else RiskLevel.HIGH,
                            f"PUMP.FUN MAYHEM AGENT VAULT: {h.protocol_agent_share:.1%} of supply — a protocol trading agent, "
                            "not the developer; it can sell into the market"
                            + (f" (above {s.max_protocol_agent_share:.0%}: size reduced)" if over else ""),
                            FinalDecision.REDUCE_SIZE if over else FinalDecision.EXECUTE))
    if h.largest_program_share > s.max_program_controlled_share:
        out.append(_finding(RiskCategory.HOLDER, "PROGRAM_CONTROLLED_HOLDING", RiskLevel.HIGH,
                            f"PROGRAM-CONTROLLED ACCOUNT ({_short(h.largest_program_owner)}): {h.largest_program_share:.1%} "
                            "of supply — owned by a program (e.g. locker, vault or other pool), not a wallet; who controls "
                            "it is unknown", FinalDecision.REQUIRE_MANUAL_APPROVAL))


def _short(addr: str | None) -> str:
    return f"{addr[:4]}…{addr[-4:]}" if addr and len(addr) > 10 else (addr or "?")


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
    if fl.volume_churn is not None and fl.volume_churn > s.max_volume_churn and fl.trade_count >= 10:
        out.append(_finding(RiskCategory.TRADING, "VOLUME_CHURN", RiskLevel.HIGH,
                            f"gross volume is {fl.volume_churn:.0f}x net demand — volume without net buying (possible wash trading)",
                            FinalDecision.REQUIRE_MANUAL_APPROVAL))
    if fl.repeated_wallet_share is not None and fl.repeated_wallet_share > s.max_repeated_wallet_share and fl.trade_count >= 10:
        out.append(_finding(RiskCategory.TRADING, "REPEATED_WALLETS", RiskLevel.HIGH,
                            f"{fl.repeated_wallet_share:.0%} of trades come from wallets trading 3+ times — low wallet diversity",
                            FinalDecision.REQUIRE_MANUAL_APPROVAL))
    if (fl.unique_buyers_first_half is not None and fl.unique_buyers_second_half is not None
            and fl.unique_buyers_first_half >= 5 and fl.unique_buyers_second_half == 0):
        out.append(_finding(RiskCategory.TRADING, "BUYERS_STOPPED", RiskLevel.MODERATE,
                            "no new buyers in the second half of the window", FinalDecision.WAIT))
    if fl.creator_linked_buyers is not None and fl.creator_linked_buyers > s.max_creator_linked_buyers:
        out.append(_finding(RiskCategory.HOLDER, "CREATOR_LINKED", RiskLevel.HIGH,
                            f"{fl.creator_linked_buyers} early buyer(s) were funded by the creator — CREATOR-LINKED INDICATOR",
                            FinalDecision.REQUIRE_MANUAL_APPROVAL))
    if fl.related_wallet_groups is not None and fl.related_wallet_groups >= s.max_related_wallet_group:
        out.append(_finding(RiskCategory.HOLDER, "RELATED_WALLETS", RiskLevel.HIGH,
                            f"{fl.related_wallet_groups} fresh early buyers share one funding source — RELATED-WALLET INDICATOR",
                            FinalDecision.REQUIRE_MANUAL_APPROVAL))
    if fl.funding_checked_wallets == 0:
        # Attempted but no wallet could be checked: the indicators above are
        # unavailable, which is stated rather than read as "no links".
        out.append(_finding(RiskCategory.HOLDER, "FUNDING_UNCHECKED", RiskLevel.MODERATE,
                            "early buyers' funding sources could not be checked; creator-linked and related-wallet "
                            "indicators are unavailable for this decision", FinalDecision.EXECUTE))
    if fl.creator_launches_24h is not None and fl.creator_launches_24h > s.max_creator_launches_24h:
        out.append(_finding(RiskCategory.HOLDER, "SERIAL_CREATOR", RiskLevel.HIGH,
                            f"creator launched {fl.creator_launches_24h} tokens in the last 24 h", FinalDecision.REQUIRE_MANUAL_APPROVAL))


def _check_observation(inp: AssessmentInput, out: list[Finding]) -> None:
    """Fresh tokens: the activity trend over the latest window (see
    solana.observation). DETERIORATING waits; the reasons are listed."""
    obs = inp.observation
    if not obs:
        return
    trend = obs.get("trend")
    if trend == "DETERIORATING":
        out.append(_finding(RiskCategory.TRADING, "ACTIVITY_DETERIORATING", RiskLevel.MODERATE,
                            f"activity deteriorating over the latest {obs.get('window_seconds')}s: "
                            + "; ".join(obs.get("negative") or []), FinalDecision.WAIT))
    elif trend == "NO_ACTIVITY":
        out.append(_finding(RiskCategory.TRADING, "NO_RECENT_ACTIVITY", RiskLevel.MODERATE,
                            f"no trades in the latest {obs.get('window_seconds')}s", FinalDecision.WAIT))
    else:
        out.append(_finding(RiskCategory.TRADING, f"ACTIVITY_{trend}", RiskLevel.LOW,
                            f"activity {str(trend).lower()} over the latest {obs.get('window_seconds')}s: "
                            + ("; ".join(obs.get("positive") or []) or "no change"), FinalDecision.EXECUTE))


ENTRY_EXIT_BLOCKING = ("REDUCE", "EXIT", "EXIT_NOW")


def _check_entry_quality(inp: AssessmentInput, out: list[Finding]) -> None:
    """ENTRY_DETERIORATION: two or more independent signs that the token is
    already turning (sellers accelerating, buyers stalling, volume collapsing
    against its own context, a sharp reversal from the local high, a spike
    without broad buying). One sign alone is only reported: a short lull is
    not a collapse. Automatic entries wait and are re-evaluated; a manual
    BUY shows it and needs the operator's confirmation."""
    q = inp.entry_quality
    if not q or not q.get("indicators"):
        return
    detail = "; ".join(q.get("evidence") or [])
    if q.get("strong"):
        action = FinalDecision.REQUIRE_MANUAL_APPROVAL if inp.operator_request else FinalDecision.WAIT
        out.append(_finding(RiskCategory.TRADING, "ENTRY_DETERIORATION", RiskLevel.HIGH,
                            f"flow is deteriorating ({', '.join(q['indicators'])}): {detail}", action))
    else:
        out.append(_finding(RiskCategory.TRADING, "ENTRY_WEAKENING", RiskLevel.MODERATE,
                            f"one sign of weakening ({q['indicators'][0]}): {detail} — not enough alone to wait",
                            FinalDecision.EXECUTE))


def _check_entry_exit(inp: AssessmentInput, out: list[Finding]) -> None:
    """EXIT_SIGNAL_AT_ENTRY: never open a position the existing exit
    intelligence would start selling on its first tick. Same rules, same
    thresholds, same pre-entry trades (see solana.assembler); the token waits
    and is re-evaluated with fresh data."""
    chk = inp.entry_exit_check
    if not chk or chk.get("action") not in ENTRY_EXIT_BLOCKING:
        return
    out.append(_finding(RiskCategory.TRADING, "EXIT_SIGNAL_AT_ENTRY", RiskLevel.HIGH,
                        f"exit intelligence would {chk['action']} a position opened now: "
                        + "; ".join(chk.get("reasons") or []), FinalDecision.WAIT))


_CREATOR_ACTION = {
    "WARN": (FinalDecision.EXECUTE, RiskLevel.MODERATE, "WARNING"),
    "REDUCE_SIZE": (FinalDecision.REDUCE_SIZE, RiskLevel.MODERATE, "WARNING"),
    "REQUIRE_MANUAL_APPROVAL": (FinalDecision.REQUIRE_MANUAL_APPROVAL, RiskLevel.HIGH, "WARNING"),
    "REJECT": (FinalDecision.REJECT, RiskLevel.CRITICAL, "REJECT"),
}


def creator_history_report(inp: AssessmentInput, s: SafetySettings) -> dict[str, Any] | None:
    """What the creator-history check decided, with the exact reason. None
    when no history was evaluated (non-Solana engines) or the check is off."""
    h = inp.creator_history
    if h is None:
        return None
    base = {"creator": h.get("creator"), "tokens_created": h.get("tokens_created"),
            "previous_launches": h.get("previous_launches"), "previous_migrated": h.get("previous_migrated"),
            "count_status": h.get("status"), "source": h.get("source"), "minimum": s.min_creator_tokens_created,
            "maximum": s.max_creator_tokens_created or None, "enabled": s.creator_history_check,
            "stream_observed_launches": h.get("stream_observed_launches"), "stream_since": h.get("stream_since"),
            "previous_creator_sold_early": h.get("previous_creator_sold_early"),
            "previous_checked_for_sells": h.get("previous_checked_for_sells"),
            "previous_observation_rejected": h.get("previous_observation_rejected"),
            "previous_rejection_reasons": h.get("previous_rejection_reasons") or [],
            "creator_sold_now": inp.flow.creator_sold if inp.flow else None,
            "creator_linked_buyers": inp.flow.creator_linked_buyers if inp.flow else None,
            "error": h.get("error")}
    if not s.creator_history_check:
        return {**base, "result": "OFF", "action": None, "reason": "creator history check disabled"}
    n, status = h.get("tokens_created"), h.get("status")
    known = n is not None and (status == "VERIFIED" or (status == "LOWER_BOUND" and n >= s.min_creator_tokens_created))
    if not known:
        action = s.creator_history_unknown_action
        return {**base, "tokens_created": None, "result": "UNKNOWN", "action": action,
                "reason": "CREATOR_HISTORY_UNKNOWN", "detail": h.get("error") or
                (f"only a lower bound is available (at least {n}), below the minimum" if n is not None else "count unavailable")}
    shown = f"at least {n}" if status == "LOWER_BOUND" else str(n)
    if s.max_creator_tokens_created and n >= s.max_creator_tokens_created and status == "VERIFIED":
        return {**base, "tokens_created_display": shown, "result": "WARNING", "action": "REQUIRE_MANUAL_APPROVAL",
                "reason": "CREATOR_SERIAL_LAUNCHER"}
    if n >= s.min_creator_tokens_created:
        return {**base, "tokens_created_display": shown, "result": "PASS", "action": None, "reason": "CREATOR_HISTORY_OK"}
    action = s.creator_below_threshold_action
    return {**base, "tokens_created_display": shown, "result": _CREATOR_ACTION[action][2], "action": action,
            "reason": "CREATOR_HISTORY_BELOW_THRESHOLD"}


def _check_creator_history(inp: AssessmentInput, s: SafetySettings, out: list[Finding]) -> None:
    r = creator_history_report(inp, s)
    if r is None or r["result"] == "OFF":
        return
    if r["result"] == "UNKNOWN":
        decision, level, _ = _CREATOR_ACTION[r["action"]]
        out.append(_finding(RiskCategory.HOLDER, "CREATOR_HISTORY_UNKNOWN", level,
                            f"CREATOR TOKENS CREATED: UNKNOWN · Minimum: {s.min_creator_tokens_created} · Action: {r['action']} · "
                            f"Reason: CREATOR_HISTORY_UNKNOWN — CREATOR HISTORY: UNKNOWN ({r['detail']}); the count is never estimated",
                            decision, decision == FinalDecision.REJECT))
        return
    shown = r["tokens_created_display"]
    prev = f"; previous launches {r['previous_launches']}" if r.get("previous_launches") is not None else ""
    if r.get("previous_migrated") is not None:
        prev += f", {r['previous_migrated']} migrated"
    if r["reason"] == "CREATOR_HISTORY_OK":
        out.append(_finding(RiskCategory.HOLDER, "CREATOR_HISTORY_OK", RiskLevel.LOW,
                            f"CREATOR TOKENS CREATED: {shown} · Minimum: {s.min_creator_tokens_created} — CREATOR HISTORY: PASS"
                            f"{prev} ({r['source']})", FinalDecision.EXECUTE))
    elif r["reason"] == "CREATOR_SERIAL_LAUNCHER":
        out.append(_finding(RiskCategory.HOLDER, "CREATOR_SERIAL_LAUNCHER", RiskLevel.HIGH,
                            f"CREATOR TOKENS CREATED: {shown} · Maximum: {s.max_creator_tokens_created} · "
                            "Action: REQUIRE_MANUAL_APPROVAL · Reason: CREATOR_SERIAL_LAUNCHER — CREATOR HISTORY: WARNING "
                            f"(serial-launcher indicator, not proof){prev}", FinalDecision.REQUIRE_MANUAL_APPROVAL))
    else:
        decision, level, label = _CREATOR_ACTION[r["action"]]
        out.append(_finding(RiskCategory.HOLDER, "CREATOR_HISTORY_BELOW_THRESHOLD", level,
                            f"Creator Tokens Created: {shown} · Minimum: {s.min_creator_tokens_created} · Action: {r['action']} · "
                            f"Reason: CREATOR_HISTORY_BELOW_THRESHOLD — CREATOR HISTORY: {label}{prev}. Few launches is not "
                            "evidence of malice; this is the configured policy", decision, decision == FinalDecision.REJECT))
    sold, checked = r.get("previous_creator_sold_early"), r.get("previous_checked_for_sells")
    if sold:
        out.append(_finding(RiskCategory.HOLDER, "CREATOR_PRIOR_EARLY_SELLS", RiskLevel.MODERATE,
                            f"creator sold within 5 min of launch on {sold} of {checked} previous launches this system "
                            "observed — historical selling indicator", FinalDecision.EXECUTE))


_INTEL_ACTION = {
    "WARN": (FinalDecision.EXECUTE, RiskLevel.MODERATE),
    "REQUIRE_MANUAL_APPROVAL": (FinalDecision.REQUIRE_MANUAL_APPROVAL, RiskLevel.HIGH),
    "WAIT": (FinalDecision.WAIT, RiskLevel.HIGH),
    "NO_TRADE": (FinalDecision.NO_TRADE, RiskLevel.HIGH),
}


def _check_intel(inp: AssessmentInput, s: SafetySettings, out: list[Finding]) -> None:
    """Regime and manipulation (solana.intel). Only the configured actions
    act; everything else in the intelligence record is evidence."""
    intel = inp.intel
    if not intel:
        return
    regime = intel.get("regime") or {}
    cm = regime.get("curve_math") or {}
    if regime.get("mayhem") or cm.get("valid") is False:
        decision, level = _INTEL_ACTION[s.mayhem_action]
        why = ("Mayhem Mode token" if regime.get("mayhem") else "bonding curve does not follow the standard constant product")
        out.append(_finding(RiskCategory.EXECUTION, "MAYHEM_OR_NONSTANDARD_CURVE", level,
                            f"{why}: curve price, impact and position sizing are computed with constant-product math that "
                            f"does not hold here ({cm.get('reason')}); Mayhem flow also contains an automated agent's trades · "
                            f"Action: {s.mayhem_action}", decision, decision == FinalDecision.NO_TRADE))
    elif regime.get("mayhem") is None and intel.get("stage") in ("FRESH", "MOMENTUM"):
        out.append(_finding(RiskCategory.DATA, "MAYHEM_FLAG_UNKNOWN", RiskLevel.MODERATE,
                            "Mayhem flag unknown (no create event or curve account read): curve math unverified",
                            FinalDecision.EXECUTE))
    m = intel.get("manipulation") or {}
    if m.get("level") in ("HIGH", "MEDIUM"):
        action = s.manipulation_high_action if m["level"] == "HIGH" else s.manipulation_medium_action
        decision, level = _INTEL_ACTION[action]
        counted = m.get("counted")
        fams = "; ".join(f"{k}: {v}" for k, v in (m.get("families") or {}).items() if counted is None or k in counted)
        out.append(_finding(RiskCategory.TRADING, f"MANIPULATION_{m['level']}", level,
                            f"manipulation score {m['level']} ({m.get('count')} independent families): {fams[:400]} · "
                            f"Action: {action} — patterns, not proof", decision))
    if regime.get("instant_bond"):
        out.append(_finding(RiskCategory.MARKET, "INSTANT_BOND", RiskLevel.MODERATE,
                            f"created and migrated within {s.instant_bond_seconds}s: a bundled bond nobody else could buy; "
                            "post-migration behaviour is its own regime", FinalDecision.EXECUTE))
    if regime.get("boost_window"):
        out.append(_finding(RiskCategory.MARKET, "BOOST_WINDOW", RiskLevel.MODERATE,
                            f"{regime.get('seconds_since_migration')}s after migration: part of the buying in the first "
                            f"{s.boost_window_seconds}s is pump.fun BOOST buybacks, not organic demand", FinalDecision.EXECUTE))
    dc = (intel.get("wallets") or {}).get("dump_cluster") or {}
    if dc.get("level") == "HIGH":
        decision, level = _INTEL_ACTION[s.dump_cluster_high_action]
        out.append(_finding(RiskCategory.TRADING, "DUMP_CLUSTER_HIGH", level,
                            f"early buyers include a cohort that repeatedly sold early together in earlier collapsed (LOSS) launches "
                            f"({'; '.join(dc.get('evidence') or [])[:300]}) · Action: {s.dump_cluster_high_action} — "
                            "a pattern from this system's history, not an accusation", decision))
    mp = intel.get("manufactured_pump") or {}
    if mp.get("risk") == "HIGH":
        decision, level = _INTEL_ACTION[s.manufactured_pump_action]
        out.append(_finding(RiskCategory.TRADING, "MANUFACTURED_PUMP_PATTERN", level,
                            f"manufactured-pump pattern over the last {mp.get('pattern_duration_seconds')}s "
                            f"({'; '.join(mp.get('evidence') or [])[:300]}; detector {mp.get('detector_version')}) · "
                            f"Action: {s.manufactured_pump_action} — a documented pattern associated with elevated "
                            "manipulation risk, not a prediction", decision))
    pm = intel.get("post_migration") or {}
    if pm.get("state") == "DUMPING":
        decision, level = _INTEL_ACTION[s.postmig_dumping_action]
        out.append(_finding(RiskCategory.MARKET, "POST_MIGRATION_DUMPING", level,
                            f"post-migration state DUMPING ({'; '.join(pm.get('evidence') or [])}) · "
                            f"Action: {s.postmig_dumping_action}", decision))


def _check_scanner_intel(inp: AssessmentInput, s: SafetySettings, out: list[Finding]) -> None:
    """Wallet relationships, organic demand and deployer history
    (solana.wallet_graph, deployer_intel). Each check needs measured
    evidence; missing evidence never fires a check and is never read as
    clean. The configured actions decide what a finding does (WARN by
    default)."""
    intel = inp.intel or {}
    rel = intel.get("relationships") or {}

    def add(code: str, action: str, msg: str, category=RiskCategory.TRADING) -> None:
        decision, level = _INTEL_ACTION[action]
        out.append(_finding(category, code, level, f"{msg} · Action: {action}", decision))

    if rel.get("status") == "MEASURED":
        b, d, c = rel.get("buyers") or {}, rel.get("demand") or {}, rel.get("creator") or {}
        eff = b.get("effective_unique_buyers")
        if s.min_effective_buyers and eff is not None and eff < s.min_effective_buyers:
            add("LOW_EFFECTIVE_BUYERS", s.low_effective_buyers_action,
                f"{eff} effective independent buyers (min {s.min_effective_buyers}): {b.get('explanation')}")
        ratio = d.get("organic_demand_ratio")
        if s.min_organic_demand_ratio and ratio is not None and ratio < float(s.min_organic_demand_ratio):
            add("LOW_ORGANIC_DEMAND", s.low_organic_demand_action,
                f"organic-demand ratio {ratio:.0%} (min {float(s.min_organic_demand_ratio):.0%}): "
                f"{d.get('known_related_volume_sol')} of {d.get('total_volume_sol')} SOL from known related wallets")
        largest = rel.get("largest_dependent_cluster") or 0
        if s.coordination_high_wallets and largest >= s.coordination_high_wallets:
            top = next((x for x in rel.get("clusters") or [] if x.get("size") == largest), {})
            add("HIGH_COORDINATION", s.coordination_high_action,
                f"{top.get('classification')} cluster {top.get('cluster_id')} of {largest} wallets "
                f"({'; '.join(top.get('evidence') or [])[:250]}) — evidence of dependence, not proof of manipulation")
        crv = c.get("creator_related_volume_ratio")
        if s.max_creator_related_volume_ratio and crv is not None and crv > float(s.max_creator_related_volume_ratio):
            add("CREATOR_CONCENTRATION", s.creator_concentration_action,
                f"creator and creator-linked wallets are {crv:.0%} of volume (max {float(s.max_creator_related_volume_ratio):.0%})")
        sm = rel.get("smart_money") or {}
        if sm.get("context") in ("MULTIPLE_RELATED", "CREATOR_RELATED"):
            add("HIGH_SMART_MONEY_CONCENTRATION", s.smart_money_concentration_action,
                f"historically proven wallets are present only as related wallets ({sm['context']}: "
                f"{sm.get('smart_money_clustered')} clustered, {sm.get('smart_money_creator_related')} creator-related, "
                f"{sm.get('smart_money_independent')} independent)")
    dep = intel.get("deployer") or {}
    if dep.get("status") == "MEASURED" and s.deployer_max_risk_score and \
            (dep.get("deployer_risk_score") or 0) >= float(s.deployer_max_risk_score):
        add("POOR_DEPLOYER_HISTORY", s.deployer_history_action,
            f"deployer risk score {dep['deployer_risk_score']} (loss rate of {dep.get('resolved_launches')} earlier resolved "
            f"launches, shrunk toward the base rate; max {float(s.deployer_max_risk_score)}); bond rate "
            f"{dep.get('deployer_bond_rate')}; history cutoff {dep.get('deployer_history_cutoff')}", RiskCategory.TOKEN)


def _check_names(inp: AssessmentInput, s: SafetySettings, out: list[Finding]) -> None:
    name = inp.token_name
    if name is None:
        return
    shown = name.strip()
    if s.min_name_length and len(shown) < s.min_name_length:
        out.append(_finding(RiskCategory.TOKEN, "NAME_TOO_SHORT", RiskLevel.CRITICAL,
                            f"token name '{shown}' is shorter than min_name_length {s.min_name_length}", FinalDecision.REJECT, True))
    if s.ascii_names_only and not (shown + (inp.symbol or "")).isascii():
        out.append(_finding(RiskCategory.TOKEN, "NON_ASCII_NAME", RiskLevel.CRITICAL,
                            f"token name/symbol '{shown}' / '{inp.symbol}' contains non-ASCII characters (ascii_names_only)",
                            FinalDecision.REJECT, True))
    if s.skip_duplicate_names and inp.duplicate_of:
        out.append(_finding(RiskCategory.TOKEN, "DUPLICATE_NAME", RiskLevel.CRITICAL,
                            f"name '{shown}' was already launched as {_short(inp.duplicate_of)} in the last 24 h "
                            "(skip_duplicate_names — copycat launch)", FinalDecision.REJECT, True))


def _migrated_applies(inp: AssessmentInput) -> bool:
    m = inp.market
    return inp.engine.startswith("solana") and m is not None and m.migrated is True


def migrated_liquidity_report(inp: AssessmentInput, s: SafetySettings, plan: TradePlan) -> dict[str, Any]:
    """Usable liquidity of a migrated (PumpSwap) pool in USD, and what the
    planned size can actually execute. Usable = the pool's SOL side — what a
    seller can withdraw — valued at SOL/USD. Total = both sides at the pool
    price (2x usable), the cosmetic figure aggregators show."""
    m = inp.market
    if not _migrated_applies(inp):
        return {"applies": False, "minimum_usd": str(s.min_migrated_liquidity_usd),
                "reason": "bonding-curve token: judged on curve state and executability; the migrated USD minimum "
                          "applies after migration"}
    out: dict[str, Any] = {"applies": True, "enabled": s.migrated_liquidity_check,
                           "minimum_usd": str(s.min_migrated_liquidity_usd),
                           "sol_usd": str(inp.sol_usd) if inp.sol_usd is not None else None,
                           "sol_usd_source": inp.sol_usd_source,
                           "usable_liquidity_sol": str(m.liquidity_quote) if m.liquidity_quote is not None else None,
                           "usable_liquidity_usd": None, "total_liquidity_usd": None,
                           "max_entry_impact_bps": str(s.max_entry_impact_bps), "max_exit_impact_bps": str(s.max_exit_impact_bps),
                           "max_entry_slippage_bps": str(s.migrated_max_entry_slippage_bps),
                           "max_exit_slippage_bps": str(s.migrated_max_exit_slippage_bps)}
    if m.liquidity_quote is not None and inp.sol_usd is not None:
        usable = (m.liquidity_quote * inp.sol_usd).quantize(Decimal("0.01"))
        out["usable_liquidity_usd"], out["total_liquidity_usd"] = str(usable), str(usable * 2)
    model = inp.liquidity_model
    size = plan.position_size.value if plan.position_size else None
    if model is not None and hasattr(model, "max_size_within") and m.liquidity_quote:
        depth = model.max_size_within(s.max_entry_impact_bps, s.max_exit_impact_bps, m.liquidity_quote)
        out["executable_max_size_sol"] = str(depth.quantize(Decimal("0.0001")))
        out["executable_max_size_usd"] = str((depth * inp.sol_usd).quantize(Decimal("0.01"))) if inp.sol_usd else None
    if model is not None and hasattr(model, "marginal_price") and size:
        o, c = round_trip_fills(model, size, inp.side)
        mp = model.marginal_price
        out["planned_size_sol"] = str(size)
        out["entry_impact_bps"], out["exit_impact_bps"] = str(o.impact_bps.quantize(Decimal("0.01"))), \
            str(c.impact_bps.quantize(Decimal("0.01")))
        if o.quantity > 0 and c.complete:
            out["entry_slippage_bps"] = str((((o.quote + o.fee) / o.quantity / mp - 1) * BPS).quantize(Decimal("0.01")))
            out["exit_slippage_bps"] = str(((1 - (c.quote - c.fee) / o.quantity / mp) * BPS).quantize(Decimal("0.01")))
    if not s.migrated_liquidity_check:
        out.update(decision="PASS", reason="migrated liquidity check disabled")
    elif out["usable_liquidity_usd"] is None:
        out.update(decision="NO_TRADE", reason="MIGRATED_LIQUIDITY_USD_UNKNOWN")
    elif Decimal(out["usable_liquidity_usd"]) < s.min_migrated_liquidity_usd:
        out.update(decision="NO_TRADE", reason="INSUFFICIENT_MIGRATED_LIQUIDITY")
    else:
        out.update(decision="PASS", reason="MIGRATED_LIQUIDITY_OK")
    return out


def _usd(v: Any) -> str:
    return f"${Decimal(v):,.0f}"


def _check_migrated_liquidity(r: dict[str, Any], s: SafetySettings, out: list[Finding]) -> None:
    if not r.get("applies") or not s.migrated_liquidity_check:
        return
    minimum = _usd(s.min_migrated_liquidity_usd)
    if r["reason"] == "MIGRATED_LIQUIDITY_USD_UNKNOWN":
        why = "SOL/USD unavailable" if r["sol_usd"] is None else "pool liquidity unknown"
        out.append(_finding(RiskCategory.LIQUIDITY, "MIGRATED_LIQUIDITY_USD_UNKNOWN", RiskLevel.CRITICAL,
                            f"Usable Liquidity: UNKNOWN ({why}) · Minimum: {minimum} · Decision: NO_TRADE · "
                            "Reason: MIGRATED_LIQUIDITY_USD_UNKNOWN", FinalDecision.NO_TRADE, True))
        return
    depth = f"; executable within impact limits up to {r['executable_max_size_sol']} SOL" if r.get("executable_max_size_sol") else ""
    base = (f"Usable Liquidity: {_usd(r['usable_liquidity_usd'])} ({r['usable_liquidity_sol']} SOL at "
            f"${Decimal(r['sol_usd']):,.2f}) · Total: {_usd(r['total_liquidity_usd'])} · Minimum: {minimum}")
    if r["reason"] == "INSUFFICIENT_MIGRATED_LIQUIDITY":
        out.append(_finding(RiskCategory.LIQUIDITY, "INSUFFICIENT_MIGRATED_LIQUIDITY", RiskLevel.CRITICAL,
                            f"{base} · Decision: NO_TRADE · Reason: INSUFFICIENT_MIGRATED_LIQUIDITY", FinalDecision.NO_TRADE, True))
        return
    out.append(_finding(RiskCategory.LIQUIDITY, "MIGRATED_LIQUIDITY_OK", RiskLevel.LOW,
                        f"{base} · Decision: PASS{depth}", FinalDecision.EXECUTE))
    for side, limit in (("entry", s.migrated_max_entry_slippage_bps), ("exit", s.migrated_max_exit_slippage_bps)):
        v = r.get(f"{side}_slippage_bps")
        if v is not None and Decimal(v) > limit:
            out.append(_finding(RiskCategory.EXECUTION, f"MIGRATED_{side.upper()}_SLIPPAGE", RiskLevel.HIGH,
                                f"{side} slippage {Decimal(v) / 100:.2f}% (price impact + pool fee at the planned size) exceeds "
                                f"{limit / 100:.2f}% · Decision: NO_TRADE", FinalDecision.NO_TRADE, True))


def _check_account(inp: AssessmentInput, s: SafetySettings, out: list[Finding]) -> None:
    a = inp.account
    if a.kill_switch_engaged:
        out.append(_finding(RiskCategory.ACCOUNT, "KILL_SWITCH", RiskLevel.CRITICAL, "kill switch engaged", FinalDecision.NO_TRADE, True))
    if a.trading_blocked_by:
        out.append(_finding(RiskCategory.ACCOUNT, "TRADING_CONTROL_OFF", RiskLevel.CRITICAL,
                            f"operator control: {a.trading_blocked_by} (new entries blocked; exits unaffected)",
                            FinalDecision.NO_TRADE, True))
    if a.insufficient_gas:
        out.append(_finding(RiskCategory.ACCOUNT, "INSUFFICIENT_GAS", RiskLevel.CRITICAL,
                            f"INSUFFICIENT GAS: {a.insufficient_gas}", FinalDecision.NO_TRADE, True))
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
    if m is not None and m.volatility is None and m.volatility_confidence == "UNAVAILABLE":
        # Reported so the operator sees WHY an automatic stop cannot be sized
        # (planning blocks that on its own); a manual stop needs no volatility.
        out.append(_finding(RiskCategory.DATA, "VOLATILITY_UNAVAILABLE", RiskLevel.LOW,
                            f"volatility not measurable: {m.volatility_note}", FinalDecision.EXECUTE))
    if m is None or m.volatility is None:
        return
    if m.volatility_confidence == "LOW_CONFIDENCE":
        out.append(_finding(RiskCategory.DATA, "VOLATILITY_LOW_CONFIDENCE", RiskLevel.HIGH,
                            f"volatility {m.volatility:.1%} estimated from few trades ({m.volatility_note}); "
                            "the stop is sized from it, so entry needs approval", FinalDecision.REQUIRE_MANUAL_APPROVAL))
    if m.volatility >= Decimal("0.10"):
        out.append(_finding(RiskCategory.MARKET, "HIGH_VOLATILITY", RiskLevel.HIGH,
                            f"window volatility {m.volatility:.1%}", FinalDecision.REDUCE_SIZE))
    elif m.volatility >= Decimal("0.05"):
        out.append(_finding(RiskCategory.MARKET, "ELEVATED_VOLATILITY", RiskLevel.MODERATE,
                            f"window volatility {m.volatility:.1%}", FinalDecision.EXECUTE))


def _check_strategy_and_ml(inp: AssessmentInput, s: SafetySettings, out: list[Finding]) -> None:
    sig = inp.signal
    if inp.operator_request:
        # A manual BUY replaces the strategy's entry signal (and the ML
        # confidence floor) with the operator's decision. Custom rules below
        # still apply, and nothing else in the gate is affected.
        out.append(_finding(RiskCategory.STRATEGY, "OPERATOR_BUY_REQUEST", RiskLevel.LOW,
                            "manual BUY requested by the operator: strategy signal not required; every safety check applies",
                            FinalDecision.EXECUTE))
    elif sig is None:
        out.append(_finding(RiskCategory.STRATEGY, "NO_SIGNAL", RiskLevel.LOW, "no strategy signal evaluated", FinalDecision.WAIT))
    elif not sig.qualified:
        out.append(_finding(RiskCategory.STRATEGY, "SIGNAL_NOT_QUALIFIED", RiskLevel.LOW,
                            f"{sig.name} v{sig.version}: " + ("; ".join(sig.reasons) or "no qualifying signal"), FinalDecision.WAIT))
    ml = inp.ml
    if ml is not None and s.min_ml_confidence is not None and ml.confidence < s.min_ml_confidence and not inp.operator_request:
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
    elif live_requested and inp.live_ready is not True:
        out.append(_finding(RiskCategory.ACCOUNT, "LIVE_NOT_READY", RiskLevel.CRITICAL,
                            f"live execution not ready: {inp.live_not_ready_reason or 'readiness not established'}",
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


_TRANSFER_BLOCKERS = {"FREEZE_AUTHORITY", "DEFAULT_FROZEN", "PAUSED", "EXT_NONTRANSFERABLE", "EXT_TRANSFERHOOK",
                      "EXT_PERMANENTDELEGATE"}
_ROUTE_BLOCKERS = {"NO_SELL_ROUTE", "ROUTE_UNVERIFIED", "BOOK_TOO_THIN", "EXIT_IMPACT", "EXECUTION_UNAVAILABLE",
                   "LIQUIDITY_UNKNOWN", "MIGRATION_PENDING"}


def _sellability(inp: AssessmentInput, plan: TradePlan, findings: list[Finding]) -> dict[str, Any]:
    """Can the position realistically be sold? SELLABLE only when a sell was
    simulated or quoted at the planned size and nothing restricts transfers."""
    codes = {f.code for f in findings}
    size = plan.position_size.value if plan.position_size else None
    out: dict[str, Any] = {"route": None, "expected_sell_price": None, "exit_impact_bps": None, "exit_cost_bps":
                           str(plan.exit_cost_bps) if plan.exit_cost_bps is not None else None,
                           "transfer_restrictions": sorted(codes & _TRANSFER_BLOCKERS),
                           "blocking": sorted(codes & (_ROUTE_BLOCKERS | _TRANSFER_BLOCKERS | {"SELL_TAX_EXCESSIVE", "TAX_UNKNOWN"}))}
    if inp.liquidity_model is not None and size:
        o, c = round_trip_fills(inp.liquidity_model, size, inp.side, only_if_complete=True)
        out["route"] = "exact pool/curve simulation"
        if c is not None and c.complete and o.quantity > 0:
            out["expected_sell_price"] = str((c.quote - c.fee) / o.quantity) if inp.side == "LONG" else None
            out["exit_impact_bps"] = str(c.impact_bps)
            out["sell_simulated"] = True
        else:
            out["sell_simulated"] = False
    elif inp.quote is not None:
        out["route"] = f"{inp.quote.venue.value} quote"
        out["sell_simulated"] = inp.quote.sell_route_available is True
        out["exit_impact_bps"] = str(inp.quote.exit_impact_bps) if inp.quote.exit_impact_bps is not None else None
    else:
        out["sell_simulated"] = False
    if out["blocking"]:
        out["status"] = "NOT SELLABLE"
    elif out.get("sell_simulated"):
        out["status"] = "SELLABLE"
    else:
        out["status"] = "UNKNOWN"
    return out


def _liquidity(inp: AssessmentInput, plan: TradePlan) -> dict[str, Any]:
    m = inp.market
    size = plan.position_size.value if plan.position_size else None
    liq = m.liquidity_quote if m else None
    out: dict[str, Any] = {
        "usable_liquidity": str(liq) if liq is not None else None,
        "position_size": str(size) if size is not None else None,
        "position_to_liquidity": str(size / liq) if size and liq else None,
        "entry_cost_bps": str(plan.entry_cost_bps) if plan.entry_cost_bps is not None else None,
        "exit_cost_bps": str(plan.exit_cost_bps) if plan.exit_cost_bps is not None else None,
        "binding_cap": plan.binding_cap,
        "caps": {k: str(v) for k, v in (getattr(plan, "caps", None) or {}).items()},
    }
    if inp.liquidity_model is not None and size:
        o, c = round_trip_fills(inp.liquidity_model, size, inp.side, only_if_complete=True)
        out["entry_impact_bps"] = str(o.impact_bps)
        out["exit_impact_bps"] = str(c.impact_bps) if c is not None and c.complete else None
    return out


def _live_target(inp: AssessmentInput) -> bool:
    """Whether an executable decision for `inp` goes to the live wallet."""
    return inp.global_mode == GlobalMode.LIVE and inp.strategy_mode != StrategyMode.PAPER and inp.live_trading_permitted


def assess(inp: AssessmentInput, settings: SafetySettings, versions: dict[str, Any] | None = None) -> Assessment:
    required = requirements_for(inp.engine)
    findings: list[Finding] = []

    _mode_findings(inp, findings)
    if inp.side not in ("LONG", "SHORT") or (inp.side == "SHORT" and inp.engine not in SHORTABLE_ENGINES):
        findings.append(_finding(RiskCategory.STRATEGY, "SHORT_NOT_SUPPORTED", RiskLevel.CRITICAL,
                                 f"{inp.engine} cannot open a {inp.side} position", FinalDecision.NO_TRADE, True))
    data_status = _check_data(inp, settings, required, findings)
    _check_token(inp, settings, findings)
    _check_names(inp, settings, findings)
    _check_liquidity(inp, settings, findings)
    _check_holders(inp, settings, findings)
    _check_flow(inp, settings, findings)
    _check_creator_history(inp, settings, findings)
    _check_intel(inp, settings, findings)
    _check_scanner_intel(inp, settings, findings)
    _check_observation(inp, findings)
    _check_entry_exit(inp, findings)
    _check_entry_quality(inp, findings)
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
        targets=inp.targets,
        fixed_cost_quote=inp.fixed_cost_quote if (_live_target(inp) or inp.paper_fixed_costs) else None,
        fixed_cost_detail=inp.fixed_cost_detail,
    )
    findings.extend(plan.findings)
    _check_execution(inp, settings, plan, findings)
    migrated_liq = migrated_liquidity_report(inp, settings, plan) if _migrated_applies(inp) or inp.engine in CURVE_ENGINES else None
    if migrated_liq is not None:
        _check_migrated_liquidity(migrated_liq, settings, findings)
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

    if decision == FinalDecision.REQUIRE_MANUAL_APPROVAL and inp.strategy_mode == StrategyMode.AUTO \
            and inp.global_mode != GlobalMode.MANUAL:
        # AUTO has no approval step: anything that would need a human is a no-trade.
        waiting = [f.code for f in findings if f.action == FinalDecision.REQUIRE_MANUAL_APPROVAL]
        findings.append(_finding(RiskCategory.STRATEGY, "AUTO_NO_APPROVAL", RiskLevel.HIGH,
                                 "AUTO mode never waits for approval; not executable because: " + ", ".join(waiting),
                                 FinalDecision.NO_TRADE))
        decision = FinalDecision.NO_TRADE

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
    elif _live_target(inp):
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
        reports={"tax": {**tax_report(inp.token, settings), "decision": _tax_decision(inp, findings)},
                 "sellability": _sellability(inp, plan, findings), "liquidity": _liquidity(inp, plan),
                 **({"migrated_liquidity": migrated_liq} if migrated_liq is not None else {}),
                 **({"creator_history": creator_report} if (creator_report := creator_history_report(inp, settings)) else {})},
    )


def _tax_decision(inp: AssessmentInput, findings: list[Finding]) -> str:
    """What the tax check alone decided: NO_TRADE above the limit; an unknown
    tax is NO_TRADE in AUTO (no approval step) and needs approval otherwise."""
    codes = {f.code for f in findings}
    if codes & {"BUY_TAX_EXCESSIVE", "SELL_TAX_EXCESSIVE"}:
        return FinalDecision.NO_TRADE.value
    if "TAX_UNKNOWN" in codes:
        auto = inp.strategy_mode == StrategyMode.AUTO and inp.global_mode != GlobalMode.MANUAL
        return FinalDecision.NO_TRADE.value if auto else FinalDecision.REQUIRE_MANUAL_APPROVAL.value
    return "PASS"


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
