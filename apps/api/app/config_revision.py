"""Bumps the runtime configuration revision after every successful settings
write, in one place for every settings route (present and future), and
returns it to the dashboard in the X-Config-Revision response header.

Pure ASGI (no request-body buffering). The route has already committed its
own transaction when it starts the response, so the revision always refers
to persisted settings; the change event is published after this commit.
"""

import re

import jwt

from yonixalpha_core import runtime_config
from yonixalpha_core.logging import get_logger
from yonixalpha_core.security import decode_token

log = get_logger("api.config_revision")

HEADER = b"x-config-revision"
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
# Settings writes only. Actions (approvals, paper-account resets, position
# exits, manual trades) are not configuration.
CONFIG_ROUTES = [
    (re.compile(r"^/api/control/modes/(global|strategy/(?P<strategy>[^/]+))$"), "mode"),
    (re.compile(r"^/api/control/settings/(?P<scope>[^/]+)(/follow-global)?$"), "risk_settings"),
    (re.compile(r"^/api/control/settings-all$"), "risk_settings"),
    (re.compile(r"^/api/control/blacklist(/.*)?$"), "blacklist"),
    (re.compile(r"^/api/control/rules(/.*)?$"), "rules"),
    (re.compile(r"^/api/strategies/(?P<strategy>[^/]+)/(mode|config)$"), "strategy"),
    (re.compile(r"^/api/live/settings$"), "live_settings"),
    (re.compile(r"^/api/paper/execution-settings$"), "paper_execution_settings"),
    (re.compile(r"^/api/notifications/prefs$"), "notification_prefs"),
    (re.compile(r"^/api/risk/kill-switch/(engage|disengage)$"), "kill_switch"),
    (re.compile(r"^/api/rpc/providers(/env/[^/]+|/[^/]+)?$"), "rpc_providers"),  # not .../test
    (re.compile(r"^/api/rpc/evm/[^/]+$"), "rpc_providers"),  # BSC / Robinhood .env / public endpoint toggles
    (re.compile(r"^/api/evm/settings$"), "evm_trading"),
    (re.compile(r"^/api/evm/coordination-settings$"), "launch_coordination"),
    (re.compile(r"^/api/evm/observation-settings$"), "evm_observation"),
    (re.compile(r"^/api/copy/targets(/[^/]+)?$"), "copy_targets"),
    (re.compile(r"^/api/wallets/validation-settings$"), "wallet_validation"),
    # Trading switches and launchpad modes (not the close-positions / emergency-exit actions).
    (re.compile(r"^/api/controls/(?!close-positions$|emergency-exit$)(?P<control>.+)$"), "trading_controls"),
]


def classify(method: str, path: str) -> dict | None:
    if method not in WRITE_METHODS:
        return None
    for pattern, kind in CONFIG_ROUTES:
        m = pattern.match(path)
        if m:
            return {"kind": kind, "method": method, "path": path, **{k: v for k, v in m.groupdict().items() if v}}
    return None


class ConfigRevisionMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        change = classify(scope.get("method", ""), scope.get("path", ""))
        if change is None:
            return await self.app(scope, receive, send)

        async def send_wrapper(message):
            if message["type"] == "http.response.start" and 200 <= message["status"] < 300:
                value = await self._bump(scope, change)
                if value is not None:
                    headers = list(message.get("headers", []))
                    headers.append((HEADER, str(value["revision"]).encode()))
                    headers.append((b"access-control-expose-headers", HEADER))
                    message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_wrapper)

    async def _bump(self, scope, change: dict) -> dict | None:
        app = scope["app"]
        try:
            actor = _username(app.state.settings, scope)
            async with app.state.db_session_factory() as session:
                value = await runtime_config.bump(session, change, actor)
                await session.commit()
            await runtime_config.announce(app.state.redis, value)
            return value
        except Exception as exc:  # noqa: BLE001 - the settings write itself already succeeded
            log.error("config.revision_bump_failed", path=change.get("path"), error=f"{type(exc).__name__}: {exc}"[:200])
            return None


def _username(settings, scope) -> str | None:
    for name, value in scope.get("headers", []):
        if name == b"authorization" and value.lower().startswith(b"bearer "):
            try:
                return decode_token(settings, value[7:].decode()).get("sub")
            except (jwt.PyJWTError, UnicodeDecodeError):
                return None
    return None
