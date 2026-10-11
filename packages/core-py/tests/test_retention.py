"""Data retention (2026-10-11): rows older than the retention window are
removed only where nothing a trade, an ML model or an audit depends on
points to them; it does nothing until the operator enables it; the preview
counts what a run would delete."""

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://yonixalpha:yonixalpha_test_pw@localhost:5432/yonixalpha_test")

import uuid  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402
from decimal import Decimal  # noqa: E402

import pytest_asyncio  # noqa: E402
from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from yonixalpha_core import retention  # noqa: E402
from yonixalpha_core.db import models as m  # noqa: E402
from yonixalpha_core.db.base import Base, make_session_factory  # noqa: E402

NOW = datetime.now(timezone.utc)
OLD = NOW - timedelta(days=4)
NEW = NOW - timedelta(hours=5)


@pytest_asyncio.fixture
async def sf():
    engine = create_async_engine(os.environ["DATABASE_URL"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield make_session_factory(engine)
    await engine.dispose()


def assessment(cand, at, executable=False):
    return m.RiskAssessment(idempotency_key=str(uuid.uuid4()), candidate_id=cand.id if cand else None, engine="solana_fresh",
                            strategy="solana_fresh", asset_id="MINT", decision="EXECUTE" if executable else "NO_TRADE",
                            status_label="x", executable=executable, execution_target="PAPER", overall_risk="LOW",
                            risk_engine_version="1", assessment={}, evaluated_at=at)


async def seed(sf):
    ids = {}
    async with sf() as s:
        tok_old = m.Token(mint_address="OLDTOKEN", first_seen_source="pump_stream", first_seen_at=OLD)
        tok_kept = m.Token(mint_address="TRADEDTOKEN", first_seen_source="pump_stream", first_seen_at=OLD)
        tok_new = m.Token(mint_address="NEWTOKEN", first_seen_source="pump_stream", first_seen_at=NEW)
        s.add_all([tok_old, tok_kept, tok_new])
        await s.flush()

        def cand(token, state, at):
            return m.TradingCandidate(token_id=token.id, engine="discovery", state=state, state_history=[],
                                      detail={"source": "pump_stream"}, created_at=at)
        c_plain = cand(tok_kept, "rejected", OLD)      # finished, nothing points to it -> deleted
        c_traded = cand(tok_kept, "closed", OLD)       # has a position -> kept
        c_ml = cand(tok_kept, "rejected", OLD)         # has an ML feature row -> kept
        c_signal = cand(tok_kept, "rejected", OLD)     # linked to an entry signal -> kept
        c_active = cand(tok_kept, "analyzing", OLD)    # not finished -> kept (the stale expiry ends it first)
        c_new = cand(tok_new, "rejected", NEW)         # too young -> kept
        s.add_all([c_plain, c_traded, c_ml, c_signal, c_active, c_new])
        await s.flush()
        a_plain = assessment(c_plain, OLD)                     # deleted
        a_exec = assessment(c_traded, OLD, executable=True)    # kept: it traded
        a_ml = assessment(c_ml, OLD)                           # kept: an ML feature row points to it
        a_new = assessment(c_new, NEW)                         # kept: too young
        s.add_all([a_plain, a_exec, a_ml, a_new])
        await s.flush()
        s.add(m.PaperPosition(candidate_id=c_traded.id, assessment_id=a_exec.id, symbol="T", provider="paper", side="LONG",
                              entry_price=Decimal(1), quantity=Decimal(1), entry_at=OLD, asset_id="TRADEDTOKEN"))
        s.add(m.MLFeatureSnapshot(candidate_id=c_ml.id, assessment_id=a_ml.id, symbol="T", features={"x": 1}))
        s.add(m.EntrySignal(mint="TRADEDTOKEN", strategy="EARLY_ACCELERATION", lifecycle="FRESH", decision="CANDIDATE",
                            decided_at=OLD, candidate_id=c_signal.id))
        s.add_all([m.Notification(kind="k", title="old", created_at=OLD), m.Notification(kind="k", title="new", created_at=NEW)])
        s.add_all([m.SystemEvent(service="x", event_type="info_old", severity="info", created_at=OLD),
                   m.SystemEvent(service="x", event_type="error_old", severity="error", created_at=OLD)])
        s.add_all([m.TradeTimelineEvent(event_type="assessed", occurred_at=NEW, candidate_id=c_new.id),
                   m.TradeTimelineEvent(event_type="loose_old", occurred_at=OLD)])
        s.add_all([m.TokenEvent(token_id=tok_old.id, event_type="trade", source="rpc", occurred_at=OLD, payload={}),
                   m.TokenEvent(token_id=tok_kept.id, event_type="migration", source="rpc", occurred_at=OLD, payload={})])
        await s.commit()
        ids.update(c_plain=c_plain.id, a_plain=a_plain.id, tok_old=tok_old.id)
    return ids


async def count(sf, model, *where):
    async with sf() as s:
        return (await s.execute(select(func.count()).select_from(model).where(*where))).scalar_one()


async def test_nothing_is_deleted_until_the_operator_enables_it(sf):
    await seed(sf)
    assert await retention.run(sf, NOW) == {"skipped": "disabled"}
    assert await count(sf, m.Notification) == 2


async def test_preview_counts_what_a_run_deletes_and_a_run_keeps_everything_important(sf):
    ids = await seed(sf)
    p = await retention.preview(sf, NOW)
    assert p["enabled"] is False and p["days"] == 3
    expected = {"notifications": 1, "system_events": 1, "risk_assessments": 1, "trading_candidates": 0,
                "trade_timeline_events": 1, "token_events": 1, "tokens": 0}
    assert p["would_delete"] == expected  # candidates/tokens free up only after their assessments/events go
    async with sf() as s:
        await retention.save_settings(s, True, 3, "test")
    out = await retention.run(sf, NOW)
    d = out["deleted"]
    assert d["notifications"] == 1 and d["system_events"] == 1 and d["risk_assessments"] == 1
    assert d["trading_candidates"] == 1  # the plain one, once its assessment was gone (same run: ordered rules)
    assert d["token_events"] == 1 and d["tokens"] == 1
    # deleted
    assert await count(sf, m.TradingCandidate, m.TradingCandidate.id == ids["c_plain"]) == 0
    assert await count(sf, m.RiskAssessment, m.RiskAssessment.id == ids["a_plain"]) == 0
    assert await count(sf, m.Token, m.Token.id == ids["tok_old"]) == 0
    # kept: trades, ML rows and their links, signals, unfinished and young rows, errors, migration events
    assert await count(sf, m.TradingCandidate) == 5 and await count(sf, m.RiskAssessment) == 3
    assert await count(sf, m.PaperPosition) == 1 and await count(sf, m.MLFeatureSnapshot) == 1
    async with sf() as s:
        ml = (await s.execute(select(m.MLFeatureSnapshot))).scalar_one()
        assert ml.candidate_id is not None and ml.assessment_id is not None  # the ML grouping keys survive
    assert await count(sf, m.EntrySignal) == 1
    assert await count(sf, m.SystemEvent, m.SystemEvent.severity == "error") == 1
    assert await count(sf, m.TokenEvent, m.TokenEvent.event_type == "migration") == 1
    assert await count(sf, m.Notification) == 1 and await count(sf, m.TradeTimelineEvent) == 1
    assert await count(sf, m.AuditLog, m.AuditLog.event_type == "retention.run") == 1
    # a second run finds nothing more
    assert not any((await retention.run(sf, NOW))["deleted"].values())


def test_retention_days_are_bounded():
    assert retention.parse_settings({"days": 0})["days"] == retention.MIN_DAYS
    assert retention.parse_settings({"days": "x"})["days"] == retention.DEFAULT_DAYS
    assert retention.parse_settings(None) == {"enabled": False, "days": 3}
