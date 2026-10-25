"""Performance metrics, positions, the forward test and the prediction audit
(seven-day investigation, 2026-10-24): closed trades only, PAPER and LIVE
apart, each cost counted once, window edges exact, rules frozen at the start,
configuration changes flagged, verdicts withheld without enough evidence."""

import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://yonixalpha:yonixalpha_test_pw@localhost:5432/yonixalpha_test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/9")
os.environ.setdefault("JWT_SECRET", "test-secret-test-secret-test-secret-32")
os.environ.setdefault("ADMIN_PASSWORD_HASH", "x")

import pytest  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from yonixalpha_core import forward_test as ft, solana_performance as sp  # noqa: E402
from yonixalpha_core.db.base import Base, make_session_factory  # noqa: E402
from yonixalpha_core.db.models import (  # noqa: E402
    AuditLog, ModelVersion, OpportunityOutcome, PaperAccount, PaperPosition, StrategyConfig,
)
from yonixalpha_core.ml import prediction_audit as pa  # noqa: E402
from yonixalpha_core.tools import performance_report  # noqa: E402

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)


def pos(pnl, *, mode="PAPER", engine="solana_momentum", exit_at=NOW - timedelta(hours=1), fees="0.001", status="closed",
        reason="stop_loss", mint=None, hold_s=60, plan=None, **kw):
    return PaperPosition(id=uuid.uuid4(), symbol="T", provider="paper", side="LONG", engine=engine, asset_id=mint or uuid.uuid4().hex,
                         entry_price=Decimal("1.02"), quantity=Decimal(1), take_profit=[], status=status, execution_mode=mode,
                         realized_pnl=None if pnl is None else Decimal(str(pnl)),
                         realized_pnl_pct=None if pnl is None else Decimal(str(pnl)) * 10,
                         fees_paid_quote=None if fees is None else Decimal(fees), entry_cost_quote=Decimal("0.1"),
                         proceeds_quote=Decimal(0), entry_at=exit_at - timedelta(seconds=hold_s),
                         exit_at=exit_at if status == "closed" else None, exit_reason=reason if status == "closed" else None,
                         plan=plan or {"entry_price": "1.0", "position_size": {"value": "0.1"}}, **kw)


