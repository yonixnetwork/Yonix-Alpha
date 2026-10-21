"""ML Training Lab and Signal Center: labels are opinions kept apart from the
recorded outcome, ideas become explicit experiments only when testable,
datasets carry no outcome as a feature, signals are immutable, recorded
only for new decisions (never backfilled) and never trade."""

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://yonixalpha:yonixalpha_test_pw@localhost:5432/yonixalpha_test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/9")
os.environ.setdefault("JWT_SECRET", "test-secret-test-secret-test-secret-32")
os.environ.setdefault("ADMIN_PASSWORD_HASH", "x")

import pytest  # noqa: E402
from sqlalchemy import func, select, text  # noqa: E402
from sqlalchemy.exc import DBAPIError  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from yonixalpha_core import ml_lab, signal_center as sc  # noqa: E402
from yonixalpha_core.db.base import Base, make_session_factory  # noqa: E402
from yonixalpha_core.db.models import (  # noqa: E402
    MlExperiment, MlSignal, MlSignalOutcome, OpportunityOutcome, PlatformSetting, TradingCandidate,
)
from yonixalpha_core.ml.opportunity_features import FEATURE_NAMES  # noqa: E402

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)


def opp(key, *, at=NOW, engine="solana_migration", liq=40.0, ex=None, peak=None, dd=None, traded=False, status="COMPLETE",
        path=None, post=None):
    return OpportunityOutcome(key=key, mint=f"M-{key}", engine=engine, stage="GATE", decision="REJECT", traded=traded,
                              decided_at=at, snapshot={"liquidity_sol": liq, "price_raw": 1.0} if liq is not None else {},
                              status=status, executable_return_pct=ex, peak_pct=peak, drawdown_pct=dd,
                              path=path, post_exit=post)


def test_verify_compares_the_opinion_with_the_recorded_outcome_and_never_claims_more():
    win = opp("w", ex=12, peak=40, dd=-5)
    loss = opp("l", ex=-20, peak=4, dd=-60)
    assert ml_lab.verify("GOOD_EARLY_ENTRY", win)["status"] == "AGREES"
    assert ml_lab.verify("GOOD_EARLY_ENTRY", loss)["status"] == "DISAGREES"
    assert ml_lab.verify("ENTRY_TOO_LATE", loss)["status"] == "AGREES"
    assert ml_lab.verify("UNSAFE_TOKEN", loss)["status"] == "AGREES"
    assert ml_lab.verify("NO_TRADE_MISSED", win)["status"] == "AGREES"
    assert ml_lab.verify("NO_TRADE_CORRECT", opp("t", ex=-3, traded=True))["status"] == "DISAGREES"
    assert ml_lab.verify("GOOD_EARLY_ENTRY", opp("p", status="TRACKING"))["status"] == "PENDING"
    assert ml_lab.verify("BUYING_WEAK", win)["status"] == "OPINION_ONLY"
    assert ml_lab.verify("SUCCESSFUL_EXIT", opp("x", post={"classification": "GOOD_EXIT"}))["status"] == "AGREES"
    assert len(ml_lab.LABELS) == 15


def test_an_idea_becomes_explicit_conditions_and_unrecorded_data_makes_it_untestable():
    p = ml_lab.propose("Migrated tokens with sustained buying volume, qualified wallets and liquidity above 30 SOL "
                       "before a large price extension; compare with the existing confirmation strategy over 15 minutes")
    conds = {c["feature"]: c for c in p["entry_conditions"]}
    assert p["categories"] == ["MIGRATED"] and p["testable"] and p["exit"]["horizon"] == "T+15m"
    assert conds["liquidity_sol"]["value"] == 30 and conds["smart_proven_wallets"]["value"] == 1  # no number leak
    assert conds["mom_return_5m"]["op"] == "<=" and "traded" in p["comparison"]
    assert all(c["feature"] in sc_features() for c in p["entry_conditions"])
    bad = ml_lab.propose("fresh tokens that trend on twitter with strong liquidity")
    assert not bad["testable"] and "twitter" in bad["untestable_reasons"][0]
    with pytest.raises(ml_lab.LabError):
        ml_lab.propose("buy")
    # an edit cannot make an idea that needs unrecorded data testable, or use an unknown feature
    edited = ml_lab.validate_edit(bad, {"entry_conditions": [{"feature": "liquidity_sol", "op": ">=", "value": 50}]})
    assert not edited["testable"] and "50 SOL" in edited["hypothesis"]
    with pytest.raises(ml_lab.LabError):
        ml_lab.validate_edit(p, {"entry_conditions": [{"feature": "executable_return_pct", "op": ">", "value": 0}]})
    with pytest.raises(ml_lab.LabError):
        ml_lab.validate_edit(p, {"exit": {"horizon": "T+2h"}})
    none = ml_lab.validate_edit(p, {"entry_conditions": []})
    assert not none["testable"]


def sc_features():
    from yonixalpha_core.ml.opportunity_features import NUMERIC
    return NUMERIC


