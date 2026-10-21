"""Entry comparison: earliest signal vs gate entry paired by token, the
extension buckets, and the skip rule chosen on the first 60 % only."""

import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://yonixalpha:yonixalpha_test_pw@localhost:5432/yonixalpha_test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/9")
os.environ.setdefault("JWT_SECRET", "test-secret-test-secret-test-secret-32")
os.environ.setdefault("ADMIN_PASSWORD_HASH", "x")

import pytest  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from yonixalpha_core.db.base import Base, make_session_factory  # noqa: E402
from yonixalpha_core.db.models import EntrySignal  # noqa: E402
from yonixalpha_core.tools import entry_compare as ec  # noqa: E402

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)


def sig(mint, strategy, at, price, ret):
    return {"mint": mint, "strategy": strategy, "lifecycle": "FRESH", "decided_at": at, "price_raw": price,
            "outcome": {"executable_return_pct": ret}}


def test_pairs_use_only_an_earlier_signal_of_the_same_token_and_the_move_between_them():
    t = NOW
    rows = [sig("A", "EARLY_ACCELERATION", t, 1.0, 30.0), sig("A", "SMART_WALLET_CONFIRMATION", t + timedelta(seconds=5), 1.1, 20.0),
            sig("A", ec.GATE, t + timedelta(seconds=60), 1.6, -10.0),
            sig("B", ec.GATE, t, 1.0, 5.0)]  # no earlier signal: not paired
    pairs = ec.earliest_refs(rows)
    assert len(pairs) == 1 and pairs[0][0]["strategy"] == "EARLY_ACCELERATION"
    assert round(ec.move(*pairs[0]), 4) == 0.6 and ec.bucket(0.6) == "50-100 %" and ec.bucket(-0.1) == "moved down"


def test_skip_rule_is_chosen_on_the_selection_period_and_reported_out_of_sample():
    pairs = []
    for i in range(40):
        at = NOW + timedelta(minutes=i)
        extended = i % 2 == 0
        a = sig(f"M{i}", "EARLY_ACCELERATION", at - timedelta(seconds=30), 1.0, 0.0)
        b = sig(f"M{i}", ec.GATE, at, 2.5 if extended else 1.1, -20.0 if extended else 10.0)
        pairs.append((a, b))
    r = ec.extension_rule(pairs)
    assert r["status"] == "evaluated" and r["chosen"] in (0.25, 0.5, 1.0)
    assert r["oos_rule"]["mean"] > r["oos_baseline"]["mean"]
    assert r["oos_baseline"]["n"] == 16 and r["selection_baseline"]["n"] == 24
    assert ec.extension_rule(pairs[:5])["status"].startswith("too few")


def test_summary_reports_the_result_without_the_best_signal():
    s = ec.summary([50.0, -5.0, -5.0])
    assert s["sum"] == 40.0 and s["sum_wo_best"] == -10.0 and round(s["pf"], 2) == 5.0


@pytest.mark.asyncio
async def test_report_runs_on_the_database(capsys):
    engine = create_async_engine(os.environ["DATABASE_URL"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    sf = make_session_factory(engine)
    async with sf() as s:
        for i, (strategy, ret) in enumerate((("EARLY_ACCELERATION", 12.0), (ec.GATE, -4.0))):
            s.add(EntrySignal(mint="MINT", strategy=strategy, lifecycle="FRESH", decision="CANDIDATE",
                              decided_at=NOW - timedelta(hours=2) + timedelta(minutes=i), price_raw=Decimal(1 + i),
                              outcome={"executable_return_pct": ret}, outcome_at=NOW))
        await s.commit()
    async with sf() as s:
        await ec.report(s, 14, NOW)
    await engine.dispose()
    out = capsys.readouterr().out
    assert "2 labelled signals" in out and "hold-out" in out and "1 pairs" in out and "moved 100-300 %" in out and "Nothing was written" in out
