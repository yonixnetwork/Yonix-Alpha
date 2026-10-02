"""Token Explorer, explorer actions, unified wallet, manual EVM BUY and the
real-time PnL view (master §45, §54-61)."""

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from yonixalpha_core.chains.evm import manual as evm_manual
from yonixalpha_core.chains.evm import native_price
from yonixalpha_core.db.models import AuditLog, EvmToken, EvmTrade, PaperPosition, Token
from yonixalpha_core.db.redis import make_redis
from yonixalpha_core.config import get_settings
from yonixalpha_core.safety import store

pytestmark = pytest.mark.asyncio
NOW = datetime.now(timezone.utc).replace(microsecond=0)
MINT = "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"
TOK = "0x1111111111111111111111111111111111111111"
CREATOR = "0x3333333333333333333333333333333333333333"
TRADER = "0x2222222222222222222222222222222222222222"


async def _seed(app) -> None:
    async with app.state.db_session_factory() as s:
        s.add(Token(mint_address=MINT, symbol="MOON", name="Moon Sol", first_seen_source="test", first_seen_at=NOW,
                    creator_address="CreatorSo1111111111111111111111111111111111"))
        s.add(Token(mint_address="So11111111111111111111111111111111111111112", symbol="1abc", name="one abc",
                    first_seen_source="test", first_seen_at=NOW))
        s.add(EvmToken(chain="bsc", token=TOK, launchpad="fourmeme", name="Moon Bsc", symbol="MOONB", creator=CREATOR,
                       created_at=NOW, created_tx="0x" + "ab" * 32, category="FRESH", stage="CURVE", venue={},
                       stats={"buys": 5, "unique_buyers": 4, "buy_volume": "1.5", "window_s": 300}, last_trade_at=NOW,
                       state={"price": "0.00000002", "liquidity_quote": "3"},
                       extra={"decimals": 18, "total_supply": str(10 ** 27)}, safety_verdict="PASS",
                       safety={"findings": [{"level": "PASS", "code": "SELLABLE", "message": "round trip ok"}]}, safety_at=NOW))
        s.add(EvmToken(chain="bsc", token="0x4444444444444444444444444444444444444444", launchpad="fourmeme",
                       name="1%off", symbol="PCT", created_at=NOW, category="FRESH", stage="CURVE", venue={}, stats={}))
        s.add(EvmToken(chain="robinhood", token="0x5555555555555555555555555555555555555555", launchpad="genius_fun",
                       name="Genius", symbol="GEN", created_at=NOW, category="FRESH", stage="CURVE", venue={}, stats={}))
        s.add(EvmTrade(event_id="bsc:0xab:1", chain="bsc", launchpad="fourmeme", token=TOK, trader=TRADER, is_buy=True,
                       token_amount=Decimal(10 ** 21), quote_amount=Decimal(10 ** 16), at=NOW))
        await s.commit()


async def _usd(redis_symbol: str, price: str) -> None:
    r = make_redis(get_settings())
    await r.set(native_price.CACHE_KEY.format(symbol=redis_symbol),
                json.dumps({"price": price, "source": "test", "at": datetime.now(timezone.utc).isoformat()}))
    await r.aclose()


async def test_search_by_name_address_creator_and_wallet_with_chain_links(app, client, auth_headers):
    await _seed(app)
    r = (await client.get("/api/explorer/search?q=moon", headers=auth_headers)).json()
    assert {(x["chain"], x["symbol"]) for x in r["results"]} == {("solana", "MOON"), ("bsc", "MOONB")}
    for x in r["results"]:
        urls = " ".join(x["links"].values())
        if x["chain"] == "solana":
            assert "solscan.io/token/" + MINT in urls and "pump.fun/coin/" in urls and "bscscan" not in urls
        else:
            assert "bscscan.com/token/" + TOK in urls and "four.meme/token/" in urls and "solscan" not in urls
            assert x["links"]["token"] == f"/dashboard/explorer/bsc/{TOK}" and "bscscan.com/address/" + CREATOR in urls
    # LIKE wildcards in the query match literally
    r = (await client.get("/api/explorer/search?q=1%25", headers=auth_headers)).json()
    assert [x["symbol"] for x in r["results"]] == ["PCT"]
    # a chain filter
    r = (await client.get("/api/explorer/search?q=moon&chain=bsc", headers=auth_headers)).json()
    assert [x["chain"] for x in r["results"]] == ["bsc"]
    # contract address (any case), creator and a wallet that traded
    r = (await client.get(f"/api/explorer/search?q={TOK.upper().replace('0X', '0x')}", headers=auth_headers)).json()
    assert r["interpreted_as"] == "EVM address" and [x["match"] for x in r["results"]] == ["contract"]
    r = (await client.get(f"/api/explorer/search?q={CREATOR}", headers=auth_headers)).json()
    assert [(x["match"], x["symbol"]) for x in r["results"]] == [("creator", "MOONB")]
    r = (await client.get(f"/api/explorer/search?q={TRADER}", headers=auth_headers)).json()
    w = [x for x in r["results"] if x["kind"] == "wallet"]
    assert len(w) == 1 and w[0]["chain"] == "bsc" and w[0]["trades_14d"] == 1 and "bscscan.com/address/" in w[0]["links"]["wallet"]
    # a Solana mint only searches Solana
    r = (await client.get(f"/api/explorer/search?q={MINT}", headers=auth_headers)).json()
    assert r["interpreted_as"] == "Solana address" and {x["chain"] for x in r["results"]} == {"solana"}
    assert (await client.get("/api/explorer/search?q=m", headers=auth_headers)).status_code == 422
    assert (await client.get("/api/explorer/search?q=moon")).status_code == 401


