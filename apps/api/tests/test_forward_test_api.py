"""Positions list and forward test over the API: booked PnL for closed
positions only, a period starts only with a label, is audited, keeps the
previous one, and changes no trading setting."""

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select

from yonixalpha_core.db.models import AuditLog, PaperPosition, PlatformSetting

NOW = datetime.now(timezone.utc).replace(microsecond=0)


def pos(status, pnl=None):
    return PaperPosition(id=uuid.uuid4(), symbol="T", provider="paper", side="LONG", engine="solana_momentum",
                         asset_id="MINT", entry_price=Decimal(1), quantity=Decimal(1), take_profit=[], status=status,
                         execution_mode="PAPER", realized_pnl=pnl, fees_paid_quote=Decimal("0.001"),
                         entry_cost_quote=Decimal("0.1"), entry_at=NOW - timedelta(minutes=10),
                         exit_at=NOW - timedelta(minutes=5) if status == "closed" else None,
                         exit_reason="stop_loss" if status == "closed" else None, plan={})


async def test_positions_and_forward_test(app, client, auth_headers):
    async with app.state.db_session_factory() as s:
        s.add_all([pos("closed", Decimal("-0.01")), pos("open")])
        await s.commit()
        settings_before = (await s.execute(select(func.count()).select_from(PlatformSetting))).scalar_one()

    r = (await client.get("/api/analytics/solana-positions", params={"engine": "solana_momentum"}, headers=auth_headers)).json()
    closed = next(p for p in r["positions"] if p["status"] == "closed")
    opened = next(p for p in r["positions"] if p["status"] == "open")
    assert closed["net_pnl_sol"] == -0.01 and closed["gross_pnl_sol"] == -0.009
    assert opened["net_pnl_sol"] is None and opened["exit_reason"] is None
    assert (await client.get("/api/analytics/solana-positions", params={"engine": "evm_bsc"},
                             headers=auth_headers)).status_code == 422

    assert (await client.post("/api/analytics/forward-test/start", json={"label": ""}, headers=auth_headers)).status_code == 422
    first = (await client.post("/api/analytics/forward-test/start", json={"label": "first period"}, headers=auth_headers)).json()
    second = await client.post("/api/analytics/forward-test/start", json={"label": "after deploy"}, headers=auth_headers)
    assert second.status_code == 200 and second.json()["rules"]["min_closed_trades"] == 30
    hist = (await client.get("/api/analytics/forward-test/history", headers=auth_headers)).json()["periods"]
    assert [h["label"] for h in hist] == ["after deploy", "first period"] and hist[1]["ended_at"]
    assert first["started_by"] is not None
    async with app.state.db_session_factory() as s:
        ev = (await s.execute(select(AuditLog.event_type).where(AuditLog.event_type.like("forward_test.%")))).scalars().all()
        after = (await s.execute(select(func.count()).select_from(PlatformSetting))).scalar_one()
    assert ev.count("forward_test.started") == 2 and ev.count("forward_test.start_requested") == 2
    assert after == settings_before + 1  # only the forward-test record itself


async def test_forward_test_needs_login(client):
    assert (await client.post("/api/analytics/forward-test/start", json={"label": "x" * 5})).status_code == 401
    assert (await client.get("/api/analytics/solana-positions")).status_code == 401
