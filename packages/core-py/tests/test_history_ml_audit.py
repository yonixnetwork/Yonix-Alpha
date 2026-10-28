"""Full-history and ML audits (2026-10-27): every position status is counted,
the metrics cover the whole history, losses are attributed to tokens, the
ledger separates traded from refused decisions, and ML predictions are judged
only when they were made before the outcome; usage shows whether any model
could have influenced a decision."""

import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://yonixalpha:yonixalpha_test_pw@localhost:5432/yonixalpha_test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/9")
os.environ.setdefault("JWT_SECRET", "test-secret-test-secret-test-secret-32")
os.environ.setdefault("ADMIN_PASSWORD_HASH", "x")

import pytest  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from yonixalpha_core.db.base import Base, make_session_factory  # noqa: E402
from yonixalpha_core.db.models import ModelVersion, OpportunityOutcome, PaperPosition  # noqa: E402
from yonixalpha_core.tools import history_audit as ha, ml_audit as ma  # noqa: E402

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
async def sf():
    engine = create_async_engine(os.environ["DATABASE_URL"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield make_session_factory(engine)
    await engine.dispose()


def pos(pnl, at, *, status="closed", mint=None, engine="solana_fresh", strategy="fresh_launch_flow"):
    return PaperPosition(id=uuid.uuid4(), symbol="T", provider="paper", side="LONG", engine=engine, asset_id=mint or uuid.uuid4().hex,
                         entry_price=Decimal(1), quantity=Decimal(1), take_profit=[], status=status, execution_mode="PAPER",
                         realized_pnl=None if pnl is None else Decimal(str(pnl)),
                         realized_pnl_pct=None if pnl is None else Decimal(str(pnl)) / Decimal("0.1"),  # a fraction, as stored
                         fees_paid_quote=Decimal("0.001"), entry_cost_quote=Decimal("0.1"), proceeds_quote=Decimal(0),
                         entry_at=at - timedelta(minutes=2), exit_at=at if status == "closed" else None,
                         exit_reason="stop_loss" if pnl is not None and float(pnl) < 0 else "take_profit_3", strategy=strategy, plan={})


@pytest.mark.asyncio
async def test_the_whole_history_is_counted_and_losses_attributed(sf):
    async with sf() as s:
        s.add_all([pos("0.05", T0, mint="WIN"), pos("-0.2", T0 + timedelta(days=1), mint="BAD"),
                   pos("-0.01", T0 + timedelta(days=1, hours=3)), pos("-0.01", T0 + timedelta(days=2)),
                   pos(None, T0, status="open"), pos(None, T0, status="failed")])
        s.add(OpportunityOutcome(key="g1", mint="M1", engine="solana_fresh", stage="GATE", decision="REJECT", traded=False,
                                 decided_at=T0, snapshot={}, executable_return_pct=Decimal("12.5"), status="COMPLETE"))
        s.add(OpportunityOutcome(key="g2", mint="M2", engine="solana_fresh", stage="GATE", decision="EXECUTE", traded=True,
                                 decided_at=T0, snapshot={}, status="TRACKING"))
        s.add_all([OpportunityOutcome(key=f"c{i}", mint=f"C{i}", engine="solana_fresh", stage="POOL", decision="NO_TRADE",
                                      traded=False, decided_at=T0, snapshot={}, executable_return_pct=Decimal("-3.00"),
                                      status="COMPLETE") for i in range(20)])
        await s.commit()
        r = await ha.build(s, ha.SECTIONS, ("solana_fresh",), None, None)
    st = {x["status"]: x["count"] for x in r["status"]}
    assert st == {"closed": 4, "open": 1, "failed": 1}
    o = r["metrics"]["solana_fresh"]["PAPER"]["overall"]
    assert o["trades"] == 4 and o["net_pnl_sol"] == -0.17 and o["largest_loss_sol"] == -0.2
    assert {x["key"] for x in r["daily"]} == {"2026-10-01", "2026-10-02", "2026-10-03"}
    assert {x["key"] for x in r["hourly"]} == {"09", "12"}
    c = r["concentration"]["solana_fresh PAPER"]
    assert c["losses"] == 3 and c["losses_making_80pct_of_gross_loss"] == 1 and c["worst_tokens"][0]["mint"] == "BAD"
    assert c["net_without_best5_tokens_sol"] == -0.22
    # -0.2 and 2 x -0.01 SOL on 0.1 SOL: -200 %, -10 %, -10 % (stored as fractions)
    assert c["loss_size_bands_pct"] == {"0..-10": 0, "-10..-20": 2, "-20..-40": 0, "below -40": 1}
    assert c["median_loss_pct"] == -10.0
    led = {(x["decision"], x["traded"]): x for x in r["ledger"]}
    assert led[("REJECT", False)]["mean_exec_pct"] == 12.5 and led[("EXECUTE", True)]["unresolved"] == 1
    assert led[("REJECT", False)]["constant"] is False  # one measured row is too few to call constant
    assert led[("NO_TRADE", False)]["constant"] is True  # 20 identical values: costs only, flagged
    assert any(t["table"] == "paper_positions" and t["earliest"] for t in r["range"])
    text = ha.render(r)
    assert "costs only, no price move was measured" in text
    assert "CONCENTRATION" in text and "Nothing was written" in text and "data_retention setting" in text


def test_auc_and_rank_correlation_are_exact_on_small_cases():
    assert ma.auc([0, 0, 1, 1], [0.1, 0.2, 0.8, 0.9]) == 1.0 and ma.auc([0, 1], [0.5, 0.5]) == 0.5
    assert ma.auc([1, 1], [0.1, 0.2]) is None
    assert ma.spearman([1, 2, 3, 4], [10, 20, 30, 40]) == 1.0 and ma.spearman([1, 2, 3], [3, 2, 1]) == -1.0


@pytest.mark.asyncio
async def test_only_prospective_predictions_are_judged_and_usage_shows_no_champion(sf):
    trained = T0 - timedelta(days=1)
    async with sf() as s:
        s.add(ModelVersion(name="shadow_e_executable_return", version=3, status="shadow", feature_names=[],
                           training_sample_count=500, metrics={"holdout": {"n": 100, "mae": 9.0}}, artifact=b"x",
                           trained_at=trained))
        s.add(ModelVersion(name="gate_solana_fresh", version=9, status="challenger", feature_names=[],
                           training_sample_count=300, metrics={"challenger": {"auc": 0.55}, "promotable": False}, artifact=b"x"))
        for i in range(120):
            at = T0 + timedelta(minutes=10 * i)
            pred = float(i % 10) - 4.0
            late = i % 4 == 0  # every fourth score written after T+5m: retrospective, never judged
            s.add(OpportunityOutcome(
                key=f"o{i}", mint=f"M{i % 30}", engine="solana_fresh", stage="GATE", decision="EXECUTE" if i % 3 == 0 else "REJECT",
                traded=i % 3 == 0, decided_at=at, snapshot={}, status="COMPLETE", executable_return_pct=Decimal(str(pred * 2)),
                labels={"available_at": (at + timedelta(hours=1)).isoformat(), "upside_50": pred > 2},
                ml_shadow={"scored_at": (at + timedelta(minutes=10 if late else 1)).isoformat(),
                           "scores": {"E_EXECUTABLE_RETURN": {"value": pred, "model": "shadow_e_executable_return", "version": 3},
                                      "P_UPSIDE_50": {"value": 0.9 if pred > 2 else 0.1, "model": "shadow_e_executable_return",
                                                      "version": 3}}}))
        await s.commit()
        r = await ma.build(s, ma.SECTIONS, ("solana_fresh",), None, None)
    reg = {m["name"]: m for m in r["registry"]}
    assert reg["gate_solana_fresh"]["ever_activated"] == 0 and reg["gate_solana_fresh"]["promotable"] is False
    assert r["usage"]["ledger_rows_scored_by_a_champion_at_the_decision"] == {}
    assert r["leakage"]["scores"] == {"PROSPECTIVE": 180, "RETROSPECTIVE": 60} and r["leakage"]["scored_after_the_t5m_horizon"] == 60
    ret = r["predictions"]["ALL E_EXECUTABLE_RETURN"]
    assert ret["n"] == 90 and ret["rank_correlation"] == 1.0
    assert ret["verdict"].startswith("UNKNOWN")  # 90 < MIN_N prospective predictions with outcomes
    up = r["predictions"]["ALL P_UPSIDE_50"]
    assert up["n"] == 90 and up["roc_auc"] == 1.0 and up["brier"] < up["brier_of_base_rate"]
    text = ma.render(r)
    assert "ever ACTIVE 0" in text and "PROSPECTIVE PREDICTIONS" in text and "LEAKAGE" in text
