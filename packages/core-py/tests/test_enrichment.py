"""Nansen / MadeOnSol wallet enrichment (master §24-25): request shapes,
error mapping, the budget, and the passes (off by default, never a signal)."""

import json
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://yonixalpha:yonixalpha_test_pw@localhost:5432/yonixalpha_test")

import httpx  # noqa: E402
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from yonixalpha_core import enrichment as en  # noqa: E402
from yonixalpha_core.db import models  # noqa: F401,E402
from yonixalpha_core.db.base import Base, make_session_factory  # noqa: E402
from yonixalpha_core.db.models import CopyTarget, PlatformSetting, WalletEnrichment, WalletProfile  # noqa: E402

pytestmark = pytest.mark.asyncio
NOW = datetime(2026, 10, 3, 12, tzinfo=timezone.utc)
SOL_WALLET = "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"
BSC_WALLET = "0x" + "a" * 40
KEYS = SimpleNamespace(NANSEN_API_KEY="nk-test", MADEONSOL_API_KEY="mk-test")


class FakeRedis:
    def __init__(self):
        self.d = {}

    async def get(self, k):
        return self.d.get(k)

    async def set(self, k, v, ex=None):
        self.d[k] = v

    async def incrby(self, k, n):
        self.d[k] = int(self.d.get(k, 0)) + n
        return self.d[k]

    async def expire(self, k, s):
        return True


def provider(seen: list, status: dict | None = None):
    status = status or {}

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content) if req.content else None
        seen.append((req.method, req.url.path, dict(req.headers), body))
        path = req.url.path
        if path in status:
            return httpx.Response(status[path])
        if path == "/api/v1/profiler/address/labels":
            return httpx.Response(200, json={"data": [{"label": "Smart Trader"}, {"label": "Fund"}]})
        if path == "/api/v1/profiler/address/pnl-summary":
            return httpx.Response(200, json={"total_pnl": 25000, "win_rate": 0.65, "nested": {"x": 1}})
        if path == "/api/v1/smart-money/dex-trades":
            return httpx.Response(200, json={"data": [{"trader_address": "W1", "trader_address_label": "Smart Trader"},
                                                      {"trader_address": "W1"}, {"trader_address": "W2"}]})
        if path == "/api/v1/account":
            return httpx.Response(200, json={"plan": "pro", "credits": 100})
        if path.endswith("/pnl") and path.startswith("/api/v1/wallet/"):
            return httpx.Response(200, json={"address": SOL_WALLET, "window_days": 30,
                                             "summary": {"realized_sol": 12.5, "wins": 7, "losses": 3, "win_rate": 0.7,
                                                         "profit_factor": 2.1, "best_realized": {"token_mint": "M"}},
                                             "notes": {"cost_basis_observable_from": "2026-07-01"}})
        if path.startswith("/api/v1/kol/leaderboard"):
            return httpx.Response(200, json={"leaderboard": [{"wallet": "K1", "name": "Alice", "pnl": 99.0},
                                                             {"wallet": "K2", "name": None, "pnl": 5.0}], "period": "30d"})
        if path.startswith("/api/v1/kol/"):
            return httpx.Response(200, json={"wallet": SOL_WALLET, "kol_name": "Alice", "win_rate": 61.0})
        if path == "/api/v1/me":
            return httpx.Response(200, json={"tier": "pro", "tier_label": "Pro", "quota": {"daily": {"remaining": 990, "limit": 1000}}})
        return httpx.Response(404)
    return handler


def client(seen, status=None):
    return httpx.AsyncClient(transport=httpx.MockTransport(provider(seen, status)))


