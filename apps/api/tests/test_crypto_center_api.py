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
    assert {x["status"] for x in body["strategies"] if not x["library"]} <= {"rejected / disabled"}
    assert "win rate is never used" in body["ranking_rule"]
    d = (await client.get("/api/crypto/center/strategies/trend_pullback", headers=auth_headers)).json()
    assert d["entry_rules"] and d["research"]["sources"] and d["params_schema"]
    assert d["performance"]["backtest"] is None and d["performance"]["live"]["status"] == "locked"
    assert d["performance"]["paper"]["closed_trades"] == 0 and d["performance"]["paper"]["win_rate"] is None
    assert d["default_exits"]["time_stop_bars"] == 48
    assert (await client.get("/api/crypto/center/strategies/nope", headers=auth_headers)).status_code == 404
    rej = (await client.get("/api/crypto/center/rejected", headers=auth_headers)).json()
    assert any(x["id"] == "volume_momentum" and x["status"] == "MERGED" for x in rej)


async def test_server_backtests_are_refused_by_policy(client, auth_headers, crypto):  # noqa: F811
    # Historical backtests run locally on downloaded candles, never on the server (backtest.policy).
    base = {"start": "2025-01-01T00:00:00Z", "end": "2025-06-01T00:00:00Z", "symbols": ["BTC/USDT", "ETH/USDT"]}
    r = await client.post("/api/crypto/backtests", headers=auth_headers, json=base)
    assert r.status_code == 409 and "run locally" in r.json()["detail"]
    listing = (await client.get("/api/crypto/backtests", headers=auth_headers)).json()
    assert listing["batches"] == [] and listing["server_backtests_enabled"] is False
    assert listing["result_import_enabled"] is False and "run locally" in listing["policy"]
    p = (await client.get("/api/crypto/portfolio", headers=auth_headers)).json()
    r = await client.put("/api/crypto/portfolio", headers=auth_headers,
                         json={"expected_version": p["limits"]["version"], "backtests_enabled": True})
    assert r.status_code == 422 and "run locally" in r.text


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
    assert st["sma_cross"] == "backtest completed" and st["donchian_breakout"] == "backtest completed"
    by = {x["id"]: x for x in body["strategies"]}
    assert by["sma_cross"]["approved_for_consideration"] is True and by["donchian_breakout"]["approved_for_consideration"] is False
    assert "verdict failed" in by["donchian_breakout"]["why"]  # a poor result is a completed run, not a failed one
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
                         json={"expected_version": 1, "max_portfolio_risk_quote": "30", "backtests_enabled": False})
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


async def _runs(rows):
    """rows: (strategy, symbol, status, tier or None, rank_key or None) in ONE batch."""
    eng = create_async_engine(os.environ["DATABASE_URL"])
    sf = async_sessionmaker(eng, expire_on_commit=False)
    async with sf() as s:
        b = BacktestBatch(status="RUNNING", config={}, total=len(rows), created_at=NOW)
        s.add(b)
        await s.flush()
        for sid, sym, status, tier, key in rows:
            s.add(BacktestRun(batch_id=b.id, strategy_id=sid, exchange="binance_us", symbol=sym, interval="1h",
                              status=status, config={}, created_at=NOW,
                              finished_at=NOW if status in ("DONE", "FAILED") else None,
                              summary={"tier": tier, "rank_key": key, "oos_trades": 40} if tier else None,
                              error="boom" if status == "FAILED" else None))
        await s.commit()
    await eng.dispose()


async def test_center_verdict_is_the_worst_symbol_of_the_latest_batch(client, auth_headers, crypto):  # noqa: F811
    await _runs([("sma_cross", "BTC/USDT", "DONE", "candidate", [0, -0.3, 5]),
                 ("sma_cross", "ETH/USDT", "DONE", "failed", [3, 0.1, 9]),
                 ("ema_cross", "BTC/USDT", "DONE", "candidate", [0, -0.3, 5]),
                 ("ema_cross", "ETH/USDT", "DONE", "candidate", [0, -0.1, 7]),
                 ("ema_cross", "SOL/USDT", "QUEUED", None, None),
                 ("adx_trend", "BTC/USDT", "DONE", "candidate", [0, -0.3, 5]),
                 ("adx_trend", "ETH/USDT", "DONE", "candidate", [0, -0.2, 6])])
    body = (await client.get("/api/crypto/center/strategies", headers=auth_headers)).json()
    by = {x["id"]: x for x in body["strategies"]}
    sma = by["sma_cross"]
    assert sma["status"] == "backtest completed" and "verdict failed" in sma["why"] and "worst of 2" in sma["why"]  # one good symbol does not hide a bad one
    assert sma["backtest"]["symbol"] == "ETH/USDT"
    assert sma["backtest"]["aggregate"]["tiers"] == {"candidate": 1, "failed": 1}
    ema = by["ema_cross"]
    assert ema["status"] == "backtest running" and not ema["approved_for_consideration"]  # not approved while a symbol is pending
    assert ema["backtest"]["symbol"] == "ETH/USDT"  # the lower expectancy of the two candidates
    adx = by["adx_trend"]
    assert adx["status"] == "backtest completed" and adx["approved_for_consideration"] is True
    assert adx["backtest"]["aggregate"]["finished"] == 2
    det = (await client.get("/api/crypto/center/strategies/sma_cross", headers=auth_headers)).json()
    assert det["status"] == "backtest completed" and det["performance"]["backtest"] is not None
    assert det["performance"]["backtest"]["symbol"] == "ETH/USDT"