def test_outcome_from_the_path_reports_cost_aware_return_and_which_level_came_first():
    path = {"T+60s": {"change_pct": 5, "peak_so_far_pct": 8, "drawdown_so_far_pct": -2, "executable": {"executable_return_pct": 1}},
            "T+5m": {"change_pct": -10, "peak_so_far_pct": 25, "drawdown_so_far_pct": -12,
                     "executable": {"executable_return_pct": -14.0}}}
    o = sc.outcome_from_path(path, "T+5m", {"target_pct": 20, "invalidation_pct": 15})
    assert o["first"] == "TARGET" and o["direction_correct"] is False and o["executable_return_pct"] == -14.0
    assert o["net_profitable"] is False and o["mfe_pct"] == 25
    # executable round trip not computed yet (ledger still tracking): no outcome yet
    assert sc.outcome_from_path({"T+5m": {"change_pct": 3}}, "T+5m", {}) is None
    unknown = sc.outcome_from_path({"T+5m": {"change_pct": 3, "unknown": "no sell route"}}, "T+5m", {})
    assert unknown["executable"] is False and unknown["would_fail"] == "no sell route"


def test_verdict_needs_enough_signals_and_beats_the_comparison_without_the_best_one():
    good = sc.metrics([5.0, -2.0] * 10)
    assert sc.verdict(10, good, {})["state"] == "INSUFFICIENT_DATA"
    assert sc.verdict(40, good, sc.metrics([-1.0] * 20))["state"] == "SUPPORTED_FORWARD"
    one_winner = sc.metrics([200.0] + [-3.0] * 19)
    v = sc.verdict(40, one_winner, sc.metrics([-5.0] * 20))
    assert v["state"] == "NOT_SUPPORTED" and "without the best" in v["reason"]
    assert sc.verdict(40, good, sc.metrics([3.0] * 20))["state"] == "NOT_SUPPORTED"


def test_lab_code_cannot_trade_or_change_a_mode():
    for mod in (ml_lab, sc):
        src = Path(mod.__file__).read_text()
        for word in ("TradingCandidate", "PaperPosition", "LivePosition", "global_mode", "execution", "PlatformSetting"):
            assert word not in src, (mod.__name__, word)


