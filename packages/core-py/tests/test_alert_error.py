"""Every error reaches Telegram, without flooding it and without leaking keys."""

from types import SimpleNamespace

from yonixalpha_core import notify

S = SimpleNamespace(TELEGRAM_BOT_TOKEN="123:abc", TELEGRAM_CHAT_ID="42")


async def test_sent_scrubbed_throttled_and_counted(monkeypatch):
    sent: list[str] = []

    async def fake_send(settings, text, client=None):
        sent.append(text)
        return True

    monkeypatch.setattr(notify, "send_telegram_alert", fake_send)
    monkeypatch.setattr(notify, "_last_sent", {})
    monkeypatch.setattr(notify, "_suppressed", {})
    url = "https://fapi.binance.com/fapi/v1/order?symbol=X&signature=deadbeef&api-key=SECRET"
    assert await notify.alert_error("svc", "boom", {"url": url, "error": f"failed at {url}"}, S, now=0.0)
    assert "deadbeef" not in sent[0] and "SECRET" not in sent[0] and "https://fapi.binance.com/…" in sent[0]
    for i in range(3):  # a tight failure loop inside the window
        assert not await notify.alert_error("svc", "boom", "again", S, now=10.0 + i)
    assert await notify.alert_error("svc", "other", "different event goes out", S, now=11.0)
    assert await notify.alert_error("svc", "boom", "later", S, now=10.0 + notify.ALERT_THROTTLE_SECONDS)
    assert len(sent) == 3 and "(+3 more of the same" in sent[-1]


async def test_never_raises(monkeypatch):
    async def broken(settings, text, client=None):
        raise RuntimeError("telegram down")

    monkeypatch.setattr(notify, "send_telegram_alert", broken)
    monkeypatch.setattr(notify, "_last_sent", {})
    assert await notify.alert_error("svc", "x", "y", S, now=0.0) is False
