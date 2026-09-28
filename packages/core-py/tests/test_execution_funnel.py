"""The execution funnel counts, from real assessments the gate produced and
persisted, where each token stopped: no BUY signal, BUY signal blocked (by
which code), needs-approval driven by which HIGH finding, executable,
position, live order."""

import os
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://yonixalpha:yonixalpha_test_pw@localhost:5432/yonixalpha_test")

import pytest_asyncio  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from yonixalpha_core import execution_funnel  # noqa: E402
from yonixalpha_core.db import models  # noqa: F401,E402
from yonixalpha_core.db.base import Base, make_session_factory  # noqa: E402
from yonixalpha_core.db.models import ExecutionOrder, PaperPosition, Token, TradeTimelineEvent, TradingCandidate  # noqa: E402
from yonixalpha_core.safety import store  # noqa: E402
from yonixalpha_core.safety.gate import assess  # noqa: E402
from yonixalpha_core.safety.models import StrategyMode, StrategySignal  # noqa: E402
from yonixalpha_core.safety.settings import default_settings_for  # noqa: E402

from tests.test_safety_gate import NOW, healthy  # noqa: E402

FRESH = default_settings_for("solana_fresh")


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(os.environ["DATABASE_URL"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with make_session_factory(engine)() as session:
        yield session
    await engine.dispose()


def _token(mint: str, **market) -> object:
    base = healthy(engine="solana_fresh", asset_id=mint, now=NOW)
    return replace(base, market=replace(base.market, **market)) if market else base


async def _persist(db, inp, key: str):
    token = Token(mint_address=inp.asset_id, first_seen_source="pump_stream")
    db.add(token)
    await db.flush()
    cand = TradingCandidate(token_id=token.id, engine="discovery", state="analyzing", state_history=[],
                            detail={"source": "pump_stream", "mint": inp.asset_id})
    db.add(cand)
    await db.flush()
    a = assess(inp, FRESH)
    row, _ = await store.persist_assessment(db, a, cand.id, key)
    return a, row, cand


async def test_funnel_counts_every_stage_and_names_the_blockers(db):
    mint_vol = "V" * 44
    mint_approval = "A" * 44
    mint_nosignal = "N" * 44
    mint_exec = "E" * 44
    # BUY signal, but the volatility-based stop is wider than max_stop_pct.
    a1, *_ = await _persist(db, _token(mint_vol, volatility=Decimal("0.40")), "k1")
    assert a1.decision.value == "NO_TRADE"
    # BUY signal, HIGH volatility finding -> needs approval -> AUTO: NO_TRADE.
    a2, *_ = await _persist(db, replace(_token(mint_approval, volatility=Decimal("0.12")),
                                        strategy_mode=StrategyMode.AUTO), "k2")
    assert "AUTO_NO_APPROVAL" in {f.code for f in a2.findings}
    # No BUY signal at all.
    await _persist(db, replace(_token(mint_nosignal), signal=StrategySignal("fresh", "1", False, 0.1, ["no acceleration"])), "k3")
    # Executable, and a paper position was opened.
    a4, row4, cand4 = await _persist(db, _token(mint_exec), "k4")
    assert a4.executable
    pos = PaperPosition(candidate_id=cand4.id, symbol="E", provider="paper", side="LONG", entry_price=Decimal(1),
                        quantity=Decimal(1), entry_at=NOW, status="open", engine="solana_fresh", asset_id=mint_exec,
                        assessment_id=row4.id, execution_mode="PAPER")
    db.add(pos)
    await db.flush()
    db.add(TradeTimelineEvent(event_type="exit_intelligence.exit", occurred_at=NOW, position_id=pos.id,
                              detail={"reasons": ["sell pressure: 3 sellers in 30s"]}))
    # A failed live buy, recorded as it would be by the order worker.
    db.add(ExecutionOrder(mode="LIVE", side="BUY", reason="entry", mint=mint_exec, provider="pumpportal_local", route="pump",
                          amount="0.01", amount_kind="sol", slippage_pct=Decimal(10), priority_fee_sol=Decimal("0.0001"),
                          status="FAILED", idempotency_key="entry:x", error="pumpportal: 400 bad request"))
    db.add(TradeTimelineEvent(event_type="live_entry_refused", occurred_at=NOW, detail={"reason": "size exceeds wallet"}))
    await db.commit()

    f = await execution_funnel.funnel(db, NOW - timedelta(hours=1))
    s = f["stages"]["solana_fresh"]
    assert s["tokens"] == 4 and s["signal_tokens"] == 3 and s["executable_tokens"] == 1
    blocked = {b["code"]: b for b in f["blocked_with_buy_signal"]}
    assert "VOLATILITY_EXCEEDS_MAX_STOP" in blocked and "AUTO_NO_APPROVAL" in blocked
    assert "SIGNAL_NOT_QUALIFIED" not in blocked  # only tokens that HAD a BUY signal
    assert {d["code"] for d in f["approval_drivers"]} >= {"HIGH_VOLATILITY"}
    assert f["positions"]["solana_fresh"]["PAPER"]["open"] == 1
    assert f["orders"]["BUY"]["FAILED"] == 1 and "400" in f["order_errors"][0]["error"]
    assert f["execution_failures"][0]["event_type"] == "live_entry_refused"
    assert f["modes"]["global"] == "PAPER"
    assert any("Global mode is PAPER" in n for n in f["diagnosis"])
    assert any("HIGH_VOLATILITY" in n for n in f["diagnosis"])

    ex = await execution_funnel.code_examples(db, "VOLATILITY_EXCEEDS_MAX_STOP", NOW - timedelta(hours=1))
    assert ex and ex[0]["asset_id"] == mint_vol and "exceeds max_stop_pct" in ex[0]["message"]
    assert "data_errors" in f

    t = await execution_funnel.token_trace(db, mint_vol)
    assert t["assessments"][0]["buy_signal"] is True
    assert t["assessments"][0]["blocking"][0]["code"] == "VOLATILITY_EXCEEDS_MAX_STOP"
    t = await execution_funnel.token_trace(db, mint_exec)
    assert t["positions"][0]["status"] == "open" and t["orders"][0]["status"] == "FAILED"
    # An executable evaluation lists no "blocking" codes (its findings are
    # informational), and the position's exit verdict is in the trace.
    assert t["assessments"][0]["executable"] and t["assessments"][0]["blocking"] is None
    assert t["position_events"][0]["event_type"] == "exit_intelligence.exit"
    assert "sell pressure" in t["position_events"][0]["detail"]["reasons"][0]


async def test_funnel_diagnoses_signal_as_the_blocker(db):
    await _persist(db, replace(_token("S" * 44), signal=StrategySignal("fresh", "1", False, 0.1, ["no trend"])), "s1")
    await db.commit()
    f = await execution_funnel.funnel(db, NOW - timedelta(hours=1))
    assert any("none produced a BUY signal" in n for n in f["diagnosis"])


async def test_live_sizing_respects_the_sol_reserve(db):
    """Fix: a live plan is sized against wallet - min_sol_reserve, so the gate
    never plans a buy that enter_live must refuse."""
    from yonixalpha_core import live_trading
    from yonixalpha_core.safety import pipeline

    acct = await live_trading.get_live_account(db)
    acct.cash_balance = Decimal("0.0829")
    await db.commit()
    controls, _, _ = await pipeline.load_controls(db, _NoRedis(), None, "solana_fresh", StrategyMode.AUTO, "M" * 44, NOW,
                                                  False, live=True)
    reserve = (await live_trading.load_live_settings(db)).min_sol_reserve
    assert controls.account.available_balance == Decimal("0.0829") - reserve
    assert controls.account.equity == Decimal("0.0829")  # risk is still measured on the whole wallet


class _NoRedis:
    async def get(self, key):
        return None


async def test_pipeline_ladder_and_final_blockers(db):
    """Each token's furthest stage and exact final blocker, from its own
    records: never "PROMOTE = bought"."""
    # PROMOTED but never assessed (e.g. rejected before the gate ran).
    token = Token(mint_address="P" * 44, first_seen_source="pump_stream")
    db.add(token)
    await db.flush()
    db.add(TradingCandidate(token_id=token.id, engine="discovery", state="rejected",
                            state_history=[{"state": "rejected", "at": NOW.isoformat(), "reason": "strategy solana_fresh is OFF"}],
                            detail={"source": "pump_stream", "mint": "P" * 44}, created_at=NOW))
    # BUY signal, but exit intelligence would sell at once.
    await _persist(db, replace(_token("X" * 44), entry_exit_check={"action": "REDUCE", "reasons": ["volume collapsed"],
                                                                   "metrics": {}}), "p2")
    # Risk-approved, stopped by sizing (no room left under the exposure cap).
    base = _token("S" * 44)
    await _persist(db, replace(base, account=replace(base.account, current_exposure=Decimal("3"))), "p3")
    # Executable and bought in paper.
    a4, row4, cand4 = await _persist(db, _token("E" * 44), "p4")
    db.add(PaperPosition(candidate_id=cand4.id, symbol="E", provider="paper", side="LONG", entry_price=Decimal(1),
                         quantity=Decimal(1), entry_at=NOW, status="open", engine="solana_fresh", asset_id="E" * 44,
                         assessment_id=row4.id, execution_mode="PAPER", execution_route="pump"))
    # Executable LIVE, the buy never landed.
    a5, row5, cand5 = await _persist(db, _token("L" * 44), "p5")
    live = PaperPosition(candidate_id=cand5.id, symbol="L", provider="live", side="LONG", entry_price=Decimal(1),
                         quantity=Decimal(0), entry_at=NOW, status="failed", engine="solana_fresh", asset_id="L" * 44,
                         assessment_id=row5.id, execution_mode="LIVE", execution_route="pump", execution_provider="pumpportal_local")
    db.add(live)
    await db.flush()
    db.add(ExecutionOrder(position_id=live.id, mode="LIVE", side="BUY", reason="entry", mint="L" * 44, provider="pumpportal_local",
                          route="pump", amount="0.01", amount_kind="sol", slippage_pct=Decimal(10), priority_fee_sol=Decimal("0.0001"),
                          status="FAILED", idempotency_key="entry:p5", signature="sig5", error="simulation failed: slippage",
                          submitted_at=None))
    await db.commit()

    pl = (await execution_funnel.funnel(db, NOW - timedelta(hours=1)))["pipeline"]
    by = {t["mint"][0]: t for t in pl["tokens"]}
    assert by["P"]["stage"] == "PROMOTE" and by["P"]["final_blocker"]["code"] == "CANDIDATE_REJECTED"
    assert by["P"]["final_blocker"]["reason"] == "strategy solana_fresh is OFF"
    assert pl["promoted_not_assessed"][0]["tokens"] == 1 and "OFF" in pl["promoted_not_assessed"][0]["reason"]
    assert by["X"]["stage"] == "BUY_SIGNAL" and by["X"]["final_blocker"]["code"] == "EXIT_SIGNAL_AT_ENTRY"
    assert "exit_signal_at_entry" in by["X"]["final_blocker"]["groups"]
    assert by["S"]["stage"] == "RISK_APPROVED" and by["S"]["final_blocker"]["code"] == "SIZE_BELOW_MINIMUM"
    assert by["S"]["final_blocker"]["groups"] == ["sizing_account"]
    assert by["E"]["stage"] == "POSITION_OPEN" and by["E"]["final_blocker"] is None and by["E"]["execution_route"] == "pump"
    # Signed (the signature is stored before simulation), then simulation failed: it stops at SIGNED.
    assert by["L"]["stage"] == "TRANSACTION_SIGNED" and by["L"]["final_blocker"]["code"] == "BUY_SIMULATION_FAILED"
    s = pl["stages"]
    assert s["PROMOTE"] == 5 and s["BUY_SIGNAL"] == 4 and s["RISK_APPROVED"] == 3 and s["EXECUTION_APPROVED"] == 2
    assert s["TRANSACTION_BUILT"] == 2 and s["TRANSACTION_SIGNED"] == 2
    assert s["BUY_SUBMITTED"] == 1 and s["POSITION_OPEN"] == 1
    assert pl["blocked_by"]["exit_signal_at_entry"] == 1 and pl["blocked_by"]["sizing_account"] == 1

    t = await execution_funnel.token_trace(db, "X" * 44)
    assert t["pipeline"]["stage"] == "BUY_SIGNAL" and t["pipeline"]["final_blocker"]["code"] == "EXIT_SIGNAL_AT_ENTRY"