async def test_nansen_wallet_sends_the_api_key_header_and_maps_bsc_to_bnb():
    seen = []
    async with client(seen) as c:
        data, calls = await en.nansen_wallet(c, "nk", "bsc", BSC_WALLET, NOW)
    assert calls == 2 and data["labels"] == ["Fund", "Smart Trader"]
    assert data["pnl"] == {"total_pnl": 25000, "win_rate": 0.65}  # scalars only
    for _, path, headers, body in seen:
        assert headers["apikey"] == "nk" and body["chain"] == "bnb" and body["address"] == BSC_WALLET
    assert seen[1][3]["date"] == {"from": "2026-09-03", "to": "2026-10-03"}
    with pytest.raises(en.ProviderError) as e:
        async with client([]) as c:
            await en.nansen_wallet(c, "nk", "robinhood", BSC_WALLET, NOW)
    assert e.value.status == en.UNSUPPORTED


async def test_madeonsol_wallet_uses_bearer_auth_and_reads_the_kol_profile():
    seen = []
    async with client(seen) as c:
        data, calls = await en.madeonsol_wallet(c, "mk", "solana", SOL_WALLET, NOW)
    assert calls == 2 and all(h["authorization"] == "Bearer mk" for _, _, h, _ in seen)
    assert data["name"] == "Alice" and data["labels"] == ["KOL"] and data["pnl_currency"] == "SOL"
    assert data["pnl"]["realized_sol"] == 12.5 and "best_realized" not in data["pnl"]
    assert data["cost_basis_from"] == "2026-07-01"
    async with client([], {f"/api/v1/kol/{SOL_WALLET}": 404}) as c:  # not a tracked KOL: no label, no name
        data, _ = await en.madeonsol_wallet(c, "mk", "solana", SOL_WALLET, NOW)
    assert data["labels"] == [] and data["name"] is None
    with pytest.raises(en.ProviderError) as e:
        async with client([]) as c:
            await en.madeonsol_wallet(c, "mk", "bsc", BSC_WALLET, NOW)
    assert e.value.status == en.UNSUPPORTED


@pytest.mark.parametrize("code,status", [(401, en.UNAUTHORIZED), (403, en.UNAUTHORIZED), (402, en.PAYMENT_REQUIRED),
                                         (429, en.RATE_LIMITED), (500, en.UNAVAILABLE)])
async def test_provider_errors_are_classified(code, status):
    async with client([], {f"/api/v1/wallet/{SOL_WALLET}/pnl": code}) as c:
        with pytest.raises(en.ProviderError) as e:
            await en.madeonsol_wallet(c, "mk", "solana", SOL_WALLET, NOW)
    assert e.value.status == status and "mk" not in e.value.detail


async def test_candidates_and_connection_tests():
    async with client([]) as c:
        assert [x["wallet"] for x in await en.nansen_candidates(c, "nk", "solana", 5)] == ["W1", "W2"]
        assert [x["label"] for x in await en.madeonsol_candidates(c, "mk", "solana", 5)] == ["Alice", None]
        assert "990 of 1000" in await en.test_connection(c, en.MADEONSOL, "mk")
        assert "Nansen account reachable" in await en.test_connection(c, en.NANSEN, "nk")
        with pytest.raises(en.ProviderError) as e:
            await en.test_connection(c, en.NANSEN, None)
        assert e.value.status == en.NOT_CONFIGURED


async def test_settings_are_validated_and_off_by_default():
    d = en.EnrichmentSettings.parse(None)
    assert d.enabled is False and d.discovery is False and d.nansen_daily_calls == 100
    assert en.EnrichmentSettings.parse({"enabled": True, "refresh_hours": 6}).refresh_hours == 6
    for bad in ({"enabled": "yes"}, {"refresh_hours": 0}, {"nansen_daily_calls": 1.5}, {"wallets_per_pass": True}):
        with pytest.raises(ValueError):
            en.EnrichmentSettings.parse(bad)


