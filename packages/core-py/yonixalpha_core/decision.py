from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from yonixalpha_core.risk import DataQuality

__all__ = ["DataQuality", "Decision", "DecisionType", "EntryType", "no_trade", "SOLANA_AVAILABLE_DECISIONS"]


class DecisionType(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"
    WAIT = "WAIT"
    NO_TRADE = "NO_TRADE"


class EntryType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    TRIGGER = "TRIGGER"


# Per spec section 51: "For Solana spot/memecoin strategies, SHORT may be
# unavailable depending on the execution venue." Spot positions can't be
# shorted without a borrow/derivatives venue, which nothing in this
# codebase provides for Solana — a signal engine producing Solana
# decisions should never emit SHORT, and this constant is what it checks
# against rather than each call site hardcoding the exclusion separately.
SOLANA_AVAILABLE_DECISIONS = frozenset({DecisionType.LONG, DecisionType.WAIT, DecisionType.NO_TRADE})


@dataclass
class Decision:
    """The structured decision output from spec section 51. `data_quality`
    is the DataQuality enum from yonixalpha_core.risk (section 52's fuller
    definition) rather than the bare float in section 51's illustrative
    JSON — the enum is the actual signal a caller needs to act on ("is this
    confidence number trustworthy"), a raw 0.0-1.0 float would just be a
    second, redundant confidence score.
    """

    decision: DecisionType
    confidence: float
    entry_type: EntryType | None
    entry: Decimal | None
    stop_loss: Decimal | None
    take_profit: list[Decimal]
    risk_score: float
    reason: list[str]
    data_quality: DataQuality

    def to_dict(self) -> dict:
        return {
            "decision": self.decision.value,
            "confidence": self.confidence,
            "entry_type": self.entry_type.value if self.entry_type else None,
            "entry": str(self.entry) if self.entry is not None else None,
            "stop_loss": str(self.stop_loss) if self.stop_loss is not None else None,
            "take_profit": [str(tp) for tp in self.take_profit],
            "risk_score": self.risk_score,
            "reason": self.reason,
            "data_quality": self.data_quality.value,
        }


def no_trade(reason: str, data_quality: DataQuality = DataQuality.UNAVAILABLE) -> Decision:
    """Per spec section 52: "If required data is unavailable: do not
    guess. Return NO_TRADE — insufficient data." The one correct way to
    construct that response — never build a Decision(decision=NO_TRADE,
    ...) by hand with fields quietly left inconsistent.
    """
    return Decision(
        decision=DecisionType.NO_TRADE,
        confidence=0.0,
        entry_type=None,
        entry=None,
        stop_loss=None,
        take_profit=[],
        risk_score=0.0,
        reason=[reason],
        data_quality=data_quality,
    )