async def test_evm_token_view_has_usd_market_cap_links_and_never_fakes_holders(app, client, auth_headers):
    await _seed(app)
    await _usd("BNB", "600")
    async with app.state.db_session_factory() as s:
        acct = await store.get_paper_account(s, "evm_bsc")
        s.add(PaperPosition(account_id=acct.id, engine="evm_bsc", symbol="MOONB", asset_id=TOK, provider="paper", side="LONG",
                            entry_price=Decimal("0.00000002"), quantity=Decimal(10 ** 6), initial_quantity=Decimal(10 ** 6),
                            remaining_quantity=Decimal(10 ** 6), entry_cost_quote=Decimal("0.0202"), take_profit=[],
                            status="open", entry_at=NOW - timedelta(minutes=2), last_price=Decimal("0.000000025"),
                            highest_price=Decimal("0.00000003"), last_marked_at=datetime.now(timezone.utc), plan={}))
        await s.commit()
    t = (await client.get(f"/api/explorer/token/bsc/{TOK}", headers=auth_headers)).json()
    m = t["market"]
    assert m["currency"] == "BNB" and m["market_cap_usd"] == "12000.00" and m["liquidity_usd"] == "1800.00"
    assert t["holders"]["count"] is None and t["holders"]["reason"]
    assert t["ml"]["status"] == "NOT_AVAILABLE" and t["buyers"]["retained_14d"] == 1
    assert t["launchpad"] == {"key": "fourmeme", "name": t["launchpad"]["name"], "observe_only": False}
    assert t["links"]["transaction"] == "https://bscscan.com/tx/0x" + "ab" * 32 and "solscan" not in json.dumps(t)
    p = t["positions"][0]["pnl"]
    assert (p["outcome"], p["tone"], p["net_pct"]) == ("PROFIT", "positive", "23.76")
    assert p["peak_pct"] == "50.00" and p["drawdown_pct"] == "-16.67"
    assert (await client.get(f"/api/explorer/token/solana/{MINT}", headers=auth_headers)).status_code == 422
    assert (await client.get("/api/explorer/token/bsc/0x" + "9" * 40, headers=auth_headers)).status_code == 404
    lk = (await client.get(f"/api/explorer/links/robinhood?token={TOK}&launchpad=pons_v2", headers=auth_headers)).json()
    assert lk["links"]["explorer"].startswith("https://robinhoodchain.blockscout.com/token/") and "dex" in lk["unavailable"]
    # the token list carries the USD market cap too
    tokens = (await client.get("/api/evm/tokens?chain=bsc", headers=auth_headers)).json()["tokens"]
    assert next(x for x in tokens if x["token"] == TOK)["market"]["market_cap_usd"] == "12000.00"


