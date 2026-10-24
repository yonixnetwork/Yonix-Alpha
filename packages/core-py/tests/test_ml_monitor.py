"""ML engine monitoring: states in plain words from saved records only,
predictions counted apart from executed paper positions, the event history
signal -> decision -> entry -> exit -> evaluation, and nothing invented."""

import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://yonixalpha:yonixalpha_test_pw@localhost:5432/yonixalpha_test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/9")
os.environ.setdefault("JWT_SECRET", "test-secret-test-secret-test-secret-32")
os.environ.setdefault("ADMIN_PASSWORD_HASH", "x")

import pytest  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from yonixalpha_core import ml_monitor as mm  # noqa: E402
from yonixalpha_core.db.base import Base, make_session_factory  # noqa: E402
from yonixalpha_core.db.models import (  # noqa: E402
    AuditLog, MlExperiment, ModelVersion, OpportunityOutcome, PaperPosition,
)

NOW = datetime.now(timezone.utc).replace(microsecond=0)


def test_model_states_are_named_from_the_saved_validation_only():
    ch = SimpleNamespace(version=2, name="m")
    assert mm._model_status(None, ch, {"status": "PASS"})[0] == "AWAITING MY APPROVAL"
    assert "never automatic" in mm._model_status(None, ch, {"status": "PASS"})[1]
    assert mm._model_status(None, ch, {"status": "FAIL", "reason": "AUC below 0.55"}) == \
        ("VALIDATION FAILED", "challenger v2: AUC below 0.55")
    assert mm._model_status(None, ch, None)[0] == "INSUFFICIENT DATA"
    assert mm._model_status(SimpleNamespace(version=1), None, None)[0] == "CHAMPION IN USE"
    assert mm._model_status(None, None, None)[0] == "OBSERVING"


@pytest.fixture
async def sf():
    engine = create_async_engine(os.environ["DATABASE_URL"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield make_session_factory(engine)
    await engine.dispose()


@pytest.mark.asyncio
async def test_overview_and_history_from_saved_records(sf):
    async with sf() as s:
        s.add_all([
            ModelVersion(name="shadow_x", version=3, status="shadow", feature_names=[], training_sample_count=500,
                         metrics={}, artifact=b"x"),
            MlExperiment(idea="fresh tokens with liquidity above 30 SOL", status="DRAFT", proposal={}, testable=True),
            MlExperiment(idea="migrated, qualified wallets", status="ACCEPTED", proposal={"categories": ["MIGRATED"]},
                         testable=True, decided_at=NOW - timedelta(hours=1)),
            AuditLog(user_id=None, event_type="ml_lab.experiment_proposed", detail={"id": "x", "idea": "i"}),
        ])
        p = PaperPosition(id=uuid.uuid4(), symbol="T", provider="paper", side="LONG", engine="solana_momentum",
                          entry_price=Decimal(1), quantity=Decimal(1), take_profit=[], status="closed",
                          execution_mode="PAPER", realized_pnl=Decimal("-0.004"), fees_paid_quote=Decimal("0.001"),
                          entry_at=NOW - timedelta(minutes=30), exit_at=NOW - timedelta(minutes=20), exit_reason="stop_loss",
                          plan={})
        s.add(p)
        await s.flush()
        s.add_all([
            OpportunityOutcome(key="t", mint="MT", engine="solana_momentum", stage="GATE", decision="EXECUTE", traded=True,
                               position_id=p.id, decided_at=NOW - timedelta(minutes=31), snapshot={}, status="COMPLETE",
                               executable_return_pct=Decimal("-6.5"), peak_pct=Decimal("3")),
            OpportunityOutcome(key="r", mint="MR", engine="solana_momentum", stage="GATE", decision="REJECT", traded=False,
                               reasons=["FEW_BUYERS: 3"], decided_at=NOW - timedelta(minutes=10), snapshot={},
                               status="TRACKING"),
        ])
        await s.commit()
        o = await mm.overview(s, None, NOW)
        assert [m["status"] for m in o["models"]] == ["OBSERVING"]
        assert o["experiment_counts"]["DRAFT"] == 1 and o["experiment_counts"]["ACCEPTED"] == 1
        by_idea = {e["idea"]: e["status"] for e in o["experiments"]}
        assert by_idea["fresh tokens with liquidity above 30 SOL"] == "AWAITING MY APPROVAL"
        assert by_idea["migrated, qualified wallets"] == "OBSERVING"  # accepted, too few signals yet
        assert o["signals"] == {"total": 0, "evaluated": 0, "pending": 0, "in_window": 0}
        closed = o["paper_trades"]["solana_momentum"]["closed"]
        assert closed["trades"] == 1 and closed["net_pnl_sol"] == -0.004  # executed positions, apart from predictions
        assert o["rejected"]["decisions"] == 1 and o["audit"]["counts"] == {"ml_lab.experiment_proposed": 1}
        assert o["forward_test"]["period"] is None and o["data_sources"] is None  # not recorded: shown as such

        h = await mm.history(s, now=NOW)
        traded = next(r for r in h if r["traded"])
        assert [x["step"] for x in traded["steps"]] == ["decision", "entry", "exit", "evaluation"]
        assert "net -0.00400 SOL" in traded["steps"][2]["text"] and "-6.50 %" in traded["steps"][3]["text"]
        refused = next(r for r in h if not r["traded"])
        assert "FEW_BUYERS" in refused["steps"][0]["text"] and "not measured yet" in refused["steps"][-1]["text"]
        assert len(await mm.history(s, traded=True, now=NOW)) == 1
