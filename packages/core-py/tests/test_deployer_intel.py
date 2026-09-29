"""Deployer intelligence is time-aware: a decision at T sees only launches
created before T whose outcome resolved at or before T; one launch never
judges a deployer; the ledger fills the history; the gate acts only through
configured actions."""
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://yonixalpha:yonixalpha_test_pw@localhost:5432/yonixalpha_test")

import pytest_asyncio  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from yonixalpha_core import deployer_intel as di  # noqa: E402
from yonixalpha_core.db import models  # noqa: F401,E402
from yonixalpha_core.db.base import Base, make_session_factory  # noqa: E402
from yonixalpha_core.db.models import DeployerLaunch  # noqa: E402
from yonixalpha_core.safety.gate import assess  # noqa: E402
from yonixalpha_core.safety.models import FinalDecision  # noqa: E402
from yonixalpha_core.safety.settings import SafetySettings, validate  # noqa: E402
from yonixalpha_core.solana.flow import Trade  # noqa: E402
from tests.test_safety_gate import _intel, healthy  # noqa: E402

T = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
DEV = "Deployer111111111111111111111111111111111111"
SOL = 1_000_000_000


@pytest_asyncio.fixture
async def db():
    di._base_cache.clear()
    engine = create_async_engine(os.environ["DATABASE_URL"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with make_session_factory(engine)() as session:
        yield session
    await engine.dispose()


def launch(i: int, created: datetime, resolved: datetime | None, outcome: str | None, migrated=False, creator=DEV, sold=None):
    return DeployerLaunch(mint=f"M{i}", creator=creator, launch_created_at=created, first_seen_at=created,
                          resolved_at=resolved, outcome=outcome, migrated=migrated if resolved else None,
                          peak_mc_sol=Decimal(40 + i) if resolved else None, creator_sold_early=sold,
                          time_to_migration_seconds=900 if migrated else None)


async def test_features_never_use_launches_resolved_or_created_after_the_decision(db):
    h = timedelta(hours=1)
    db.add_all([
        launch(1, T - 5 * h, T - 4 * h, "LOSS", sold=True),
        launch(2, T - 4 * h, T - 3 * h, "LOSS", sold=True),
        launch(3, T - 3 * h, T - 2 * h, "WIN", migrated=True, sold=False),
        launch(4, T - 2 * h, T + h, "WIN"),  # created before T, resolved AFTER T: outcome invisible at T
        launch(5, T + h, T + 2 * h, "WIN"),  # created after T: invisible
        launch(6, T - h, None, None),  # not resolved yet
        launch(7, T - 9 * h, T - 8 * h, "LOSS", creator="SomeoneElse"),  # base rate only
    ])
    await db.commit()
    f = await di.features_asof(db, DEV, T, exclude_mint="CURRENT")
    assert f["deployer_history_cutoff"] == T.isoformat() and f["deployer_feature_timestamp"]
    assert f["deployer_launch_count"] == 5 and f["resolved_launches"] == 3 and f["status"] == "MEASURED"
    assert f["deployer_outcomes"] == {"WIN": 1, "FLAT": 0, "LOSS": 2}
    assert f["deployer_bond_rate"] == round(1 / 3, 4) and f["deployer_creator_sell_rate"] == round(2 / 3, 4)
    assert f["deployer_median_time_to_migration_seconds"] == 900 and f["launches_last_24h"] == 5
    # Shrunk toward the base rate of everything resolved by the 10-minute bucket start (3 LOSS of 4 → 0.75).
    assert f["base_rates"]["n"] == 4 and f["deployer_risk_score"] == round((2 + 5 * 0.75) / (3 + 5), 4)
    # Later, the same deployer's history includes launch 4 (resolved by then) but still not the current mint.
    later = await di.features_asof(db, DEV, T + 90 * timedelta(minutes=1), exclude_mint="M5")
    assert later["resolved_launches"] == 4


async def test_one_launch_never_judges_a_deployer(db):
    db.add(launch(1, T - timedelta(hours=2), T - timedelta(hours=1), "LOSS"))
    await db.commit()
    f = await di.features_asof(db, DEV, T)
    assert f["status"] == "INSUFFICIENT_HISTORY" and "3 needed" in f["reason"]
    assert (await di.features_asof(db, "Nobody", T))["status"] == "NO_HISTORY"
    assert (await di.features_asof(db, None, T))["status"] == "UNKNOWN"


async def test_ledger_notes_and_resolves_a_launch_once(db):
    created = T - timedelta(minutes=61)
    meta = {"creator": DEV, "created_at": int(created.timestamp())}
    assert await di.note_launch(db, "MX", meta, created + timedelta(seconds=10))
    assert not await di.note_launch(db, "MX", meta, created + timedelta(seconds=20))
    trades = [Trade(created + timedelta(seconds=1), DEV, True, 1 * SOL, 1000, 0, 0),
              Trade(created + timedelta(seconds=5), "Buyer", True, 2 * SOL, 900, 0, 0),
              Trade(created + timedelta(seconds=60), DEV, False, SOL, 400, 0, 0)]
    assert await di.resolve(db, "MX", meta, trades, outcome="LOSS", migrated_at=None, peak_mc_sol=Decimal("55"), now=T)
    assert not await di.resolve(db, "MX", meta, trades, outcome="WIN", migrated_at=None, peak_mc_sol=None, now=T)
    await db.commit()
    row = await db.get(DeployerLaunch, "MX")
    assert row.outcome == "LOSS" and row.resolved_at == T and row.creator_sold_early is True
    assert row.creator_sell_share == Decimal("0.4000") and row.volume_sol_60m == Decimal("4.0000") and row.unique_buyers_60m == 2
    assert row.migrated is False and row.peak_mc_sol == Decimal("55")


async def test_creator_behaviour_is_unknown_without_history_from_the_launch(db):
    created = T - timedelta(minutes=61)
    facts = di.launch_facts([Trade(created + timedelta(seconds=300), DEV, False, SOL, 10, 0, 0)], DEV, created, T)
    assert facts["creator_sold_early"] is None and facts["creator_sell_share"] is None


# --- gate -------------------------------------------------------------------------------------

def rel(**kw):
    base = {"status": "MEASURED", "buyers": {"effective_unique_buyers": 3, "explanation": "9 apparent buyers ... = 3 effective"},
            "demand": {"organic_demand_ratio": 0.1, "known_related_volume_sol": 9, "total_volume_sol": 10},
            "creator": {"creator_related_volume_ratio": 0.5}, "largest_dependent_cluster": 5,
            "clusters": [{"size": 5, "classification": "COORDINATED", "cluster_id": "fund-x", "evidence": ["e"]}],
            "smart_money": {"context": "MULTIPLE_RELATED", "smart_money_clustered": 2}}
    base.update(kw)
    return base


DEP = {"status": "MEASURED", "deployer_risk_score": 0.85, "resolved_launches": 6, "deployer_bond_rate": 0.0,
       "deployer_history_cutoff": T.isoformat()}
CODES = {"LOW_EFFECTIVE_BUYERS", "LOW_ORGANIC_DEMAND", "HIGH_COORDINATION", "CREATOR_CONCENTRATION",
         "HIGH_SMART_MONEY_CONCENTRATION", "POOR_DEPLOYER_HISTORY"}


def test_scanner_findings_warn_by_default_and_act_only_when_configured():
    a = assess(healthy(intel=_intel(relationships=rel(), deployer=DEP)), SafetySettings())
    found = {f.code: f for f in a.findings}
    assert CODES <= set(found) and all(found[c].action == FinalDecision.EXECUTE for c in CODES)
    assert a.executable  # WARN never blocks
    strict = replace(SafetySettings(), coordination_high_action="NO_TRADE", deployer_history_action="REQUIRE_MANUAL_APPROVAL")
    b = assess(healthy(intel=_intel(relationships=rel(), deployer=DEP)), strict)
    assert not b.executable and {f.code: f.action for f in b.findings}["HIGH_COORDINATION"] == FinalDecision.NO_TRADE


def test_missing_or_unmeasured_evidence_never_fires_and_zero_turns_a_check_off():
    unmeasured = rel(status="UNAVAILABLE")
    a = assess(healthy(intel=_intel(relationships=unmeasured, deployer={"status": "INSUFFICIENT_HISTORY"})), SafetySettings())
    assert not CODES & {f.code for f in a.findings}
    partial = rel(demand={"organic_demand_ratio": None, "organic_demand_ratio_lower": 0.0})
    b = assess(healthy(intel=_intel(relationships=partial)), SafetySettings())
    assert "LOW_ORGANIC_DEMAND" not in {f.code for f in b.findings}  # unattributed volume is not "low organic demand"
    off = replace(SafetySettings(), min_effective_buyers=0, min_organic_demand_ratio=Decimal(0), coordination_high_wallets=0,
                  max_creator_related_volume_ratio=Decimal(0), deployer_max_risk_score=Decimal(0))
    c = assess(healthy(intel=_intel(relationships=rel(smart_money={}), deployer=DEP)), off)
    assert not CODES & {f.code for f in c.findings}


def test_scanner_settings_are_validated():
    assert validate(replace(SafetySettings(), coordination_high_action="BUY"))
    assert validate(replace(SafetySettings(), min_organic_demand_ratio=Decimal("1.5")))
    assert not validate(SafetySettings())
