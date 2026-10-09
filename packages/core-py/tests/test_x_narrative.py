"""X narrative intelligence (shadow, 2026-10-10) against a mocked X API:
identity needs evidence, a ticker or a famous name is not identification,
spam and few-author bursts are flagged, no data is NO_DATA (never neutral),
failures never block and never fabricate, the budget and the switches hold,
and features are only read as of a decision time. No network."""

import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://yonixalpha:yonixalpha_test_pw@localhost:5432/yonixalpha_test")

import httpx  # noqa: E402
import pytest_asyncio  # noqa: E402
from pydantic import SecretStr  # noqa: E402
from redis.asyncio import from_url  # noqa: E402
from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from yonixalpha_core import x_narrative as xn  # noqa: E402
from yonixalpha_core.db.base import Base, make_session_factory  # noqa: E402
from yonixalpha_core.db.models import PlatformSetting, XNarrativeObservation  # noqa: E402

MINT = "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"
OTHER = "9n4nbM75f5Ui33ZbPYXn59EwSgE8CGsHtAeTH5YFeJ9E"
NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
ON = SimpleNamespace(X_NARRATIVE_ENABLED=True, X_API_BEARER_TOKEN=SecretStr("test-bearer-not-real"))


def post(i, text, author="a1", age_s=30, kind=None, likes=0, urls=None, cashtags=None):
    p = {"id": str(1000 + i), "text": text, "author_id": author,
         "created_at": (NOW - timedelta(seconds=age_s)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
         "public_metrics": {"like_count": likes, "retweet_count": 0, "reply_count": 0, "quote_count": 0},
         "entities": {}}
    if urls:
        p["entities"]["urls"] = [{"expanded_url": u} for u in urls]
    if cashtags:
        p["entities"]["cashtags"] = [{"tag": t} for t in cashtags]
    if kind:
        p["referenced_tweets"] = [{"type": kind, "id": "1"}]
    return p


def body(posts, users=None):
    return {"data": posts, "includes": {"users": users or []}}


def test_exact_mint_in_text_or_url_identifies_the_token():
    cls, _ = xn.classify_post(post(1, f"aping {MINT} now"), MINT, "Moon Dog", "MDOG")
    assert cls == xn.EXACT_MINT
    cls, _ = xn.classify_post(post(2, "chart", urls=[f"https://pump.fun/coin/{MINT}"]), MINT, "Moon Dog", "MDOG")
    assert cls == xn.EXACT_MINT


def test_a_ticker_alone_is_ambiguous_and_a_different_address_conflicts():
    cls, notes = xn.classify_post(post(1, "$MDOG to the moon", cashtags=["MDOG"]), MINT, "Moon Dog", "MDOG")
    assert cls == xn.TICKER_ONLY and "many tokens share a ticker" in notes[0]
    cls, _ = xn.classify_post(post(2, f"$MDOG real CA {OTHER}", cashtags=["MDOG"]), MINT, "Moon Dog", "MDOG")
    assert cls == xn.CONFLICT  # the same ticker, but another token
    cls, _ = xn.classify_post(post(3, "Moon Dog $MDOG launching", cashtags=["MDOG"]), MINT, "Moon Dog", "MDOG")
    assert cls == xn.NAME_TICKER


def test_a_celebrity_name_is_not_identification_or_endorsement():
    # token named after a person; the person's own post mentions their name, not the token
    a = xn.analyse(body([post(1, "Elon Musk at the launch event today", author="u1")],
                        [{"id": "u1", "username": "elonmusk", "verified": True}]), MINT, "Elon Musk", "ELON", NOW)
    assert a["status"] == xn.NO_MATCH and a["narrative_score"] is None  # NAME_ONLY stays a candidate
    # an account whose handle resembles the token name is flagged, never credited
    b = xn.analyse(body([post(2, f"{MINT} official", author="u2")], [{"id": "u2", "username": "moondogofficial"}]),
                   MINT, "Moon Dog", "MDOG", NOW)
    assert any("not treated as an endorsement" in n for n in b["evidence"][0]["notes"])


def test_relevant_original_posts_from_independent_authors_score():
    posts = [post(i, f"{MINT} is moving {i}", author=f"a{i}", age_s=20 * i) for i in range(1, 6)]
    a = xn.analyse(body(posts), MINT, "Moon Dog", "MDOG", NOW, onchain_score=Decimal("60"))
    assert a["status"] == xn.OK and a["distinct_authors"] == 5 and a["social_data_quality"] == "OK"
    assert a["narrative_score"] > 0 and a["coordination_flags"] == []
    assert a["identity_confidence"] == "0.95" and a["posts_by_kind"]["original"] == 5
    assert a["combined_signal_score"] == "60"  # narrative_weight 0 (shadow): equals the on-chain score
    w = xn.analyse(body(posts), MINT, "Moon Dog", "MDOG", NOW, onchain_score=Decimal("60"), narrative_weight=Decimal("0.5"))
    assert Decimal(w["combined_signal_score"]) == (Decimal("60") + Decimal(str(w["narrative_score"]))) / 2


def test_spam_and_few_authors_are_flagged_not_counted_as_many_people():
    same = [post(i, f"{MINT} BUY NOW 100x", author="bot1" if i % 2 else "bot2", age_s=2 * i, likes=500) for i in range(1, 9)]
    a = xn.analyse(body(same), MINT, "Moon Dog", "MDOG", NOW)
    assert a["distinct_authors"] == 2 and a["engagement_total"] == 8 * 500
    assert {"REPEATED_CONTENT", "FEW_INDEPENDENT_AUTHORS", "SYNCHRONIZED_BURST"} <= set(a["coordination_flags"])
    clean = xn.analyse(body([post(i, f"{MINT} note {i}", author=f"x{i}", age_s=60 * i) for i in range(1, 9)]),
                       MINT, "Moon Dog", "MDOG", NOW)
    assert a["narrative_score"] < clean["narrative_score"]  # 8 posts by 2 accounts are worth less than 8 people


def test_no_posts_is_no_data_and_irrelevant_posts_are_no_match():
    a = xn.analyse(body([]), MINT, "Moon Dog", "MDOG", NOW)
    assert a["status"] == xn.NO_DATA and a["narrative_score"] is None and a["identity_confidence"] is None
    b = xn.analyse(body([post(1, "$MDOG", cashtags=["MDOG"])]), MINT, "Moon Dog", "MDOG", NOW)
    assert b["status"] == xn.NO_MATCH and b["narrative_score"] is None


def test_few_posts_are_insufficient_data_not_a_score():
    a = xn.analyse(body([post(1, f"{MINT}", author="a1")]), MINT, "Moon Dog", "MDOG", NOW)
    assert a["status"] == xn.OK and a["social_data_quality"] == xn.INSUFFICIENT_DATA and a["narrative_score"] is None


def test_first_relevant_post_and_engagement_only_when_returned():
    p = post(1, f"{MINT}", age_s=7200)  # posted before the token was discovered: recorded as such
    p.pop("public_metrics")
    a = xn.analyse(body([p, post(2, f"{MINT} b", author="a2", age_s=60), post(3, f"{MINT} c", author="a3", age_s=30)]),
                   MINT, "Moon Dog", "MDOG", NOW)
    assert a["first_relevant_post_at"].startswith("2026-10-10T10:00")
    assert a["windows"]["3600s"]["posts"] == 2 and a["windows"]["60s"]["posts"] == 2


def test_query_is_bounded_and_needs_no_ticker():
    q = xn.build_query(MINT, 'Moon "Dog"', "MDOG")
    assert MINT in q and "$MDOG" in q and "-is:retweet" in q and len(q) <= 512 and '""' not in q
    assert xn.build_query(MINT, None, None) == f'("{MINT}") -is:retweet'


def test_settings_validation_and_status_states():
    s, errors = xn.parse_settings({"max_posts_per_query": 5, "provider": "scraper", "narrative_weight": "2"})
    assert len(errors) == 3
    assert xn.provider_status(SimpleNamespace(X_NARRATIVE_ENABLED=True, X_API_BEARER_TOKEN=None)) == xn.NOT_CONFIGURED
    assert xn.provider_status(SimpleNamespace(X_NARRATIVE_ENABLED=False, X_API_BEARER_TOKEN=SecretStr("t"))) == xn.DISABLED
    assert xn.provider_status(ON, xn.XNarrativeSettings(enabled=False)) == xn.DISABLED
    assert xn.provider_status(ON, xn.XNarrativeSettings(enabled=True)) == "ENABLED"


@pytest_asyncio.fixture
async def redis():
    r = from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/15"), decode_responses=True)
    await r.flushdb()
    yield r
    await r.flushdb()
    await r.aclose()


@pytest_asyncio.fixture
async def sf():
    engine = create_async_engine(os.environ["DATABASE_URL"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield make_session_factory(engine)
    await engine.dispose()


def client_for(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _enable(sf, **extra):
    async with sf() as s:
        s.add(PlatformSetting(key=xn.SETTINGS_KEY, value={"enabled": True, **extra}))
        await s.commit()


async def test_pass_looks_up_queued_candidates_and_stores_only_derived_data(redis, sf):
    await _enable(sf)
    seen = []

    def handler(request):
        seen.append(request)
        posts = [post(i, f"secret text {MINT} {i}", author=f"a{i}") for i in range(1, 4)]
        return httpx.Response(200, json=body(posts))

    assert await xn.enqueue(redis, MINT, "Moon Dog", "MDOG", {"qualified": True, "onchain_score": "55"}, NOW.timestamp())
    assert not await xn.enqueue(redis, MINT, "Moon Dog", "MDOG", {"qualified": True}, NOW.timestamp())  # duplicate event
    async with client_for(handler) as c:
        out = await xn.run_pass(sf, redis, ON, c, NOW)
    assert out["stored"] == 1 and len(seen) == 1
    assert seen[0].headers["authorization"] == "Bearer test-bearer-not-real" and seen[0].url.path == "/2/tweets/search/recent"
    async with sf() as s:
        row = (await s.execute(select(XNarrativeObservation))).scalar_one()
    stored = json.dumps({"f": row.features, "e": row.evidence})
    assert row.status == xn.OK and "secret text" not in stored and "test-bearer" not in stored  # no post text, no token
    assert row.evidence[0]["link"].startswith("https://x.com/i/web/status/")
    assert await xn.spent_today(redis, NOW) == Decimal("0.015")  # 3 posts x $0.005
    # the same posts again today cost nothing (X deduplicates per UTC day)
    assert await xn.charge(redis, ["1001", "1002", "1003"], NOW) == 0


async def test_disabled_or_missing_token_makes_no_request(redis, sf):
    def handler(request):
        raise AssertionError("no request may be sent")

    await xn.enqueue(redis, MINT, "Moon Dog", "MDOG", {"qualified": True}, NOW.timestamp())
    async with client_for(handler) as c:
        assert (await xn.run_pass(sf, redis, ON, c, NOW))["status"] == xn.DISABLED  # dashboard switch off
        await _enable(sf)
        none = SimpleNamespace(X_NARRATIVE_ENABLED=True, X_API_BEARER_TOKEN=None)
        assert (await xn.run_pass(sf, redis, none, c, NOW))["status"] == xn.NOT_CONFIGURED


async def test_only_qualified_candidates_are_looked_up_by_default(redis, sf):
    await _enable(sf)

    def handler(request):
        raise AssertionError("an unqualified candidate must not cost a request")

    await xn.enqueue(redis, MINT, "Moon Dog", "MDOG", {"qualified": False}, NOW.timestamp())
    async with client_for(handler) as c:
        assert (await xn.run_pass(sf, redis, ON, c, NOW))["looked_up"] == 0


async def test_timeout_and_malformed_are_recorded_never_guessed(redis, sf):
    await _enable(sf)

    def timeout(request):
        raise httpx.ReadTimeout("slow")

    await xn.enqueue(redis, MINT, "Moon Dog", "MDOG", {"qualified": True}, NOW.timestamp())
    async with client_for(timeout) as c:
        await xn.run_pass(sf, redis, ON, c, NOW)
    await xn.enqueue(redis, OTHER, "X", "X", {"qualified": True}, NOW.timestamp())
    async with client_for(lambda r: httpx.Response(200, text="<html>")) as c:
        await xn.run_pass(sf, redis, ON, c, NOW)
    async with sf() as s:
        rows = {r.mint: r for r in (await s.execute(select(XNarrativeObservation))).scalars()}
    assert rows[MINT].status == xn.TIMEOUT and rows[MINT].narrative_score is None
    assert rows[OTHER].status == xn.MALFORMED and rows[OTHER].identity_confidence is None


async def test_rate_limit_pauses_the_provider_and_stores_nothing(redis, sf):
    await _enable(sf)
    calls = []

    def limited(request):
        calls.append(1)
        return httpx.Response(429, headers={"x-rate-limit-reset": str(int(NOW.timestamp()) + 600)})

    for m in (MINT, OTHER):
        await xn.enqueue(redis, m, "Moon Dog", "MDOG", {"qualified": True}, NOW.timestamp())
    async with client_for(limited) as c:
        out = await xn.run_pass(sf, redis, ON, c, NOW)
        again = await xn.run_pass(sf, redis, ON, c, NOW)
    assert out["stopped"] == xn.RATE_LIMITED and len(calls) == 1  # the second candidate is not requested
    assert again["status"] == xn.RATE_LIMITED and len(calls) == 1  # cooldown: no request at all
    async with sf() as s:
        assert (await s.execute(select(func.count()).select_from(XNarrativeObservation))).scalar_one() == 0


async def test_daily_budget_and_per_token_request_cap(redis, sf):
    await _enable(sf, max_daily_budget_usd="0.04")  # less than one 10-post query ($0.05)

    def handler(request):
        raise AssertionError("over budget: no request")

    await xn.enqueue(redis, MINT, "Moon Dog", "MDOG", {"qualified": True}, NOW.timestamp())
    async with client_for(handler) as c:
        out = await xn.run_pass(sf, redis, ON, c, NOW)
    assert out["stopped"] == xn.BUDGET_EXHAUSTED and await redis.get(xn.COOLDOWN) == xn.BUDGET_EXHAUSTED
    cfg = xn.XNarrativeSettings(enabled=True, max_requests_per_token=1, cache_ttl_seconds=60)
    await redis.delete(xn.COOLDOWN)
    async with client_for(lambda r: httpx.Response(200, json=body([]))) as c:
        first = await xn.lookup(redis, c, "t", cfg, OTHER, {}, NOW)
        await redis.delete(xn.CACHE.format(mint=OTHER))
        second = await xn.lookup(redis, c, "t", cfg, OTHER, {}, NOW)
    assert first["status"] == xn.NO_DATA and second["status"] == "SKIPPED"


async def test_features_are_read_as_of_the_decision_time_only(redis, sf):
    async with sf() as s:
        for minutes, score in ((-10, "10"), (5, "90")):  # one before the decision, one after it
            s.add(XNarrativeObservation(mint=MINT, observed_at=NOW + timedelta(minutes=minutes), status=xn.OK,
                                        narrative_score=Decimal(score), features={"distinct_authors": 3}))
        await s.commit()
        at_decision = await xn.features_at(s, MINT, NOW)
        before_any = await xn.features_at(s, MINT, NOW - timedelta(hours=1))
    assert at_decision["narrative_score"] == Decimal("10")  # the later observation is never visible (no look-ahead)
    assert before_any is None