async def test_a_queue_batch_can_use_each_strategys_saved_parameters(client, auth_headers, crypto):  # noqa: F811
    # The queue itself is server-disabled; its batch builder is still checked
    # because it shares the run configuration format with the local runner.
    from datetime import timedelta

    from sqlalchemy import select

    from yonixalpha_crypto import config_store
    from yonixalpha_crypto.backtest import jobs
    from yonixalpha_crypto.db import StrategyConfig

    await client.get("/api/crypto/center/strategies", headers=auth_headers)  # creates the default settings rows
    eng = create_async_engine(os.environ["DATABASE_URL"])
    sf = async_sessionmaker(eng, expire_on_commit=False)
    kw = dict(symbols=["BTC/USDT"], interval="1h", exchange="binance_us", start=NOW, end=NOW + timedelta(days=30),
              costs={}, risk={}, optimize=False, actor="t", strategies=["sma_cross", "ema_cross"])
    async with sf() as s:
        v = (await s.execute(select(StrategyConfig.version).where(StrategyConfig.strategy_id == "sma_cross"))).scalar_one()
        await config_store.save_strategy(s, "sma_cross", {"params": {"fast": 10, "slow": 30}}, v, "t")
        try:
            await jobs.create_batch(s, **kw, param_source="tuned")
            raise AssertionError("expected BatchError")
        except jobs.BatchError:
            pass
        saved = await jobs.create_batch(s, **kw, param_source="saved")
        default = await jobs.create_batch(s, **kw)
        await s.commit()
        runs = {(r.batch_id, r.strategy_id): r.config for r in (await s.execute(select(BacktestRun))).scalars()}
    await eng.dispose()
    sc = runs[(saved.id, "sma_cross")]
    assert sc["param_source"] == "saved" and sc["params"]["fast"] in (10, "10") and sc["settings_version"] == v + 1
    dc = runs[(default.id, "sma_cross")]
    assert dc["param_source"] == "defaults" and dc["params"] == {}


async def test_center_shows_paper_active_exchange_demo_unavailable_and_paper_balances(client, auth_headers, crypto):  # noqa: F811
    from datetime import timedelta
    from decimal import Decimal

    from sqlalchemy import select

    from yonixalpha_crypto import config_store
    from yonixalpha_crypto.db import ApiConnection, DemoAccount, DemoPosition, StrategyConfig

    await client.get("/api/crypto/center/strategies", headers=auth_headers)
    eng = create_async_engine(os.environ["DATABASE_URL"])
    sf = async_sessionmaker(eng, expire_on_commit=False)
    async with sf() as s:
        conn = ApiConnection(id=uuid.uuid4(), exchange="binance", environment="DEMO", label="d", key_enc="x",
                             secret_enc="x", key_fingerprint=uuid.uuid4().hex, key_hint="…1", enabled=True,
                             status="BLOCKED", created_at=NOW, updated_at=NOW,
                             last_error="binance refused requests from this server's location (HTTP 451)")
        s.add(conn)
        s.add(DemoAccount(exchange="binance_us", quote="USDT", starting_balance=Decimal(1000), cash=Decimal(900)))
        s.add(DemoPosition(strategy_id="ema_cross", exchange="binance_us", symbol="BTC/USDT", qty=1, entry_price=100,
                           entry_cost_quote=100, status="OPEN", opened_at=NOW))
        s.add(DemoPosition(strategy_id="ema_cross", exchange="binance_us", symbol="BTC/USDT", qty=1, entry_price=100,
                           entry_cost_quote=100, status="CLOSED", realized_pnl_quote=Decimal("-2.5"),
                           fees_quote=Decimal("0.2"), opened_at=NOW - timedelta(hours=2), closed_at=NOW))
        await s.commit()
        v = lambda sid: s.scalar(select(StrategyConfig.version).where(StrategyConfig.strategy_id == sid))  # noqa: E731
        await config_store.save_strategy(s, "ema_cross", {"enabled": True, "mode": "DEMO"}, await v("ema_cross"), "t")
        await config_store.save_strategy(s, "sma_cross", {"enabled": False, "mode": "EXCHANGE_DEMO", "exchange": "binance",
                                                          "connection_id": str(conn.id)}, await v("sma_cross"), "t")
        cfg = (await s.execute(select(StrategyConfig).where(StrategyConfig.strategy_id == "sma_cross"))).scalar_one()
        cfg.enabled = True  # enabled on a connection that has since been blocked
        await s.commit()
    await eng.dispose()
    body = (await client.get("/api/crypto/center/strategies", headers=auth_headers)).json()
    by = {x["id"]: x for x in body["strategies"]}
    assert len(body["strategies"]) >= 20 and sum(1 for x in body["strategies"] if x["library"]) == 20
    assert by["ema_cross"]["status"] == "paper trading active"
    assert by["ema_cross"]["paper"] == {"open_positions": 1, "open_cost_quote": "100.000000000000000000",
                                        "closed_trades": 1, "realized_pnl_quote": "-2.500000000000000000",
                                        "fees_quote": "0.200000000000000000"}
    assert by["sma_cross"]["status"] == "exchange demo unavailable" and "HTTP 451" in by["sma_cross"]["why"]
    assert body["paper_accounts"][0]["exchange"] == "binance_us" and body["paper_accounts"][0]["cash"].startswith("900")
