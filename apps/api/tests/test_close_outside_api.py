"""Closing LIVE positions whose tokens were sold or moved outside the system
(2026-10-07: a token sold in a wallet app kept its position open and the
exit loop retried the sell thousands of times). The wallet is read on chain
first; a token still in the wallet is never closed this way; the realized
PnL stays unknown."""
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select

from app.api.routes import trade
from yonixalpha_core.db.models import ExecutionOrder, PaperPosition, TradeTimelineEvent

NOW = datetime(2026, 10, 7, 7, 0, tzinfo=timezone.utc)


def _pos(symbol, mint, status="open", quantity=Decimal("1000")):
    return PaperPosition(symbol=symbol, provider="pumpportal", side="LONG", status=status, engine="solana_fresh",
                         asset_id=mint, entry_price=Decimal("0.00000003"), quantity=quantity, remaining_quantity=quantity,
                         entry_at=NOW, execution_mode="LIVE", proceeds_quote=Decimal("0.004"))


def _order(position, status):
    return ExecutionOrder(position_id=position.id, mode="LIVE", side="SELL", reason="trailing_stop", mint=position.asset_id,
                          provider="pumpportal_local", route="pump-amm", amount="1000", amount_kind="tokens",
                          slippage_pct=Decimal("10"), priority_fee_sol=Decimal("0.0001"), status=status,
                          idempotency_key=f"k-{position.symbol}-{status}")


async def test_close_outside_reads_the_wallet_and_never_closes_a_held_token(app, client, auth_headers, monkeypatch):
    held = {"HELD1111111111111111111111111111111111111111": {"amount": 500, "decimals": 6}}

    async def wallet_tokens(db, redis, settings):
        return held

    monkeypatch.setattr(trade, "_wallet_tokens", wallet_tokens)
    async with app.state.db_session_factory() as s:
        gone = _pos("GONE", "GONE1111111111111111111111111111111111111111")
        review = _pos("REVIEW", "REVW1111111111111111111111111111111111111111", status="needs_review", quantity=Decimal(0))
        kept = _pos("HELD", "HELD1111111111111111111111111111111111111111")
        busy = _pos("BUSY", "BUSY1111111111111111111111111111111111111111")
        s.add_all([gone, review, kept, busy])
        await s.flush()
        s.add_all([_order(gone, "PENDING"), _order(busy, "SUBMITTED")])
        await s.commit()
        ids = {p.symbol: p.id for p in (gone, review, kept, busy)}

    assert (await client.post(f"/api/trade/close-outside/{ids['GONE']}", headers=auth_headers)).status_code == 422
    r = await client.post(f"/api/trade/close-outside/{ids['HELD']}?confirm=true", headers=auth_headers)
    assert r.status_code == 409 and "still holds 500" in r.json()["detail"]
    r = await client.post(f"/api/trade/close-outside/{ids['GONE']}?confirm=true", headers=auth_headers)
    assert r.status_code == 200 and r.json()["status"] == "closed"

    assert (await client.post("/api/trade/close-outside-all", json={"confirm": "yes"}, headers=auth_headers)).status_code == 422
    r = (await client.post("/api/trade/close-outside-all", json={"confirm": "CLOSE SOLD OUTSIDE"}, headers=auth_headers)).json()
    assert [c["symbol"] for c in r["closed"]] == ["REVIEW"]
    assert {k["symbol"] for k in r["kept"]} == {"HELD", "BUSY"}
    assert any("signed or submitted" in k["why"] for k in r["kept"])

    async with app.state.db_session_factory() as s:
        g = await s.get(PaperPosition, ids["GONE"])
        assert (g.status, g.exit_reason, g.realized_pnl, g.exit_price) == ("closed", "sold_outside", None, None)
        assert g.proceeds_quote == Decimal("0.004")  # earlier sells of ours are kept, nothing invented
        assert (await s.get(PaperPosition, ids["REVIEW"])).status == "failed"  # never held tokens: not "closed"
        assert (await s.get(PaperPosition, ids["HELD"])).status == "open"
        assert (await s.get(PaperPosition, ids["BUSY"])).status == "open"
        orders = {o.idempotency_key: o.status for o in (await s.execute(select(ExecutionOrder))).scalars()}
        assert orders == {"k-GONE-PENDING": "CANCELLED", "k-BUSY-SUBMITTED": "SUBMITTED"}
        ev = (await s.execute(select(TradeTimelineEvent).where(TradeTimelineEvent.position_id == ids["GONE"]))).scalars().all()
        assert [e.event_type for e in ev] == ["closed_outside"]


async def test_close_outside_fails_closed_without_an_on_chain_answer(app, client, auth_headers):
    async with app.state.db_session_factory() as s:
        p = _pos("NOWALLET", "NOWL1111111111111111111111111111111111111111")
        s.add(p)
        await s.commit()
        pid = p.id
    r = await client.post(f"/api/trade/close-outside/{pid}?confirm=true", headers=auth_headers)
    assert r.status_code == 503  # the wallet address is unknown: nothing is closed
    async with app.state.db_session_factory() as s:
        assert (await s.get(PaperPosition, pid)).status == "open"