@pytest.fixture
async def sf():
    engine = create_async_engine(os.environ["DATABASE_URL"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield make_session_factory(engine)
    await engine.dispose()


def test_judge_withholds_a_verdict_without_enough_evidence_and_names_what_is_missing():
    j = ft.judge([0.01] * 5, [0.0] * 40, tokens=5, elapsed_days=2, rules=ft.RULES)
    assert j["verdict"] == "INSUFFICIENT EVIDENCE"
    assert any("closed trades" in g for g in j["evidence_gaps"]) and any("days elapsed" in g for g in j["evidence_gaps"])
    assert any("distinct tokens" in g for g in j["evidence_gaps"])
    one = ft.judge([1.0] + [-0.001] * 39, [-0.01] * 40, tokens=40, elapsed_days=8, rules=ft.RULES)
    assert one["verdict"] == "INSUFFICIENT EVIDENCE" and "one trade carries it" in one["evidence_gaps"][0]


def test_judge_compares_with_the_baseline_through_an_interval():
    better = ft.judge([0.01, 0.012, 0.008] * 12, [-0.01, -0.012, -0.008] * 12, tokens=36, elapsed_days=7, rules=ft.RULES)
    assert better["verdict"] == "IMPROVING" and better["difference_ci_sol"][0] > 0
    worse = ft.judge([-0.01, -0.012, -0.008] * 12, [0.01, 0.012, 0.008] * 12, tokens=36, elapsed_days=7, rules=ft.RULES)
    assert worse["verdict"] == "DETERIORATING" and "not established" in worse["edge"]
    noisy = ft.judge([0.05, -0.05] * 18, [0.04, -0.05] * 18, tokens=36, elapsed_days=7, rules=ft.RULES)
    assert noisy["verdict"] == "INCONCLUSIVE"
    assert ft.judge([0.01] * 36, [], tokens=36, elapsed_days=7, rules=ft.RULES)["verdict"] == "NO BASELINE"
    assert ft.bootstrap_mean_ci([1.0, 2.0, 3.0]) == ft.bootstrap_mean_ci([1.0, 2.0, 3.0])  # deterministic


def test_prediction_is_prospective_only_when_trained_before_and_scored_before_the_horizon():
    d = NOW
    assert pa.classify(d, d + timedelta(minutes=2), d - timedelta(hours=1)) == "PROSPECTIVE"
    assert pa.classify(d, d + timedelta(minutes=6), d - timedelta(hours=1)) == "RETROSPECTIVE"  # after T+5m
    assert pa.classify(d, d + timedelta(minutes=1), d + timedelta(seconds=10)) == "RETROSPECTIVE"  # model newer than the decision
    assert pa.classify(d, None, d - timedelta(hours=1)) == "RETROSPECTIVE"


@pytest.mark.asyncio
async def test_metrics_count_each_cost_once_and_keep_open_positions_and_live_apart(sf):
    async with sf() as s:
        s.add(PaperAccount(name="solana", quote_currency="SOL", starting_balance=Decimal(10), cash_balance=Decimal(10)))
        win, loss, flat = pos("0.03", reason="take_profit_1"), pos("-0.01"), pos("0", reason="manual_exit")
        live = pos("-0.002", mode="LIVE")
        edge_in = pos("0.005", exit_at=NOW - timedelta(hours=24))     # exactly at since: inside
        edge_out = pos("0.5", exit_at=NOW)                            # exactly at until: outside
        other = pos("0.2", engine="solana_fresh")
        opened = pos(None, status="open", fees="0.0005", last_price=Decimal("2"), remaining_quantity=Decimal(1),
                     last_marked_at=NOW)
        s.add_all([win, loss, flat, live, edge_in, edge_out, other, opened])
        await s.flush()
        s.add(OpportunityOutcome(key="gate:w", mint="MW", engine="solana_momentum", stage="GATE", decision="EXECUTE",
                                 traded=True, position_id=win.id, decided_at=win.entry_at,
                                 snapshot={"age_seconds": 900, "deterioration_indicators": []},
                                 regime={"launches_last_hour_seen": 1200}))
        await s.commit()
        r = await sp.report(s, since=NOW - timedelta(hours=24), until=NOW, engine="solana_momentum", outcomes=False)
        o = r["trades_by_mode"]["PAPER"]["overall"]
        assert (o["trades"], o["wins"], o["losses"], o["breakeven"]) == (4, 2, 1, 1)
        assert o["net_pnl_sol"] == 0.025 and o["fees_sol"] == 0.004 and o["gross_pnl_sol"] == 0.029
        assert o["expectancy_sol"] == 0.00625 and o["largest_win_sol"] == 0.03 and o["largest_loss_sol"] == -0.01
        assert o["profit_factor"] == 3.5 and o["return_on_starting_capital_pct"] == 0.25
        assert r["trades_by_mode"]["LIVE"]["overall"]["trades"] == 1  # never mixed with PAPER
        assert r["trades_by_mode"]["LIVE"]["overall"]["return_on_starting_capital_pct"] is None
        assert [x["count"] for x in r["open_positions"]] == [1] and r["open_positions"][0]["status"] == "open"
        paper = r["trades_by_mode"]["PAPER"]
        assert paper["by_age"]["5-30m"]["trades"] == 1 and paper["by_age"]["UNKNOWN"]["trades"] == 3
        assert paper["by_regime"]["UNKNOWN"]["trades"] == 3 and paper["by_entry_quality"]["CLEAN"]["trades"] == 1
        assert r["coverage"]["PAPER"]["no_ledger_record"] == 3
        assert sum(d["trades"] for d in paper["by_day"].values()) == 4
        only_wins = await sp.report(s, since=NOW - timedelta(hours=2), until=NOW, engine="solana_fresh", outcomes=False)
        assert only_wins["trades_by_mode"]["PAPER"]["overall"]["profit_factor"] == "no losses"
        assert "undefined" in only_wins["trades_by_mode"]["PAPER"]["overall"]["profit_factor_note"]

        rows = await sp.positions(s, engine="solana_momentum", limit=20)
        op = next(p for p in rows if p["status"] == "open")
        assert op["net_pnl_sol"] is None and op["unrealized_marked_sol"] == 1.9  # 1 x 2 - 0.1 cost, labelled unrealized
        cl = next(p for p in rows if p["exit_reason"] == "take_profit_1")
        assert cl["gross_pnl_sol"] == 0.031 and cl["entry_slippage_estimate_pct"] == 2.0
        text = performance_report.render(await performance_report.build(
            s, ("positions", "day", "week", "all", "rejected", "predictions"), None, None, 5, 24, NOW), 24)
        assert "OPEN (not booked)" in text and "LAST 7 COMPLETE DAYS" in text and "Nothing was written" in text


@pytest.mark.asyncio
async def test_forward_test_freezes_rules_records_the_deployment_and_flags_changes(sf):
    async with sf() as s:
        s.add(StrategyConfig(strategy="solana_momentum", mode="PAPER", config={}))
        await ft.record_deployment(s, "abc1234!", NOW - timedelta(hours=1))
        await s.commit()
        p1 = await ft.start(s, None, label="first", user="op", now=NOW - timedelta(days=2))
        await s.commit()
        p2 = await ft.start(s, None, label="after deploy", user="op", now=NOW - timedelta(days=1))
        await s.commit()
        assert p2["deployment"]["revision"] == "abc1234" and p2["rules"]["min_closed_trades"] == 30
        hist = await ft.history(s)
        assert [h["id"] for h in hist] == [p2["id"], p1["id"]] and hist[1]["ended_at"]  # kept, ended, never deleted
        # a trade in the period, one before it (baseline)
        s.add_all([pos("0.01", exit_at=NOW - timedelta(hours=3)), pos("-0.02", exit_at=NOW - timedelta(days=3))])
        await s.commit()
        r = await ft.report(s, now=NOW)
        m = r["strategies"]["solana_momentum"]["modes"]["PAPER"]
        assert m["judgement"]["verdict"] == "INSUFFICIENT EVIDENCE" and m["judgement"]["closed_trades"] == 1
        assert m["judgement"]["baseline_trades"] == 1 and r["configuration_changed"] == []
        # the rules a period started with are the rules it is judged by
        ft.RULES["min_closed_trades"] = 1
        try:
            assert (await ft.report(s, now=NOW))["rules"]["min_closed_trades"] == 30
        finally:
            ft.RULES["min_closed_trades"] = 30
        # a new challenger (routine retraining) is never loaded for decisions: listed, not a configuration change
        s.add(ModelVersion(name="c", version=1, status="challenger", feature_names=[], training_sample_count=1,
                           metrics={}, artifact=b"x"))
        await s.commit()
        r = await ft.report(s, now=NOW)
        assert r["configuration_changed"] == [] and r["challenger_models"]["now"] == {"c": "v1 challenger"}
        # a configuration change after the start is named
        row = await s.get(StrategyConfig, "solana_momentum")
        row.mode = "AUTO"
        s.add(ModelVersion(name="m", version=1, status="active", feature_names=[], training_sample_count=1,
                           metrics={}, artifact=b"x"))
        await s.commit()
        changed = (await ft.report(s, now=NOW))["configuration_changed"]
        assert "strategy:solana_momentum" in changed and "models" in changed
        ev = (await s.execute(select(AuditLog.event_type))).scalars().all()
        assert ev.count("forward_test.started") == 2 and "deployment.recorded" in ev


@pytest.mark.asyncio
async def test_prediction_audit_evaluates_prospective_scores_only(sf):
    async with sf() as s:
        trained = NOW - timedelta(days=1)
        s.add(ModelVersion(name="shadow_ret", version=1, status="shadow", feature_names=[], training_sample_count=1,
                           metrics={}, artifact=b"x", trained_at=trained))
        rows = [("p1", 0.2, 10.0, 1), ("p2", -0.1, -5.0, 1), ("late", 0.3, 50.0, 30)]
        for key, pred, outcome, scored_min in rows:
            d = NOW - timedelta(hours=2)
            s.add(OpportunityOutcome(key=key, mint=key, engine="solana_fresh", stage="GATE", decision="REJECT", traded=False,
                                     decided_at=d, snapshot={}, status="COMPLETE", executable_return_pct=Decimal(str(outcome)),
                                     ml_shadow={"scored_at": (d + timedelta(minutes=scored_min)).isoformat(),
                                                "scores": {"E_EXECUTABLE_RETURN": {"value": pred, "model": "shadow_ret",
                                                                                   "version": 1}}}))
        await s.commit()
        a = await pa.audit(s, NOW - timedelta(hours=24), NOW)
        c = a["models"]["shadow_ret v1"]
        assert (c["PROSPECTIVE"], c["RETROSPECTIVE"]) == (2, 1)
        rm = a["return_model_prospective"]
        assert rm["favoured"] == {"n": 1, "mean_executable_return_pct": 10.0} and rm["all"]["n"] == 2  # the late +50 % is out


@pytest.mark.asyncio
async def test_the_regime_keeps_a_sol_price_only_from_before_the_decision():
    import json

    from yonixalpha_core import opportunities
    from yonixalpha_core.solana.sol_price import CACHE_KEY

    class Fake:
        def __init__(self, value):
            self.value = value

        async def get(self, key):
            assert key == CACHE_KEY
            return self.value

    before = json.dumps({"price": "150.1", "source": "jupiter", "at": (NOW - timedelta(seconds=30)).isoformat()})
    after = json.dumps({"price": "151", "source": "jupiter", "at": (NOW + timedelta(seconds=5)).isoformat()})
    assert await opportunities._sol_usd_before(Fake(before), NOW) == {"sol_usd": "150.1",
                                                                       "sol_usd_at": (NOW - timedelta(seconds=30)).isoformat()}
    assert await opportunities._sol_usd_before(Fake(after), NOW) == {}  # stamped after the decision: never used
    assert await opportunities._sol_usd_before(Fake(None), NOW) == {}


@pytest.mark.asyncio
async def test_a_live_entry_that_never_filled_is_neither_open_nor_a_trade_and_reasons_group_without_numbers(sf):
    async with sf() as s:
        failed = pos(None, mode="LIVE", status="failed")
        failed.quantity, failed.entry_cost_quote, failed.exit_at = Decimal(0), Decimal(0), NOW - timedelta(hours=1)
        s.add_all([failed, pos(None, status="open")])
        for i, liq in enumerate(("0.0031", "0.0051", "17.8841")):
            s.add(OpportunityOutcome(key=f"gate:m{i}", mint=f"MM{i}", engine="solana_migration", stage="GATE",
                                     decision="EXPIRED", traded=False, decided_at=NOW - timedelta(hours=2),
                                     reasons=[f"liquidity {liq} below minimum 20"], snapshot={}))
        await s.commit()
        r = await sp.report(s, since=NOW - timedelta(hours=24), until=NOW, engine="solana_momentum", outcomes=False)
        assert [o["status"] for o in r["open_positions"]] == ["open"] and r["failed_entries"] == {"LIVE": 1}
        assert "LIVE" not in r["trades_by_mode"]
        sigs = await ft.signals(s, NOW - timedelta(hours=24), NOW, ("solana_migration",))
        assert [(g["reason"], g["count"]) for g in sigs] == [("liquidity N below minimum N", 3)]
        text = performance_report.render(await performance_report.build(
            s, ("day", "rejected"), None, None, 5, 24, NOW), 24)
        assert "never filled (failed, no cost, not trades, not open): 1" in text
        assert "solana_migration: no decision has a measured outcome" in text and "PumpSwap pool" in text


@pytest.mark.asyncio
async def test_all_time_rates_count_from_the_first_entry_not_from_the_epoch(sf):
    async with sf() as s:
        s.add_all([pos("0.01", exit_at=NOW - timedelta(days=2)), pos("-0.01", exit_at=NOW - timedelta(hours=1))])
        await s.commit()
        r = await sp.report(s, since=performance_report.EPOCH, until=NOW, engine="solana_momentum", outcomes=False)
        assert 1.9 < r["days"] < 2.1 and r["rates_from"] > performance_report.EPOCH.isoformat()
        assert r["trades_by_mode"]["PAPER"]["overall"]["trades_per_day"] == 1.0
        week = await sp.report(s, since=NOW - timedelta(days=7), until=NOW, engine="solana_momentum", outcomes=False)
        assert week["days"] == 7 and week["trades_by_mode"]["PAPER"]["overall"]["trades_per_day"] == 0.29
