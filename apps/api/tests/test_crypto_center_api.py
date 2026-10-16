"""Strategy Center, backtests, comparison, portfolio and alerts API: login
required, honest empty states, validation, and results kept apart by mode."""

import os
import uuid
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yonixalpha_crypto.db import BacktestBatch, BacktestRun, Event

from tests.test_crypto_api import crypto  # noqa: F401  (fixture)

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
PATHS = ["/api/crypto/center/strategies", "/api/crypto/center/strategies/sma_cross", "/api/crypto/center/rejected",
         "/api/crypto/backtests", "/api/crypto/portfolio", "/api/crypto/alerts", "/api/crypto/audit"]


async def test_every_endpoint_requires_login(client):
    for p in PATHS:
        assert (await client.get(p)).status_code == 401, p
    assert (await client.post("/api/crypto/backtests", json={})).status_code == 401
    assert (await client.put("/api/crypto/portfolio", json={"expected_version": 1})).status_code == 401


async def test_center_lists_twenty_modules_with_honest_status(client, auth_headers, crypto):  # noqa: F811
    body = (await client.get("/api/crypto/center/strategies", headers=auth_headers)).json()
    lib = [x for x in body["strategies"] if x["library"]]
    assert len(lib) == 20 and body["live_orders_unlocked"] is False
    assert all(x["status"] == "backtest pending" and x["backtest"] is None for x in lib)  # nothing has run
    assert {x["status"] for x in body["strategies"] if not x["library"]} <= {"blocked", "research"}
    assert "win rate is never used" in body["ranking_rule"]
    d = (await client.get("/api/crypto/center/strategies/trend_pullback", headers=auth_headers)).json()
    assert d["entry_rules"] and d["research"]["sources"] and d["params_schema"]
    assert d["performance"]["backtest"] is None and d["performance"]["live"]["status"] == "locked"
    assert d["performance"]["paper"]["closed_trades"] == 0 and d["performance"]["paper"]["win_rate"] is None
    assert d["default_exits"]["time_stop_bars"] == 48
    assert (await client.get("/api/crypto/center/strategies/nope", headers=auth_headers)).status_code == 404
    rej = (await client.get("/api/crypto/center/rejected", headers=auth_headers)).json()
    assert any(x["id"] == "volume_momentum" and x["status"] == "MERGED" for x in rej)


async def test_backtest_batches_are_validated_queued_and_cancellable(client, auth_headers, crypto):  # noqa: F811
    base = {"start": "2025-01-01T00:00:00Z", "end": "2025-06-01T00:00:00Z", "symbols": ["BTC/USDT", "ETH/USDT"]}
    bad = await client.post("/api/crypto/backtests", headers=auth_headers, json={**base, "costs": {"taker_fee_bps": 500}})
    assert bad.status_code == 422
    bad = await client.post("/api/crypto/backtests", headers=auth_headers, json={**base, "strategies": ["nope"]})
    assert bad.status_code == 422
    r = await client.post("/api/crypto/backtests", headers=auth_headers, json={**base, "costs": {"taker_fee_bps": 10}})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["runs"] == 39 and out["backtests_enabled"] is False  # 19 single-symbol x 2 + meta_muse once
    listing = (await client.get("/api/crypto/backtests", headers=auth_headers)).json()
    assert listing["batches"][0]["counts"] == {"QUEUED": 39}
    bid = out["batch_id"]
    det = (await client.get(f"/api/crypto/backtests/{bid}", headers=auth_headers)).json()
    assert len(det["runs"]) == 39
    assert (await client.post(f"/api/crypto/backtests/{bid}/cancel", headers=auth_headers)).json()["cancelled_runs"] == 39
    audit = (await client.get("/api/crypto/audit", headers=auth_headers)).json()
    assert any(a["scope"] == "backtest" for a in audit)


