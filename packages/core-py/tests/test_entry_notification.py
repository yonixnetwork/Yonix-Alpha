"""An entry notification says whether the order is real: a LIVE entry used
to be announced as "Paper entry"."""

from decimal import Decimal
from types import SimpleNamespace

from yonixalpha_core import events
from yonixalpha_core.safety import pipeline


async def _titles(monkeypatch, mode):
    sent = []

    async def fake_notify(session, redis, settings, kind, title, body="", severity="info", data=None):
        sent.append((title, body))

    async def fake_publish(*a, **kw):
        return None

    monkeypatch.setattr(events, "notify", fake_notify)
    monkeypatch.setattr(events, "publish", fake_publish)
    a = SimpleNamespace(engine="solana_fresh", symbol="STRAK",
                        plan=SimpleNamespace(position_size=SimpleNamespace(value=Decimal("0.0014")),
                                             stop_loss=SimpleNamespace(value=Decimal("2.4e-7"))))
    position = SimpleNamespace(id="p", side="LONG", account_id="acc", execution_mode=mode, entry_price=Decimal("3e-7"))
    await pipeline.after_entry(None, None, None, a, position)
    return sent


async def test_live_entry_is_announced_as_a_live_order(monkeypatch):
    (title, body), = await _titles(monkeypatch, "LIVE")
    assert title == "LIVE BUY submitted: STRAK" and "real order" in body and "Paper" not in title


async def test_paper_entry_keeps_its_label(monkeypatch):
    (title, _), = await _titles(monkeypatch, "PAPER")
    assert title == "Paper entry: STRAK LONG"
