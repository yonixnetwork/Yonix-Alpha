import httpx

from yonixalpha_core.config import Settings
from yonixalpha_core.logging import get_logger
from yonixalpha_core.redact import redact_text

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