async def seed_runs():
    eng = create_async_engine(os.environ["DATABASE_URL"])
    sf = async_sessionmaker(eng, expire_on_commit=False)
    curve = {"t": [f"2025-0{m}-{d:02d}T00:00:00+00:00" for m in (3, 4) for d in range(1, 29)],
             "equity": [10000 + k * 3 for k in range(56)], "drawdown_pct": [0.0] * 56}
    async with sf() as s:
        b = BacktestBatch(status="DONE", config={}, total=2, created_at=NOW)
        s.add(b)
        await s.flush()
        ids = []
        for sid, tier in (("sma_cross", "candidate"), ("donchian_breakout", "failed")):
            r = BacktestRun(batch_id=b.id, strategy_id=sid, exchange="binance_us", symbol="BTC/USDT", interval="1h",
                            status="DONE", config={"costs_effective": {"taker_fee_bps": 2}}, code_version="src-x",
                            created_at=NOW, finished_at=NOW, data={"bars": 100},
                            summary={"tier": tier, "rank_key": [0 if tier == "candidate" else 3, -0.2, 5], "oos_trades": 40},
                            result={"oos_curve": curve, "splits": {"out_of_sample": {"from": "2025-03-01"}},
                                    "default_params": {"metrics": {"out_of_sample": {"trades": 40}}},
                                    "verdict": {"tier": tier}, "oos_trades_sample": [
                                        {"entry_time": "2025-03-02T00:00:00+00:00", "exit_time": "2025-03-05T00:00:00+00:00"}]})
            s.add(r)
            await s.flush()
            ids.append(str(r.id))
        s.add(Event(at=NOW, level="ERROR", source="risk", message="drawdown shutdown: test"))
        await s.commit()
    await eng.dispose()
    return b, ids


async def test_status_ranking_and_comparison_use_stored_results(client, auth_headers, crypto):  # noqa: F811
    b, ids = await seed_runs()
    body = (await client.get("/api/crypto/center/strategies", headers=auth_headers)).json()
    st = {x["id"]: x["status"] for x in body["strategies"]}
    assert st["sma_cross"] == "approved for consideration" and st["donchian_breakout"] == "backtest failed"
    det = (await client.get(f"/api/crypto/backtests/{b.id}", headers=auth_headers)).json()
    assert [r["strategy_id"] for r in det["runs"]] == ["sma_cross", "donchian_breakout"]  # documented ranking
    cmp = (await client.get(f"/api/crypto/compare?runs={','.join(ids)}", headers=auth_headers)).json()
    assert len(cmp["items"]) == 2 and cmp["correlation"][ids[0]][ids[1]] is not None
    assert cmp["overlap"][ids[0]][ids[1]] == 1.0 and cmp["same_period_and_costs"] is True
    full = (await client.get(f"/api/crypto/backtests/runs/{ids[0]}", headers=auth_headers)).json()
    assert full["code_version"] == "src-x" and full["data"]["bars"] == 100
    assert (await client.get(f"/api/crypto/backtests/runs/{uuid.uuid4()}", headers=auth_headers)).status_code == 404
    al = (await client.get("/api/crypto/alerts?kind=risk", headers=auth_headers)).json()
    assert al[0]["kind"] == "risk" and "drawdown" in al[0]["message"]


async def test_portfolio_limits_round_trip_with_versions(client, auth_headers, crypto):  # noqa: F811
    p = (await client.get("/api/crypto/portfolio", headers=auth_headers)).json()
    assert p["limits"]["version"] == 1 and p["state"]["open_positions"] == 0
    r = await client.put("/api/crypto/portfolio", headers=auth_headers,
                         json={"expected_version": 1, "max_portfolio_risk_quote": "30", "backtests_enabled": True})
    assert r.status_code == 200 and r.json()["version"] == 2
    assert (await client.put("/api/crypto/portfolio", headers=auth_headers,
                             json={"expected_version": 1, "max_portfolio_risk_quote": "31"})).status_code == 409
    assert (await client.put("/api/crypto/portfolio", headers=auth_headers,
                             json={"expected_version": 2, "max_portfolio_risk_quote": "-1"})).status_code == 422


async def test_strategy_save_supports_risk_modes_exits_and_refuses_live(client, auth_headers, crypto):  # noqa: F811
    d = (await client.get("/api/crypto/center/strategies/ema_cross", headers=auth_headers)).json()
    v = d["config"]["version"]
    r = await client.put("/api/crypto/strategies/ema_cross", headers=auth_headers,
                         json={"expected_version": v, "risk": {"risk_pct": "0.5"},
                               "exits": {"trailing": {"method": "atr", "activate_r": 1, "atr_mult": 2.5}}})
    assert r.status_code == 200, r.text
    d = (await client.get("/api/crypto/center/strategies/ema_cross", headers=auth_headers)).json()
    assert d["risk_view"]["per_trade"] == "0.5% of equity" and d["config"]["exits"]["trailing"]["atr_mult"] == "2.5"
    r = await client.put("/api/crypto/strategies/ema_cross", headers=auth_headers,
                         json={"expected_version": d["config"]["version"], "mode": "LIVE"})
    assert r.status_code == 422 and "no live mode" in r.text