async def test_position_lists_show_profit_or_loss_never_a_bare_open(app, client, auth_headers):
    async with app.state.db_session_factory() as s:
        acct = await store.get_paper_account(s, "solana")
        base = dict(account_id=acct.id, engine="solana_fresh", provider="paper", side="LONG", entry_price=Decimal("0.001"),
                    quantity=Decimal(1000), initial_quantity=Decimal(1000), remaining_quantity=Decimal(1000),
                    entry_cost_quote=Decimal("1"), take_profit=[], status="open", entry_at=NOW, plan={})
        s.add(PaperPosition(symbol="LOSER", asset_id="L", last_price=Decimal("0.0008"), last_marked_at=datetime.now(timezone.utc), **base))
        s.add(PaperPosition(symbol="NOMARK", asset_id="N", **base))
        await s.commit()
    items = (await client.get("/api/paper/positions?status=open", headers=auth_headers)).json()["items"]
    by = {i["symbol"]: i["pnl"] for i in items}
    assert (by["LOSER"]["outcome"], by["LOSER"]["net_pct"], by["LOSER"]["tone"]) == ("LOSS", "-20.00", "negative")
    assert by["NOMARK"]["outcome"] == "PNL_UNAVAILABLE" and by["NOMARK"]["net"] is None
    s = (await client.get("/api/summary/memecoin", headers=auth_headers)).json()
    assert {p["symbol"]: p["pnl"]["outcome"] for p in s["positions"]} == {"LOSER": "LOSS", "NOMARK": "PNL_UNAVAILABLE"}


async def test_unified_wallet_overview_rows_never_fake_a_balance(app, client, auth_headers):
    await _usd("BNB", "600")
    r = (await client.get("/api/wallets/overview", headers=auth_headers)).json()
    assert r["wallet"] == "YonixAlpha Trading Wallet"
    rows = {(x["chain"], x["mode"]): x for x in r["rows"]}
    assert set(rows) == {(c, m) for c in ("solana", "bsc", "robinhood") for m in ("LIVE", "PAPER")}
    sol_live = rows[("solana", "LIVE")]
    assert sol_live["status"] == "UNAVAILABLE" and sol_live["total"] is None and sol_live["trading_balance"] is None
    assert rows[("bsc", "LIVE")]["status"] == "NOT_CONFIGURED" and rows[("bsc", "LIVE")]["total"] is None
    bsc = rows[("bsc", "PAPER")]
    assert bsc["currency"] == "BNB" and bsc["gas_reserve"] == "0.002" and bsc["usd_rate"] == "600"
    assert Decimal(bsc["trading_balance"]) == Decimal(bsc["available"]) - Decimal("0.002")
    assert rows[("robinhood", "PAPER")]["currency"] == "ETH" and rows[("robinhood", "PAPER")]["usd_rate"] is None
    # public addresses only: each account names its key variable, never a key value
    assert set(r["accounts"]["evm"]) == {"chains", "key", "address", "status"} and set(r["accounts"]["solana"]) == {"chains", "key", "address"}


async def test_manual_evm_buy_is_queued_paper_only_and_refuses_observe_only_venues(app, client, auth_headers):
    await _seed(app)
    p = (await client.get(f"/api/trade/evm/preview?chain=bsc&token={TOK}", headers=auth_headers)).json()
    assert p["execution_mode"] == "PAPER" and p["observe_only"] is False and p["currency"] == "BNB" and p["gas_reserve"] == "0.002"
    assert (await client.get("/api/trade/evm/preview?chain=bsc&token=0x" + "9" * 40, headers=auth_headers)).status_code == 404
    assert (await client.get("/api/trade/evm/preview?chain=solana&token=" + TOK, headers=auth_headers)).status_code == 422
    assert (await client.post("/api/trade/evm/buy", json={"chain": "bsc", "token": TOK}, headers=auth_headers)).status_code == 422
    r = await client.post("/api/trade/evm/buy", json={"chain": "bsc", "token": TOK, "confirm": True}, headers=auth_headers)
    assert r.status_code == 200 and r.json()["status"] == "QUEUED" and r.json()["mode"] == "PAPER"
    rid = r.json()["id"]
    redis = make_redis(get_settings())
    assert await evm_manual.next_request(redis, "bsc") == rid
    await redis.aclose()
    st = (await client.get(f"/api/trade/evm/requests/{rid}", headers=auth_headers)).json()
    assert st["requested_by"] == "admin" and st["history"][0]["status"] == "QUEUED"
    assert [x["id"] for x in (await client.get("/api/trade/evm/requests", headers=auth_headers)).json()] == [rid]
    bad = await client.post("/api/trade/evm/buy", json={"chain": "robinhood", "token": "0x" + "5" * 40, "confirm": True},
                            headers=auth_headers)
    assert bad.status_code == 422 and "observe only" in bad.json()["detail"]
    async with app.state.db_session_factory() as s:
        actions = (await s.execute(select(AuditLog.event_type))).scalars().all()
    assert actions.count("manual_trade.evm_buy_requested") == 1
