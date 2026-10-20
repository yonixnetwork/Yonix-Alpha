"""Strategy modes as the operator sets them on the dashboard, and what each
one actually does right now.

The mode (OFF / MANUAL / PAPER / AUTO) is the strategy's automation; it is
never an authorization for real money. A real order also needs the global
mode LIVE and the server's environment locks (TRADING_ENABLED,
LIVE_TRADING_ENABLED, PAPER_TRADING=false), which only the server can open,
and the live worker's readiness. AUTO therefore runs on paper unless all of
those hold, and `execution_view` says which one applies and why.

Switching a strategy to AUTO needs an explicit confirmation (the request
must carry confirm_auto=true): AUTO is the one mode that can place real
orders without a person approving each one, once the live locks are open.
The confirmation, the previous mode and the user are in the audit log.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core.safety import store
from yonixalpha_core.safety.models import GlobalMode, StrategyMode

OFF_NOTE = ("No new entries and no entry orders. Open positions keep their stops, take-profits and exit "
            "management; switching OFF never sells anything.")


class ModeChangeRefused(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def execution_view(mode: StrategyMode, global_mode: GlobalMode, live_permitted: bool) -> dict[str, Any]:
    """{effective_execution: NONE / PAPER / LIVE / APPROVAL, live_authorized, reason}."""
    live_authorized = global_mode == GlobalMode.LIVE and live_permitted
    why_not_live = ("global mode is LIVE but the server's environment locks are closed" if global_mode == GlobalMode.LIVE
                    else f"global mode is {global_mode.value}" + ("" if live_permitted else " and the environment locks are closed"))
    if mode == StrategyMode.OFF:
        return {"effective_execution": "NONE", "live_authorized": live_authorized, "reason": OFF_NOTE}
    if mode == StrategyMode.PAPER:
        return {"effective_execution": "PAPER", "live_authorized": live_authorized,
                "reason": "PAPER: simulated fills on the paper account; never a real transaction"}
    if mode == StrategyMode.MANUAL:
        return {"effective_execution": "APPROVAL", "live_authorized": live_authorized,
                "reason": "each entry waits for your approval; " + ("approved entries are LIVE" if live_authorized
                                                                     else f"approved entries are PAPER ({why_not_live})")}
    if live_authorized:
        return {"effective_execution": "LIVE", "live_authorized": True,
                "reason": "AUTO with global LIVE and the environment locks open: entries are real orders "
                          "(still only when the live worker is ready and every safety check passes)"}
    return {"effective_execution": "PAPER", "live_authorized": False,
            "reason": f"AUTO runs on PAPER: live execution unavailable ({why_not_live})"}


async def change_mode(session: AsyncSession, strategy: str, mode: StrategyMode, user_id: uuid.UUID | None,
                      confirm_auto: bool = False) -> StrategyMode:
    """Validated, audited mode change (store.set_strategy_mode writes the
    audit row with the previous and new mode). Returns the previous mode."""
    before = await store.load_strategy_mode(session, strategy)
    if mode == StrategyMode.AUTO and before != StrategyMode.AUTO and not confirm_auto:
        raise ModeChangeRefused(
            "AUTO needs confirmation (confirm_auto=true): AUTO trades without asking you, and it places real orders "
            "whenever the global mode is LIVE and the server's live locks are open")
    await store.set_strategy_mode(session, strategy, mode, user_id,
                                  {"auto_confirmed": True} if mode == StrategyMode.AUTO and before != StrategyMode.AUTO else None)
    return before
