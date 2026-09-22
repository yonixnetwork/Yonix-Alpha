import json

import httpx
import pytest

from yonixalpha_core.config import Settings
from yonixalpha_core.notify import send_telegram_alert

pytestmark = pytest.mark.asyncio


def _settings(**overrides) -> Settings:
    base = {"JWT_SECRET": "x" * 32, "ADMIN_PASSWORD_HASH": "unused"}
    base.update(overrides)
    return Settings(**base)


async def test_skips_silently_when_credentials_unset():
    settings = _settings(TELEGRAM_BOT_TOKEN=None, TELEGRAM_CHAT_ID=None)
    sent = await send_telegram_alert(settings, "hello")
    assert sent is False


async def test_skips_silently_when_only_token_set():
    settings = _settings(TELEGRAM_BOT_TOKEN="123:abc", TELEGRAM_CHAT_ID=None)
    sent = await send_telegram_alert(settings, "hello")
    assert sent is False


async def test_posts_to_the_real_bot_api_url_with_correct_payload():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.read())
        return httpx.Response(200, json={"ok": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = _settings(TELEGRAM_BOT_TOKEN="123456:abc-token", TELEGRAM_CHAT_ID="999")

    sent = await send_telegram_alert(settings, "kill switch engaged", client=client)

    assert sent is True
    assert captured["url"] == "https://api.telegram.org/bot123456:abc-token/sendMessage"
    assert captured["body"] == {"chat_id": "999", "text": "kill switch engaged"}
    await client.aclose()


async def test_returns_false_on_non_200_response():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"ok": False, "description": "bad request"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = _settings(TELEGRAM_BOT_TOKEN="123:abc", TELEGRAM_CHAT_ID="999")

    sent = await send_telegram_alert(settings, "x", client=client)

    assert sent is False
    await client.aclose()


async def test_returns_false_on_network_error_rather_than_raising():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = _settings(TELEGRAM_BOT_TOKEN="123:abc", TELEGRAM_CHAT_ID="999")

    sent = await send_telegram_alert(settings, "x", client=client)

    assert sent is False
    await client.aclose()
