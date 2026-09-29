"""Every opportunity is recorded with the state it was decided on and what
the market did afterwards; traded ones get their result, losing ones a
LOSS_ANALYSIS; winners, losers and rejected-then-up tokens are compared."""

import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://yonixalpha:yonixalpha_test_pw@localhost:5432/yonixalpha_test")

import pytest_asyncio  # noqa: E402
from redis.asyncio import from_url  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from yonixalpha_core import opportunities as opp  # noqa: E402
from yonixalpha_core.db import models  # noqa: F401,E402
from yonixalpha_core.db.base import Base, make_session_factory  # noqa: E402
from yonixalpha_core.db.models import ExecutionOrder, OpportunityOutcome, PaperPosition  # noqa: E402
from yonixalpha_core.safety.gate import assess  # noqa: E402
from yonixalpha_core.safety.settings import SafetySettings  # noqa: E402
from yonixalpha_core.solana import pump_stream  # noqa: E402

from tests.test_safety_gate import healthy  # noqa: E402

T0 = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
VT = 10**15


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(os.environ["DATABASE_URL"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with make_session_factory(engine)() as session:
        yield session
    await engine.dispose()


@pytest_asyncio.fixture
async def redis():
    r = from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/9"), decode_responses=True)
    await r.flushdb()
    yield r
    await r.flushdb()
    await r.aclose()


async def put_trades(redis, mint: str, points: list[tuple[int, int]]) -> None:
    """(seconds after T0, virtual SOL lamports) → trades priced vsol/VT."""
    for sec, vsol in points:
        await redis.rpush(pump_stream.trades_key(mint), json.dumps([int((T0 + timedelta(seconds=sec)).timestamp()), f"w{sec}", 1,
                                                                    10**8, 10**9, vsol, VT]))


async def test_rejected_token_horizons_peak_drawdown_and_migration(db, redis):
    mint = "RejectedMint1111111111111111111111111111111"
    base = 30 * 10**9
    await put_trades(redis, mint, [(-5, base), (3, base), (8, int(base * 1.1)), (25, int(base * 1.5)), (50, int(base * 0.9)),
                                   (200, int(base * 2)), (800, int(base * 1.2)), (1700, int(base * 0.8))])
    await redis.zadd(pump_stream.MIGRATED, {mint: (T0 + timedelta(seconds=1000)).timestamp()})
    await redis.hset(pump_stream.curve_key(mint), mapping={"vsol": base, "vtok": VT, "updated_at": int(T0.timestamp()), "fee_bps": 125})
    snap = {"price_raw": str(Decimal(base) / Decimal(VT)), "market_cap_sol": "30"}
    await opp.record(db, key="obs:x", mint=mint, symbol="REJ", engine="solana_fresh", stage="OBSERVATION", decision="REJECT",
                     traded=False, reasons=["price 52% below its observed peak"], decided_at=T0, snapshot=snap)
    await db.commit()
    await opp.track(db, redis, T0 + timedelta(seconds=70))
    row = (await db.execute(select(OpportunityOutcome))).scalar_one()
    hz = row.horizons
    assert hz["T+5s"]["change_pct"] == "0.00" and hz["T+10s"]["change_pct"] == "10.00"
    assert hz["T+20s"]["change_pct"] == "10.00"  # the 25 s trade is not visible yet at T+20s
    assert hz["T+30s"]["change_pct"] == "50.00" and hz["T+60s"]["change_pct"] == "-10.00"
    assert "T+5m" not in hz and row.status == "TRACKING"
    # Nothing is due until T+5m: the row is not re-read.
    assert await opp.track(db, redis, T0 + timedelta(seconds=90)) == 0
    await opp.track(db, redis, T0 + timedelta(seconds=1900))
    await db.refresh(row)
    assert row.horizons["T+5m"]["change_pct"] == "100.00" and row.horizons["T+30m"]["change_pct"] == "-20.00"
    assert row.peak_pct == Decimal("100.00") and row.drawdown_pct == Decimal("-20.00")
    # Tracking runs to T+60m now; peak / drawdown keep their 30-minute meaning.
    assert row.migrated_at == T0 + timedelta(seconds=1000) and row.status == "TRACKING" and "T+60m" not in row.horizons
    await opp.track(db, redis, T0 + timedelta(seconds=3700))
    await db.refresh(row)
    assert row.status == "COMPLETE" and row.peak_pct == Decimal("100.00") and row.horizons["T+60m"]["change_pct"] == "-20.00"
    assert row.path["T+30s"]["buys"] == 1 and row.path["T+5m"]["peak_so_far_pct"] == 100.0
    assert row.regime["data_regime"] == "post_boost" and row.regime["strategy"] == "solana_fresh"
    # These synthetic trades do not follow a constant product: executable returns are UNKNOWN, never guessed.
    assert "curve math" in row.path["T+5m"]["executable"]["unknown"] and row.executable_return_pct is None
    cf = row.analysis["counterfactual"]
    assert cf["rule_exit"] == "TP" and cf["classification"] == "UNKNOWN" and cf["rejecting_rule"].startswith("price 52%")
    assert row.labels["upside_100"] is True and row.labels["upside_200"] is False and row.labels["migrate_60m"] is True


async def test_horizons_without_stream_data_say_why(db, redis):
    await opp.record(db, key="gate:y", mint="PoolOnly11111111111111111111111111111111111", symbol=None, engine="solana_migration",
                     stage="GATE", decision="REJECT", traded=False, reasons=[], decided_at=T0, snapshot={})
    await db.commit()
    await opp.track(db, redis, T0 + timedelta(seconds=40))
    row = (await db.execute(select(OpportunityOutcome))).scalar_one()
    assert "no stream trades" in row.horizons["T+30s"]["unavailable"] and row.peak_pct is None


def test_gate_snapshot_records_what_the_decision_saw():
    inp = healthy(entry_quality={"indicators": ["buyer_stall"], "strong": False})
    a = assess(inp, SafetySettings())
    s = opp.gate_snapshot(inp, {"errors": ["holders rpc: timeout"]}, a, {"price_age_seconds": 2.5, "decision_eval_ms": 900})
    assert s["price_sol"] and s["market_cap_sol"] and s["liquidity_sol"] == "30" and s["buyers"] == 25 and s["sellers"] == 8
    assert s["buy_sell_ratio"] == "3.12" and s["deterioration_indicators"] == ["buyer_stall"]
    assert s["data_errors"] == ["holders rpc: timeout"] and s["signal_qualified"] is True


async def _closed_trade(db, *, pnl: str, high: str, low: str, snapshot: dict, entry_diag: dict | None = None,
                        exit_reason: str = "stop_loss") -> OpportunityOutcome:
    pos = PaperPosition(symbol="TR", provider="live", side="LONG", entry_price=Decimal("1"), quantity=Decimal(1), entry_at=T0,
                        status="closed", exit_at=T0 + timedelta(minutes=3), exit_reason=exit_reason, exit_price=Decimal("0.8"),
                        realized_pnl=Decimal(pnl), realized_pnl_pct=Decimal(pnl) / 10, highest_price=Decimal(high),
                        lowest_price=Decimal(low), max_loss_quote=Decimal("0.05"), execution_mode="LIVE",
                        plan={"entry_price": "1", "fill": {"market_price": "1", "market_price_basis": "trade price"}})
    db.add(pos)
    await db.flush()
    if entry_diag is not None:
        db.add(ExecutionOrder(position_id=pos.id, mode="LIVE", side="BUY", reason="entry", mint="M", provider="p", route="pump",
                              amount="0.1", amount_kind="sol", slippage_pct=Decimal(10), priority_fee_sol=Decimal("0.0001"),
                              status="CONFIRMED", idempotency_key=f"k-{pos.id}", diagnostics=entry_diag))
    await opp.record(db, key=f"gate:{pos.id}", mint="M", symbol="TR", engine="solana_fresh", stage="GATE", decision="EXECUTE",
                     traded=True, reasons=[], decided_at=T0, snapshot={"decimals": 6, "supply_raw": str(VT), **snapshot},
                     position_id=pos.id)
    await db.flush()
    await opp.on_position_closed(db, pos)
    await db.commit()
    return (await db.execute(select(OpportunityOutcome).where(OpportunityOutcome.position_id == pos.id))).scalar_one()


async def test_losing_trade_analysis_classifies_the_conditions(db):
    bad = await _closed_trade(db, pnl="-0.02", high="1.005", low="0.75",
                              snapshot={"deterioration_indicators": ["seller_acceleration"], "signal_qualified": True})
    la = bad.loss_analysis
    assert la["type"] == "LOSS_ANALYSIS" and la["classification"] == "BAD_ENTRY" and la["max_adverse_excursion_pct"] == "-25.00"
    assert bad.trade_result["mfe_pct"] == "0.50" and bad.trade_result["market_cap_entry_sol"] == "1000000000.00"

    rev = await _closed_trade(db, pnl="-0.02", high="1.3", low="0.8", snapshot={"signal_qualified": True})
    assert rev.loss_analysis["classification"] == "MARKET_REVERSAL"

    sig = await _closed_trade(db, pnl="-0.02", high="1.0", low="0.8", snapshot={"signal_qualified": True, "signal_strength": 1.0})
    assert sig.loss_analysis["classification"] == "SIGNAL_FAILURE"

    data = await _closed_trade(db, pnl="-0.02", high="1.0", low="0.8", snapshot={"volatility_confidence": "LOW_CONFIDENCE"})
    assert data.loss_analysis["classification"] == "DATA_FAILURE"

    exe = await _closed_trade(db, pnl="-0.02", high="1.0", low="0.8", snapshot={},
                              entry_diag={"timing": {"decision_to_confirm_ms": 4000},
                                          "price": {"classification": "CURVE_MOVEMENT",
                                                    "components_pct": {"total_vs_decision_pct": "18.5"}}})
    assert exe.loss_analysis["classification"] == "EXECUTION_QUALITY" and exe.loss_analysis["price_vs_decision_pct"] == "18.5"

    risk = await _closed_trade(db, pnl="-0.09", high="1.0", low="0.5", snapshot={})
    assert risk.loss_analysis["classification"] == "RISK_MODEL_FAILURE"

    win = await _closed_trade(db, pnl="0.03", high="1.4", low="0.98", snapshot={}, exit_reason="take_profit_1")
    assert win.loss_analysis is None and win.trade_result["pnl_sol"] == "0.03"


async def test_comparison_separates_winners_losers_and_rejected_that_went_up(db):
    await _closed_trade(db, pnl="0.03", high="1.4", low="0.98", snapshot={"market_cap_sol": "40", "buy_sell_ratio": "3"},
                        exit_reason="take_profit_1")
    await _closed_trade(db, pnl="-0.02", high="1.0", low="0.8", snapshot={"market_cap_sol": "80", "buy_sell_ratio": "1.2",
                                                                         "signal_qualified": True})
    for i, peak in enumerate(("45", "5")):
        await opp.record(db, key=f"obs:{i}", mint=f"R{i}", symbol=None, engine="solana_fresh", stage="OBSERVATION",
                         decision="REJECT", traded=False, reasons=[], decided_at=T0, snapshot={"market_cap_sol": "20"})
    await db.flush()
    for r in (await db.execute(select(OpportunityOutcome).where(OpportunityOutcome.traded.is_(False)))).scalars():
        r.peak_pct = Decimal("45") if r.key == "obs:0" else Decimal("5")
    await db.commit()
    c = await opp.comparison(db, T0 - timedelta(days=1))
    assert c["winning_trades"]["n"] == 1 and c["losing_trades"]["n"] == 1 and c["rejected"]["n"] == 2
    assert c["winning_trades"]["market_cap_sol"] == "40.0000" and c["losing_trades"]["market_cap_sol"] == "80.0000"
    assert c["rejected_later_up"]["n"] == 1 and c["loss_classes"] == {"SIGNAL_FAILURE": 1}


async def test_loss_report_explains_old_losing_trades(db, monkeypatch, capsys):
    from yonixalpha_core.config import get_settings
    from yonixalpha_core.db.models import RiskAssessment
    from yonixalpha_core.tools import loss_report

    a = RiskAssessment(idempotency_key="lr1", engine="solana_fresh", strategy="s", asset_id="M1", symbol="L1", decision="EXECUTE",
                       status_label="x", executable=True, execution_target="LIVE", overall_risk="MODERATE", risk_engine_version="1",
                       evaluated_at=T0, assessment={"qualified": True, "findings": [
                           {"code": "ACTIVITY_DETERIORATING", "level": "MODERATE", "action": "EXECUTE", "message": "sellers rising"}],
                           "inputs_snapshot": {"features": {"unique_buyers": "4"}, "volatility_source": "10-second returns",
                                               "entry_exit_check": {"action": "HOLD", "reasons": []}}})
    b = RiskAssessment(idempotency_key="lr2", engine="solana_fresh", strategy="s", asset_id="M2", symbol="L2", decision="NO_TRADE",
                       status_label="x", executable=False, execution_target="LIVE", overall_risk="HIGH", risk_engine_version="1",
                       evaluated_at=datetime.now(timezone.utc), assessment={"findings": [{"code": "AUTO_SL_NO_VOLATILITY"}],
                                                                           "inputs_snapshot": {"volatility_source": "unavailable: 2 trades"}})
    db.add_all([a, b])
    await db.flush()
    now = datetime.now(timezone.utc)
    db.add(PaperPosition(symbol="L1", provider="live", side="LONG", entry_price=Decimal("1"), quantity=Decimal(1), entry_at=now - timedelta(minutes=5),
                         status="closed", exit_at=now, exit_reason="stop_loss", realized_pnl=Decimal("-0.01"), realized_pnl_pct=Decimal("-0.2"),
                         highest_price=Decimal("1.001"), lowest_price=Decimal("0.8"), execution_mode="LIVE", assessment_id=a.id,
                         engine="solana_fresh", asset_id="M1", plan={"entry_price": "1"}))
    await db.commit()
    monkeypatch.setenv("JWT_SECRET", "x" * 40)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", "$2b$12$" + "x" * 53)
    get_settings.cache_clear()
    try:
        assert await loss_report.main(["--json"]) == 0
    finally:
        get_settings.cache_clear()
    out = json.loads(capsys.readouterr().out)
    loss = out["losses"][0]
    assert loss["symbol"] == "L1" and loss["mfe_pct"] == "0.10" and loss["loss_analysis"]["classification"] == "SIGNAL_FAILURE"
    assert loss["warnings_at_entry"][0].startswith("ACTIVITY_DETERIORATING")
    assert out["volatility"]["blocked_no_volatility"] == 1 and "unavailable: 2 trades" in out["volatility"]["by_reason"]


def test_excursions_use_the_market_entry_price_not_the_cost_basis():
    # Filled after the fix: marks start at the trade price (0.6); the cost
    # basis (1.0) includes fees and new-account rent.
    new = PaperPosition(execution_mode="LIVE", entry_price=Decimal("1.0"), highest_price=Decimal("0.66"),
                        lowest_price=Decimal("0.54"), plan={"entry_price": "0.6", "fill": {"market_price": "0.6"}})
    e = opp.excursions(new)
    assert e["mfe_pct"] == "10.00" and e["mae_pct"] == "-10.00"

    # Filled before the fix: high and low were seeded from the cost basis.
    # A high equal to the seed is not a market price and a low equal to it is
    # unknown, so neither is reported as an excursion.
    old = PaperPosition(execution_mode="LIVE", entry_price=Decimal("1.0"), highest_price=Decimal("1.0"),
                        lowest_price=Decimal("1.0"), plan={"entry_price": "0.6"})
    e = opp.excursions(old)
    assert e["mfe_pct"] is None and e["mae_pct"] is None and "planned entry price" in e["mfe_mae_basis"]
    from yonixalpha_core.live_trading import market_reference
    assert market_reference(old)["high"] == Decimal("0.6")  # exit intelligence's reference: a lower bound
    old.lowest_price = Decimal("0.5")  # a real mark below the seed
    assert opp.excursions(old)["mae_pct"] == "-16.67"

    # Paper positions are unchanged: their entry price is a market price.
    paper = PaperPosition(execution_mode="PAPER", entry_price=Decimal("1.0"), highest_price=Decimal("1.2"), lowest_price=Decimal("0.9"))
    assert opp.excursions(paper)["mfe_pct"] == "20.00"


def test_legacy_cost_seeded_high_is_not_a_signal_failure():
    """Before the fix every LIVE loss read "never went up (MFE 0.00%)"
    because the high started at the cost basis."""
    snapshot = {"signal_qualified": True}
    old = PaperPosition(execution_mode="LIVE", entry_price=Decimal("1.0"), highest_price=Decimal("1.0"),
                        lowest_price=Decimal("0.55"), plan={"entry_price": "0.6"})
    result = {"pnl_sol": "-0.003", "pnl_pct": "-0.44", "exit_reason": "exit_intel_exit_now", **opp.excursions(old)}
    la = opp.classify_loss(snapshot, result, {"price": {"components_pct": {"fees_pct": "70.79"}}}, Decimal("0.0008"))
    assert "SIGNAL_FAILURE" not in la["flags"] and la["classification"] == "RISK_MODEL_FAILURE"
    assert any("entry costs were +70.79%" in e for e in la["evidence"])


async def test_ledger_review_counts_categories_buckets_and_latency(db):
    def row(key, **kw):
        base = dict(key=key, mint=key, engine="solana_fresh", stage="GATE", decision="REJECT", traded=False, reasons=["LOW_LIQUIDITY: x"],
                    decided_at=T0, snapshot={"market_cap_sol": "45", "age_seconds": 40, "decision_eval_ms": 800},
                    horizons={}, status="COMPLETE")
        base.update(kw)
        return OpportunityOutcome(**base)

    db.add_all([
        row("a", analysis={"counterfactual": {"classification": "MISSED_WIN"}}, labels={"recovery": True},
            theoretical_return_pct=Decimal("40"), executable_return_pct=Decimal("31")),
        row("b", analysis={"counterfactual": {"classification": "CORRECT_REJECTION"}}, labels={"recovery": False}),
        row("c", analysis={"counterfactual": {"classification": "UNEXECUTABLE"}}),
        row("d", traded=True, decision="EXECUTE", trade_result={"pnl_sol": "-0.01", "entry_execution": {"decision_to_confirm_ms": 1500}},
            post_exit={"classification": "POSSIBLY_EARLY"}),
        row("e", traded=True, decision="EXECUTE", trade_result={"pnl_sol": "0.02", "entry_execution": {"decision_to_confirm_ms": 900}},
            post_exit={"classification": "GOOD_EXIT"}),
        row("old", decided_at=T0 - timedelta(days=30), analysis={"counterfactual": {"classification": "MISSED_WIN"}}),
    ])
    await db.commit()
    r = await opp.review(db, T0 - timedelta(days=1))
    c = r["counts"]
    assert c["observed"] == 5 and c["traded"] == 2 and c["rejected"] == 3 and c["missed_win"] == 1
    assert c["correct_rejection"] == 1 and c["unexecutable"] == 1 and c["false_positive"] == 1 and c["true_positive"] == 1
    assert c["premature_exit"] == 1 and c["good_exit"] == 1 and c["recovery"] == 1
    assert r["missed_win_buckets"]["rejecting_rule"] == {"LOW_LIQUIDITY": 1}
    assert r["missed_win_buckets"]["market_cap_at_decision"] == {"30-60 SOL": 1}
    assert r["signal_vs_execution"]["rows"] == 1 and r["signal_vs_execution"]["executable_return_avg_pct"].startswith("31")
    assert r["snipe_latency"]["decision_to_confirm_ms"]["n"] == 2 and r["snipe_latency"]["creation_to_decision_s"]["median"] == 40.0
    assert opp.category_filter("nonsense") is None
