from dataclasses import dataclass
from enum import StrEnum


class ExecutionProvider(StrEnum):
    """Legacy (pre-gate) routing, used only by paper-trading's app/entry.py
    for Phase 5 candidates. Gate-driven Pump.fun trades do not use it: PAPER
    fills come from paper_engine and LIVE fills from live_trading.

    Per spec section 20: ExecutionProvider -> SolanaBondingCurveExecutor
    | JupiterExecutor | BinanceFuturesExecutor. UNSUPPORTED is this
    codebase's honest fourth option — see route()'s docstring for why it's
    the common case for Solana candidates today.
    """

    JUPITER = "jupiter"
    BONDING_CURVE = "bonding_curve"
    BINANCE_FUTURES = "binance_futures"
    UNSUPPORTED = "unsupported"


@dataclass
class RoutingContext:
    asset_class: str  # "solana" | "binance_futures"
    # True only when a *verified* migration parser (see
    # services/engine-solana-migration/app/detect.py) actually confirmed
    # this token graduated to an AMM pool — never inferred from anything
    # weaker (e.g. "an engine=migration candidate row exists"), since
    # Engine B currently ships with zero registered parsers and so never
    # produces a real confirmation.
    migration_confirmed: bool = False


def route(context: RoutingContext) -> tuple[ExecutionProvider, list[str]]:
    """Pure classification: which executor should handle this candidate.
    This is a routing DECISION, not itself an execution call — a router
    result gets handed to whichever process owns that executor (today,
    only services/engine-binance-futures actually exists as a working
    executor; Jupiter/bonding-curve executors were never built in any
    prior phase, since implementing real Solana swap execution needs the
    same live-network verification this environment cannot do — see
    ARCHITECTURE_AUDIT.md). A Solana candidate therefore honestly routes
    to UNSUPPORTED today unless a verified migration parser confirmed an
    AMM pool exists for it, and even then, no JupiterExecutor is actually
    wired up on the other end yet — a future phase's job.
    """
    if context.asset_class == "binance_futures":
        return ExecutionProvider.BINANCE_FUTURES, ["USDT-M Futures order via engine-binance-futures"]

    if context.asset_class == "solana":
        if context.migration_confirmed:
            return (
                ExecutionProvider.JUPITER,
                ["migration confirmed by a verified AMM parser — would route to a Jupiter/DEX executor (not yet implemented)"],
            )
        return (
            ExecutionProvider.UNSUPPORTED,
            [
                "no verified post-migration AMM pool confirmed for this token, and no bonding-curve-native "
                "executor exists in this codebase — cannot execute"
            ],
        )

    return ExecutionProvider.UNSUPPORTED, [f"unknown asset_class {context.asset_class!r}"]
