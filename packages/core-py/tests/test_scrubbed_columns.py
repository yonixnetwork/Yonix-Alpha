"""Credential-bearing URLs never reach stored records (2026-10-15): decision
reasons, system events, audit entries, candidate and order details are
written through ScrubbedJSONB / ScrubbedString, for ORM objects and Core
insert()/update() statements alike. Other URLs are kept."""

import os
from datetime import datetime, timezone

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://yonixalpha:yonixalpha_test_pw@localhost:5432/yonixalpha_test")

import pytest_asyncio  # noqa: E402
from sqlalchemy import insert, select, update  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from yonixalpha_core.db.base import Base, make_session_factory  # noqa: E402
from yonixalpha_core.db.models import AuditLog, RiskAssessment, SystemEvent  # noqa: E402
from yonixalpha_core.redact import has_credential, scrub_credentials  # noqa: E402

KEYED = [
    "https://solana-mainnet.core.chainstack.com/0123456789abcdef0123456789abcdef",
    "https://mainnet.helius-rpc.com/?api-key=11111111-2222-3333-4444-555555555555",
    "https://example-name.solana-mainnet.quiknode.pro/abcdef0123456789/",
    "https://solana-mainnet.g.alchemy.com/v2/AbCdEf0123456789",
    "https://rpc.ankr.com/solana/0123456789abcdef",
    "wss://atlas-mainnet.helius-rpc.com/?api-key=abc",
    "https://user:hunter2@node.example.com/",
    "https://api.example.com/data?token=abc123",
    "https://pumpportal.fun/api/trade?api-key=SECRETXYZ",
    "https://api.telegram.org/bot123456:AAAbbbCCC/sendMessage",
]
KEPT = [
    "https://ipfs.io/ipfs/QmYwAPJzv5CZsnA625s3Xf2nemtYgPpHdWEz79ojWnPbdG",
    "https://pump.mypinata.cloud/ipfs/bafkreia",
    "https://solscan.io/token/So11111111111111111111111111111111111111112",
    "https://x.com/someproject/status/123",
    "https://frontend-api.pump.fun/coins/MINT",
]


def test_which_urls_count_as_credentials():
    assert all(has_credential(u) for u in KEYED)
    assert not any(has_credential(u) for u in KEPT)


def test_scrub_is_recursive_keeps_other_urls_and_returns_the_same_object_when_clean():
    reason = f"RPC error from {KEYED[0]} (403); metadata {KEPT[0]}"
    v = {"reasons": [reason, "clean"], "nested": {"err": KEYED[1]}, "n": 3, "ok": True}
    s = scrub_credentials(v)
    text = str(s)
    assert "0123456789abcdef0123456789abcdef" not in text and "api-key" not in text
    assert "https://solana-mainnet.core.chainstack.com/…" in text and KEPT[0] in text
    assert s["n"] == 3 and s["reasons"][1] == "clean"
    clean = {"a": [KEPT[1], {"b": "no url"}]}
    assert scrub_credentials(clean) is clean
    assert scrub_credentials("https://mainnet.helius-rpc.com/…") == "https://mainnet.helius-rpc.com/…"  # idempotent


@pytest_asyncio.fixture
async def sf():
    engine = create_async_engine(os.environ["DATABASE_URL"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield make_session_factory(engine)
    await engine.dispose()


def _ra(**kw):
    base = dict(idempotency_key="k1", engine="solana_migration", strategy="s", asset_id="MINT", decision="REJECT",
                status_label="REJECT", executable=False, execution_target="PAPER", overall_risk="HIGH",
                risk_engine_version="1", evaluated_at=datetime.now(timezone.utc))
    return {**base, **kw}


async def test_orm_and_core_writes_are_scrubbed(sf):
    async with sf() as s:
        s.add(SystemEvent(service="data-solana", event_type="rpc_health_check_failed", severity="error",
                          detail={"error": f"all endpoints failed: {KEYED[1]}", "meta": KEPT[0]}))
        s.add(AuditLog(event_type="rpc.provider_updated", detail={"note": KEYED[2]}))
        # Core insert, as safety.store writes assessments
        await s.execute(insert(RiskAssessment).values(**_ra(assessment={"reasons": [f"quote failed: {KEYED[0]}"]})))
        await s.commit()
    async with sf() as s:
        # Core update, as the paper-trading gate does
        await s.execute(update(RiskAssessment).where(RiskAssessment.idempotency_key == "k1")
                        .values(outcome={"error": KEYED[3]}))
        await s.commit()
    async with sf() as s:
        ev = (await s.execute(select(SystemEvent))).scalar_one()
        au = (await s.execute(select(AuditLog))).scalar_one()
        ra = (await s.execute(select(RiskAssessment))).scalar_one()
        # JSONB operators still work on the scrubbed columns
        reason = (await s.execute(select(RiskAssessment.assessment["reasons"][0].astext))).scalar_one()
    stored = str([ev.detail, au.detail, ra.assessment, ra.outcome, reason])
    for secret in ("11111111-2222", "abcdef0123456789", "0123456789abcdef0123456789abcdef", "AbCdEf0123456789"):
        assert secret not in stored, secret
    assert ev.detail["meta"] == KEPT[0]
    assert reason == "quote failed: https://solana-mainnet.core.chainstack.com/…"
    assert ev.detail["error"] == "all endpoints failed: https://mainnet.helius-rpc.com/…"


async def test_jsonb_key_and_containment_queries_still_work(sf):
    async with sf() as s:
        s.add(SystemEvent(service="x", event_type="e", severity="info", detail={"final": 1, "tags": ["a"]}))
        await s.commit()
    async with sf() as s:
        assert (await s.execute(select(SystemEvent).where(SystemEvent.detail.has_key("final")))).scalar_one()
        assert (await s.execute(select(SystemEvent).where(SystemEvent.detail.contains({"tags": ["a"]})))).scalar_one()
        assert (await s.execute(select(SystemEvent).where(~SystemEvent.detail.has_key("missing")))).scalar_one()


async def test_null_handling_is_the_same_as_plain_jsonb(sf):
    # never set -> SQL NULL (found by IS NULL, e.g. "not scored yet"); explicit None -> JSON null, as JSONB does
    async with sf() as s:
        s.add(SystemEvent(service="x", event_type="unset", severity="info"))
        s.add(SystemEvent(service="x", event_type="none", severity="info", detail=None))
        await s.commit()
    async with sf() as s:
        found = (await s.execute(select(SystemEvent.event_type).where(SystemEvent.detail.is_(None)))).scalars().all()
    assert found == ["unset"]
