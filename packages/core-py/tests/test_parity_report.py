"""The read-only paper vs live / copy report runs on a real schema with
paper and live positions, live orders, decisions and copy events."""
import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://yonixalpha:yonixalpha_test_pw@localhost:5432/yonixalpha_test")
os.environ.setdefault("JWT_SECRET", "test-secret-test-secret-test-secret-32")
os.environ.setdefault("ADMIN_PASSWORD_HASH", "x")

from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from yonixalpha_core.db.base import Base, make_session_factory  # noqa: E402
from yonixalpha_core.db.models import CopyEvent, CopyTarget, ExecutionOrder, PaperPosition  # noqa: E402
from yonixalpha_core.tools import parity_report  # noqa: E402

NOW = datetime.now(timezone.utc)


async def _seed():
    engine = create_async_engine(os.environ["DATABASE_URL"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with make_session_factory(engine)() as s:
        for i, (mode, pnl) in enumerate([("PAPER", "0.02"), ("PAPER", "-0.01"), ("LIVE", "-0.004")]):
            s.add(PaperPosition(symbol=f"T{i}", provider="paper", side="LONG", status="closed", engine="solana_fresh",
                                asset_id=f"M{i}", entry_price=Decimal(1), quantity=Decimal(1), entry_at=NOW - timedelta(minutes=10),
                                exit_at=NOW - timedelta(minutes=5), realized_pnl=Decimal(pnl),
                                realized_pnl_pct=Decimal(pnl) * 10, fees_paid_quote=Decimal("0.0003"),
                                exit_reason="stop_loss" if pnl.startswith("-") else "take_profit_1", execution_mode=mode))
        s.add(ExecutionOrder(mode="LIVE", side="SELL", reason="stop_loss", mint="M2", provider="pumpportal_local",
                             route="pump", amount="1", amount_kind="tokens", slippage_pct=Decimal(20),
                             priority_fee_sol=Decimal("0.0001"), status="CONFIRMED", idempotency_key="o1"))
        t = CopyTarget(id=uuid.uuid4(), chain="solana", wallet="W" * 32, label="w", mode="PAPER")
        s.add(t)
        await s.flush()
        s.add(CopyEvent(target_id=t.id, chain="solana", wallet=t.wallet, token="TOK", side="BUY", source_event_id="e1",
                        target_token_amount=Decimal(1), target_quote_amount=Decimal(1), target_at=NOW - timedelta(minutes=3),
                        detected_at=NOW - timedelta(minutes=3), decision="SKIPPED", reason="PRICE_MOVED_TOO_FAR: +40%",
                        latency_ms={"detection": 900, "decision": 120, "total": 1020}))
        await s.commit()
    await engine.dispose()


def test_report_runs_and_separates_paper_from_live(capsys):
    asyncio.run(_seed())
    assert asyncio.run(parity_report.main(["--days", "1"])) == 0
    out = capsys.readouterr().out
    assert "PAPER n=2" in out and "win rate 50.0%" in out and "LIVE  n=1" in out
    assert "SELL CONFIRMED" in out and "paper charged LIVE fixed costs: True" in out
    assert "solana     BUY  SKIPPED       1  PRICE_MOVED_TOO_FAR" in out and "detection 900" in out
    assert "Nothing was written." in out
