"""Demo-only enforcement at the execution boundary.

The engine has two modes, DISABLED and DEMO. There is no LIVE mode to
switch on: validation rejects any other value, the demo executor calls
require_demo() before every simulated fill, and the package has no
credential setting, no signing code and no order endpoint (publichttp only
allows public market-data GETs). A future live mode would need new code,
a separate review and the operator's explicit approval."""

from __future__ import annotations

# DEMO = paper trading (simulated fills on live public data);
# EXCHANGE_DEMO = orders on an exchange's official demo / testnet account
# (private.executor, practice hosts only). There is no LIVE mode to select:
# live orders are locked in code (private.livelock).
MODES = ("DISABLED", "DEMO", "EXCHANGE_DEMO")
ACTIVE_MODES = ("DEMO", "EXCHANGE_DEMO")


class LiveTradingNotAvailable(RuntimeError):
    pass


def require_demo(mode: str) -> None:
    if mode != "DEMO":
        raise LiveTradingNotAvailable(f"mode {mode!r}: only DEMO executes (simulated); there is no live mode")