@pytest.fixture
async def sf():
    engine = create_async_engine(os.environ["DATABASE_URL"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield make_session_factory(engine)
    await engine.dispose()


@pytest.mark.asyncio
async def test_labels_copy_decision_time_features_and_the_dataset_keeps_outcomes_out_of_features(sf):
    async with sf() as s:
        s.add_all([opp("a", ex=10, peak=30, dd=-4), opp("b", engine="solana_fresh", ex=-8, peak=2, dd=-30, liq=None)])
        await s.commit()
        a = (await s.execute(select(OpportunityOutcome).where(OpportunityOutcome.key == "a"))).scalar_one()
        lab = await ml_lab.add_label(s, a.id, "GOOD_EARLY_ENTRY", value=12.5, notes="clean breakout", user="op")
        await s.commit()
        assert lab.features["liquidity_sol"] == 40.0 and lab.feature_version and lab.source == "MANUAL"
        assert lab.decided_at == a.decided_at
        with pytest.raises(ml_lab.LabError):
            await ml_lab.add_label(s, a.id, "GOOD_EARLY_ENTRY", user="op")  # same label twice
        with pytest.raises(ml_lab.LabError):
            await ml_lab.add_label(s, a.id, "PROFIT_GUARANTEED", user="op")
        await ml_lab.add_label(s, a.id, "GOOD_EARLY_ENTRY", user="other")  # another operator may agree
        await s.commit()
        summary = await ml_lab.label_summary(s)
        assert summary["labels"]["GOOD_EARLY_ENTRY"]["AGREES"] == 2
        tl = await ml_lab.timeline(s, a.id)
        assert len(tl["labels"]) == 2 and tl["labels"][0]["verification"]["status"] == "AGREES"
        q = await ml_lab.queue(s, hours=1, now=NOW + timedelta(minutes=5))
        assert {r["category"] for r in q["rows"]} == {"MIGRATED", "FRESH"}
        assert next(r for r in q["rows"] if r["mint"] == "M-a")["labels"] == 2

        d = await ml_lab.dataset(s, days=1, now=NOW + timedelta(minutes=5))
        assert d["count"] == 2 and d["duplicates_dropped"] == 0
        assert set(FEATURE_NAMES).isdisjoint(ml_lab.OUTCOME_COLUMNS)
        assert not [n for n in FEATURE_NAMES if n.startswith(("outcome", "executable", "peak", "drawdown", "path"))]
        row_a = next(r for r in d["rows"] if r["key"] == "a")
        assert row_a["outcome"]["executable_return_pct"] == 10.0 and row_a["manual_labels"] == ["GOOD_EARLY_ENTRY"] * 2
        assert d["missing_by_feature"].get("liquidity_sol") == 1  # the fresh row had no liquidity: counted, not guessed
        csv = ml_lab.dataset_csv(d).splitlines()
        assert len(csv) == 3 and "outcome_executable_return_pct" in csv[0]


async def accepted(s, *, at, conds=None, cats=("MIGRATED",), horizon="T+5m", status="ACCEPTED"):
    prop = ml_lab.propose("migrated tokens with liquidity above 30 SOL")
    prop = ml_lab.validate_edit(prop, {"categories": list(cats), "exit": {"horizon": horizon, "target_pct": 20,
                                                                          "invalidation_pct": 15},
                                       **({"entry_conditions": conds} if conds is not None else {})})
    e = MlExperiment(idea="test", status=status, proposal=prop, testable=prop["testable"], created_by="op",
                     decided_by="op", decided_at=at)
    s.add(e)
    await s.flush()
    return e


@pytest.mark.asyncio
async def test_signals_are_recorded_once_for_new_decisions_only_and_cannot_be_changed(sf):
    async with sf() as s:
        e = await accepted(s, at=NOW)
        await accepted(s, at=NOW, status="DRAFT")  # a draft records nothing
        s.add_all([
            opp("before", at=NOW - timedelta(seconds=30)),  # decided before the acceptance: never backfilled
            opp("match", at=NOW + timedelta(seconds=10)),
            opp("low", at=NOW + timedelta(seconds=20), liq=5.0),
            opp("missing", at=NOW + timedelta(seconds=25), liq=None),  # feature missing: no signal, never guessed
            opp("fresh_engine", at=NOW + timedelta(seconds=30), engine="solana_fresh"),
            opp("stale", at=NOW + timedelta(seconds=40)),
        ])
        await s.commit()
        candidates_before = (await s.execute(select(func.count()).select_from(TradingCandidate))).scalar_one()
        settings_before = (await s.execute(select(func.count()).select_from(PlatformSetting))).scalar_one()

        g = await sc.generate(s, now=NOW + timedelta(seconds=60))
        await s.commit()
        assert g["signals"] == 2 and g["missing_inputs"] == 1  # match + stale (both within 120 s)
        again = await sc.generate(s, now=NOW + timedelta(seconds=70))
        await s.commit()
        assert again["signals"] == 0
        # a decision first seen more than FRESH_SECONDS later is not signalled
        s.add(opp("late_seen", at=NOW + timedelta(seconds=80)))
        await s.commit()
        assert (await sc.generate(s, now=NOW + timedelta(seconds=80 + sc.FRESH_SECONDS + 30)))["signals"] == 0
        await s.commit()

        sigs = (await s.execute(select(MlSignal).order_by(MlSignal.decided_at))).scalars().all()
        assert [x.mint for x in sigs] == ["M-match", "M-stale"] and sigs[0].experiment_id == e.id
        assert sigs[0].inputs["features"] == {"liquidity_sol": 40.0} and sigs[0].created_at is not None
        assert (await s.execute(select(func.count()).select_from(TradingCandidate))).scalar_one() == candidates_before
        assert (await s.execute(select(func.count()).select_from(PlatformSetting))).scalar_one() == settings_before
        sid = sigs[0].id

    for stmt in ("UPDATE ml_signals SET horizon = 'T+60m' WHERE id = :id", "DELETE FROM ml_signals WHERE id = :id"):
        async with sf() as s:
            with pytest.raises(DBAPIError, match="immutable"):
                await s.execute(text(stmt), {"id": sid})
                await s.commit()


@pytest.mark.asyncio
async def test_outcomes_are_written_after_the_horizon_and_the_report_compares_with_the_group(sf):
    async with sf() as s:
        e = await accepted(s, at=NOW)
        good_path = {"T+5m": {"change_pct": 9, "peak_so_far_pct": 22, "drawdown_so_far_pct": -3,
                              "executable": {"executable_return_pct": 6.0}}}
        s.add(opp("x", at=NOW + timedelta(seconds=5), status="TRACKING"))
        s.add(opp("other", at=NOW + timedelta(seconds=6), liq=5.0, path={"T+5m": {"executable": {"executable_return_pct": -4.0}}}))
        await s.commit()
        await sc.generate(s, now=NOW + timedelta(seconds=30))
        await s.commit()
        assert (await sc.evaluate(s, now=NOW + timedelta(minutes=2)))["evaluated"] == 0  # horizon not reached
        assert (await sc.evaluate(s, now=NOW + timedelta(minutes=10)))["evaluated"] == 0  # ledger has no path yet
        x = (await s.execute(select(OpportunityOutcome).where(OpportunityOutcome.key == "x"))).scalar_one()
        x.path, x.status = good_path, "COMPLETE"
        await s.commit()
        assert (await sc.evaluate(s, now=NOW + timedelta(minutes=70)))["evaluated"] == 1
        await s.commit()
        o = (await s.execute(select(MlSignalOutcome))).scalar_one().outcome
        assert o["executable_return_pct"] == 6.0 and o["first"] == "TARGET" and o["direction_correct"] is True
        r = await sc.report(s, e.id)
        assert r["signals"] == 1 and r["evaluated"] == 1 and r["all"]["mean"] == 6.0
        assert r["comparison"]["all"]["n"] == 2  # both decisions of the category with a measured return
        assert r["verdict"]["state"] == "INSUFFICIENT_DATA"
