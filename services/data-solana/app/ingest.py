from datetime import datetime, timezone

from yonixalpha_core.schemas.market import NormalizedMarketEvent

# Deliberately NOT attempting to decode pump.fun/launch-platform-specific
# instruction data here (mint address, creator, buy/sell amounts). Doing
# that correctly requires the exact program ID and instruction layout for
# whatever launch platform is being watched, verified against its current
# documentation — this environment has no outbound network access to verify
# that (see ARCHITECTURE_AUDIT.md), and guessing would mean fabricating
# trading-relevant data, which the spec explicitly forbids (section 53).
# Engine A/B (Phase 3) own that decoding once built against verified,
# version-pinned program IDs; this stays a generic, source-agnostic Solana
# JSON-RPC/WS normalizer.


def normalize_slot_notification(message: dict) -> NormalizedMarketEvent | None:
    """A slotSubscribe notification: {"params": {"result": {"parent", "root", "slot"}}}.
    Every field here is unambiguous and part of the stable Solana JSON-RPC
    contract — no source-specific interpretation needed.
    """
    result = message.get("params", {}).get("result")
    if not result or "slot" not in result:
        return None
    return NormalizedMarketEvent(
        source="solana",
        symbol="slot",
        snapshot_type="slot",
        occurred_at=datetime.now(timezone.utc),
        sequence=result["slot"],
        payload=result,
    )


def normalize_logs_notification(message: dict) -> NormalizedMarketEvent | None:
    """A logsSubscribe notification: {"params": {"result": {"context": {"slot"}, "value": {"signature", "err", "logs"}}}}.
    Stored as a raw, undecoded observation — see module docstring. No
    `sequence` is set: a transaction signature has no natural ordinal, and a
    wrong stand-in (e.g. the containing slot) would silently break the
    dedup constraint since multiple signatures share a slot.
    """
    params = message.get("params", {}).get("result")
    if not params:
        return None
    value = params.get("value", {})
    signature = value.get("signature", "unknown")
    return NormalizedMarketEvent(
        source="solana",
        symbol=signature,
        snapshot_type="log_notification",
        occurred_at=datetime.now(timezone.utc),
        payload=params,
    )
