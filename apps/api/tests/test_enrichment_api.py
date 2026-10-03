"""External wallet intelligence endpoints (master §24-25) and the update
monitor's "what to do" guidance (§64-66)."""

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from yonixalpha_core.db.models import AuditLog, UpdateWatch, WalletEnrichment, WalletProfile

pytestmark = pytest.mark.asyncio
SOL = "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"
NOW = datetime.now(timezone.utc).replace(microsecond=0)


async def test_enrichment_is_off_by_default_and_settings_are_validated_and_audited(client, auth_headers):
    r = (await client.get("/api/wallets/enrichment", headers=auth_headers)).json()
    assert r["settings"]["enabled"] is False and r["settings"]["discovery"] is False and r["candidates"] == []
    assert r["providers"]["nansen"]["configured"] is False and r["providers"]["nansen"]["chains"] == ["solana", "bsc"]
    assert r["providers"]["madeonsol"]["chains"] == ["solana"] and r["providers"]["madeonsol"]["calls_today"] == 0
    assert "never" in r["note"] and "nk-" not in str(r)
    bad = await client.put("/api/wallets/enrichment-settings", json={"refresh_hours": 0}, headers=auth_headers)
    assert bad.status_code == 422
    ok = await client.put("/api/wallets/enrichment-settings", json={"enabled": True, "nansen_daily_calls": 50}, headers=auth_headers)
    assert ok.status_code == 200 and ok.json()["settings"]["nansen_daily_calls"] == 50
    r = (await client.get("/api/wallets/enrichment", headers=auth_headers)).json()
    assert r["settings"]["enabled"] is True and r["providers"]["nansen"]["daily_budget"] == 50
    assert (await client.get("/api/wallets/enrichment")).status_code == 401


async def test_profiles_carry_provider_records_and_candidates_are_listed(app, client, auth_headers):
    async with app.state.db_session_factory() as s:
        s.add(WalletProfile(chain="solana", wallet=SOL, metrics={}, labels=[], source="test", trades=4, tokens=2, last_seen=NOW))
        s.add(WalletEnrichment(chain="solana", wallet=SOL, provider="madeonsol", kind="PROFILE", status="OK",
                               data={"labels": ["KOL"], "name": "Alice", "pnl": {"realized_sol": 3.5}, "pnl_currency": "SOL"},
                               fetched_at=NOW))
        s.add(WalletEnrichment(chain="solana", wallet="K2" + "1" * 40, provider="madeonsol", kind="CANDIDATE", status="OK",
                               data={"label": "Bob", "source": "madeonsol KOL leaderboard (30d)"}, discovered_at=NOW))
        await s.commit()
    p = (await client.get("/api/wallets/profiles?chain=solana", headers=auth_headers)).json()["profiles"][0]
    ext = p["external"]["madeonsol"]
    assert ext["name"] == "Alice" and ext["labels"] == ["KOL"] and ext["pnl"] == {"realized_sol": 3.5} and "not verified" in ext["note"]
    c = (await client.get("/api/wallets/enrichment", headers=auth_headers)).json()["candidates"]
    assert len(c) == 1 and c[0]["labels"] == ["Bob"] and c[0]["own_history"] is None


async def test_manual_lookup_without_keys_calls_nothing_and_is_audited(app, client, auth_headers):
    r = await client.post(f"/api/wallets/enrichment/solana/{SOL}/refresh", headers=auth_headers)
    assert r.status_code == 200 and r.json()["results"] == {"nansen": "NOT_CONFIGURED", "madeonsol": "NOT_CONFIGURED"}
    r = await client.post("/api/wallets/enrichment/robinhood/0x" + "b" * 40 + "/refresh", headers=auth_headers)
    assert r.json()["results"] == {"nansen": "UNSUPPORTED_CHAIN", "madeonsol": "UNSUPPORTED_CHAIN"}
    assert (await client.post("/api/wallets/enrichment/ethereum/abc/refresh", headers=auth_headers)).status_code == 422
    async with app.state.db_session_factory() as s:
        assert (await s.execute(select(WalletEnrichment))).scalars().all() == []
        events = (await s.execute(select(AuditLog.event_type))).scalars().all()
    assert events.count("wallet_enrichment.refresh") == 2


async def test_update_panel_says_what_to_do_with_each_update(app, client, auth_headers):
    async with app.state.db_session_factory() as s:
        s.add(UpdateWatch(key="pypi:websockets", category="dependency", installed_version="14.1", latest_release="17.1",
                          last_checked=NOW))
        s.add(UpdateWatch(key="pypi:httpx", category="dependency", installed_version="0.28.1", latest_release="0.28.1",
                          last_checked=NOW))
        await s.commit()
    r = (await client.get("/api/system/updates", headers=auth_headers)).json()
    w = {x["key"]: x["how_to_apply"]["action"] for x in r["watches"]}
    assert w["pypi:websockets"] == "PIN_BUMP" and w["pypi:httpx"] == "APPLIED"
    assert w["github:anza-xyz/agave"] == "REVIEW_ONLY"
    assert any("DEPLOY_PULL=1" in t for t in r["routine"])