@pytest_asyncio.fixture
async def sf():
    engine = create_async_engine(os.environ["DATABASE_URL"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield make_session_factory(engine)
    await engine.dispose()


async def _seed(sf, settings: dict | None):
    async with sf() as s:
        if settings is not None:
            s.add(PlatformSetting(key=en.SETTINGS_KEY, value=settings))
        s.add(CopyTarget(chain="bsc", wallet=BSC_WALLET, mode="NOTIFY"))
        s.add(WalletProfile(chain="solana", wallet=SOL_WALLET, metrics={"discovery": {"stage": "VALIDATED"}}, labels=[],
                            source="test", trades=10, tokens=3))
        s.add(WalletProfile(chain="robinhood", wallet="0x" + "b" * 40, metrics={"discovery": {"stage": "VALIDATED"}},
                            labels=[], source="test", trades=10, tokens=3))
        await s.commit()


async def test_nothing_is_called_while_enrichment_is_off(sf):
    await _seed(sf, None)
    seen = []
    async with client(seen) as c:
        out = await en.enrich_pass(sf, FakeRedis(), KEYS, c, NOW)
        disc = await en.discovery_pass(sf, FakeRedis(), KEYS, c, NOW)
    assert out == {"status": "OFF"} and disc == {"status": "OFF"} and seen == []


async def test_enrich_pass_stores_records_once_per_window_and_respects_the_budget(sf):
    await _seed(sf, {"enabled": True, "nansen_daily_calls": 3, "madeonsol_daily_calls": 100})
    redis, seen = FakeRedis(), []
    async with client(seen) as c:
        out = await en.enrich_pass(sf, redis, KEYS, c, NOW)
        # Nansen: BSC copy target (2 calls), then the Solana wallet would need 2 more > budget 3
        assert out["nansen"] == {"looked_up": 1, "errors": 0, "stopped": en.BUDGET_EXHAUSTED}
        assert out["madeonsol"] == {"looked_up": 1, "errors": 0}  # Solana only; the Robinhood wallet is skipped
        assert await en.calls_today(redis, en.NANSEN, NOW) == 2 and await en.calls_today(redis, en.MADEONSOL, NOW) == 2
        n = len(seen)
        again = await en.enrich_pass(sf, redis, KEYS, c, NOW + timedelta(hours=1))  # inside the refresh window
        assert len(seen) == n and again["madeonsol"] == {"looked_up": 0, "errors": 0}
    async with sf() as s:
        rec = await s.get(WalletEnrichment, ("solana", SOL_WALLET, "madeonsol"))
        assert rec.status == en.OK and rec.data["name"] == "Alice" and rec.data["note"] == en.NOTE
        assert await s.get(WalletEnrichment, ("robinhood", "0x" + "b" * 40, "nansen")) is None


async def test_a_refusing_provider_is_recorded_and_not_called_again_this_pass(sf):
    await _seed(sf, {"enabled": True})
    seen = []
    async with client(seen, {"/api/v1/profiler/address/labels": 402}) as c:
        out = await en.enrich_pass(sf, FakeRedis(), SimpleNamespace(NANSEN_API_KEY="nk", MADEONSOL_API_KEY=None), c, NOW)
    assert out["nansen"]["stopped"] == en.PAYMENT_REQUIRED and out["madeonsol"] == en.NOT_CONFIGURED
    assert len([x for x in seen if "labels" in x[1]]) == 1
    async with sf() as s:
        rec = await s.get(WalletEnrichment, ("bsc", BSC_WALLET, "nansen"))
        assert rec.status == en.PAYMENT_REQUIRED and "402" in rec.error and rec.data is None


async def test_discovery_stores_candidates_once_a_day_and_never_copies(sf):
    await _seed(sf, {"enabled": True, "discovery": True, "discovery_limit": 5})
    redis = FakeRedis()
    async with client([]) as c:
        out = await en.discovery_pass(sf, redis, KEYS, c, NOW)
        assert out["madeonsol:solana"] == {"found": 2, "new": 2} and out["nansen:bsc"] == {"found": 2, "new": 2}
        assert await en.discovery_pass(sf, redis, KEYS, c, NOW) == {}  # once a day per provider and chain
    async with sf() as s:
        k1 = await s.get(WalletEnrichment, ("solana", "K1", "madeonsol"))
        assert k1.kind == "CANDIDATE" and k1.data["label"] == "Alice" and k1.discovered_at == NOW
        from sqlalchemy import func, select
        assert (await s.execute(select(func.count()).select_from(CopyTarget))).scalar_one() == 1  # nothing copied
