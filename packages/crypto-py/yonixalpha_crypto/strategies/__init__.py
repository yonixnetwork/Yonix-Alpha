"""Strategy modules. A strategy turns closed candles into a Decision; it has
no access to the database, the network or any credential. The runner
(yonixalpha_crypto.runner) does risk validation and demo execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

from yonixalpha_crypto.candles import Candle
from yonixalpha_crypto.registry import StrategySpec


@dataclass
class Decision:
    side: str  # LONG | SHORT | NONE
    action: str  # ENTER | EXIT | HOLD | NONE
    stop_price: Decimal | None = None
    target_price: Decimal | None = None
    reasons: list[str] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)


class Strategy(Protocol):
    spec: StrategySpec

    def required_candles(self, params: dict[str, Any]) -> int: ...

    def traded_symbol(self, symbols: list[str]) -> str: ...

    def evaluate(self, candles: dict[str, list[Candle]], params: dict[str, Any], position_open: bool) -> Decision: ...


IMPLEMENTATIONS: dict[str, Strategy] = {}


def implementation(strategy_id: str) -> Strategy | None:
    return IMPLEMENTATIONS.get(strategy_id)


# Built-in strategy modules register themselves on import.
from yonixalpha_crypto.strategies import meta_muse  # noqa: E402,F401
from yonixalpha_crypto.strategies import library  # noqa: E402,F401  (the 20-module library)
