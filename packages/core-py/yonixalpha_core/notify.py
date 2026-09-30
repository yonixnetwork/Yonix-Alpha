import re
import time

import httpx

from yonixalpha_core.config import Settings
from yonixalpha_core.logging import get_logger
from yonixalpha_core.redact import redact_text, redact_url

log = get_logger("core.notify")

TELEGRAM_API_BASE = "https://api.telegram.org"
TELEGRAM_TIMEOUT_SECONDS = 10.0


async def send_telegram_alert(settings: Settings, text: str, client: httpx.AsyncClient | None = None) -> bool:
    """Best-effort Telegram notification — never raises. A Telegram outage,
    a bad token, or unset credentials must never block or crash the caller
    (a kill-switch toggle, a service's health-check loop, an auth lockout),
    so every failure mode here is logged and swallowed rather than
    propagated. Returns True only on a confirmed 2xx from the Bot API.

    `client` mirrors the DI pattern `RpcManager.create` already uses in
    `yonixalpha_core.solana.rpc` — tests inject an `httpx.AsyncClient`
    wired to `httpx.MockTransport`; real callers omit it and a short-lived
    client is created and closed here.
    """
    if not getattr(settings, "TELEGRAM_BOT_TOKEN", None) or not getattr(settings, "TELEGRAM_CHAT_ID", None):
        log.debug("telegram.alert.skipped_not_configured")
        return False

    url = f"{TELEGRAM_API_BASE}/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": settings.TELEGRAM_CHAT_ID, "text": text}

    try:
        if client is not None:
            response = await client.post(url, json=payload, timeout=TELEGRAM_TIMEOUT_SECONDS)
        else:
            async with httpx.AsyncClient() as owned_client:
                response = await owned_client.post(url, json=payload, timeout=TELEGRAM_TIMEOUT_SECONDS)
    except httpx.HTTPError as exc:
        # The URL embeds the bot token; never let it reach a log line.
        log.warning("telegram.alert.network_error", error=redact_text(str(exc), [url, settings.TELEGRAM_BOT_TOKEN]))
        return False

    if response.status_code != 200:
        log.warning("telegram.alert.rejected", status_code=response.status_code, body=response.text[:500])
        return False

    log.info("telegram.alert.sent")
    return True


ALERT_THROTTLE_SECONDS = 300
_URL = re.compile(r"(?:https?|wss?)://[^\s'\"<>]+")
_last_sent: dict[str, float] = {}
_suppressed: dict[str, int] = {}


def scrub(text: str) -> str:
    """Every URL in `text` reduced to scheme://host (keys often live in URLs
    and query strings, e.g. RPC api-key or a signed request's signature)."""
    return _URL.sub(lambda m: redact_url(m.group(0)), text)


async def alert_error(service: str, event: str, detail: dict | str | None = None, settings: Settings | None = None,
                      now: float | None = None) -> bool:
    """Sends an error to Telegram. The same (service, event) is sent at most
    once per ALERT_THROTTLE_SECONDS, so a failure repeating in a loop cannot
    flood the chat; the next message says how many were suppressed meanwhile.
    Returns whether a message was sent. Never raises."""
    try:
        from yonixalpha_core.config import get_settings

        settings = settings or get_settings()
        key = f"{service}:{event}"
        t = time.monotonic() if now is None else now
        last = _last_sent.get(key)
        if last is not None and t - last < ALERT_THROTTLE_SECONDS:
            _suppressed[key] = _suppressed.get(key, 0) + 1
            return False
        _last_sent[key] = t
        more = _suppressed.pop(key, 0)
        body = scrub(str(detail))[:1500] if detail else ""
        text = f"[{service}] ERROR: {event}" + (f"\n{body}" if body else "") + (
            f"\n(+{more} more of the same in the last {ALERT_THROTTLE_SECONDS // 60} min)" if more else "")
        return await send_telegram_alert(settings, text)
    except Exception as exc:  # noqa: BLE001 - alerting must never take a service down
        log.warning("telegram.alert_error_failed", error=type(exc).__name__)
        return False
