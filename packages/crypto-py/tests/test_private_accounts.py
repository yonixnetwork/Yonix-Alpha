"""Exchange accounts: encrypted keys, fixed hosts per environment, the
signing allowlist, least-privilege assessment and the live-order lock.
Every exchange answer here is a MockTransport; nothing reaches a network."""

import hashlib
import hmac
import json
from datetime import timedelta
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest
from sqlalchemy import select, text

from yonixalpha_crypto.db import ApiConnection, Event
from yonixalpha_crypto.private import connections, environments, livelock, secretstore
from yonixalpha_crypto.private.signedhttp import BlockedRequest, ExchangeRequestError, SignedHttp

from tests.conftest import NOW

ENV = {"JWT_SECRET": "j" * 40}
KEY, SECRET = "aaaaaaaabbbbbbbb3456", "ccccccccdddddddd9999"  # fake values


def mock(routes: dict[str, object], sent: list):
    def handler(request: httpx.Request):
        sent.append(request)
        body = routes.get(request.url.path)
        if body is None:
            return httpx.Response(404, json={"code": -1, "msg": "not mocked"})
        if isinstance(body, httpx.Response):
            return body
        return httpx.Response(200, json=body)
    return httpx.MockTransport(handler)


def bybit_ok(result):
    return {"retCode": 0, "retMsg": "OK", "result": result}


# ---- environments ---------------------------------------------------------

def test_each_environment_has_its_own_fixed_host(monkeypatch):
    monkeypatch.setenv("CRYPTO_BYBIT_BASE_URL", "https://evil.example")
    assert environments.get("bybit", "DEMO").base_url == "https://api-demo.bybit.com"
    assert environments.get("bybit", "LIVE").base_url == "https://api.bybit.com"
    assert environments.get("binance", "TESTNET").base_url == "https://testnet.binance.vision"
    assert environments.get("binance", "DEMO").base_url == "https://demo-api.binance.com"
    assert SignedHttp("bybit", "DEMO", KEY, SECRET).env.base_url == "https://api-demo.bybit.com"
    for bad in [("binance_us", "LIVE"), ("hyperliquid", "LIVE"), ("bybit", "PAPER"), ("binance", "")]:
        with pytest.raises(environments.UnknownEnvironment):
            environments.get(*bad)


# ---- secret store ---------------------------------------------------------

def test_keys_are_encrypted_and_only_readable_with_the_same_key():
    token = secretstore.encrypt(SECRET, ENV)
    assert SECRET not in token
    assert secretstore.decrypt(token, ENV) == SECRET
    assert secretstore.decrypt(token, {"JWT_SECRET": "k" * 40}) is None
    assert secretstore.decrypt(token, {"JWT_SECRET": "j" * 40, "CRYPTO_CREDENTIALS_KEY": "c" * 40}) is None
    with pytest.raises(secretstore.NoEncryptionKey):
        secretstore.encrypt(SECRET, {"JWT_SECRET": "short"})
    fp = secretstore.fingerprint(KEY, ENV)
    assert fp == secretstore.fingerprint(KEY, ENV) and KEY not in fp
    assert secretstore.hint(KEY) == "…3456"


# ---- signing ---------------------------------------------------------------

async def test_bybit_signature_matches_the_documented_scheme():
    sent = []
    http = SignedHttp("bybit", "DEMO", KEY, SECRET, clock=lambda: 1_700_000_000.0,
                      transport=mock({"/v5/account/wallet-balance": bybit_ok({"list": []})}, sent))
    await http.request("GET", "/v5/account/wallet-balance", {"accountType": "UNIFIED"})
    await http.aclose()
    r = sent[0]
    expected = hmac.new(SECRET.encode(), b"1700000000000" + KEY.encode() + b"5000accountType=UNIFIED",
                        hashlib.sha256).hexdigest()
    assert r.url.host == "api-demo.bybit.com"
    assert r.headers["X-BAPI-SIGN"] == expected and r.headers["X-BAPI-API-KEY"] == KEY
    assert r.headers["X-BAPI-TIMESTAMP"] == "1700000000000" and r.headers["X-BAPI-RECV-WINDOW"] == "5000"


async def test_binance_signature_matches_the_documented_scheme():
    sent = []
    http = SignedHttp("binance", "TESTNET", KEY, SECRET, clock=lambda: 1_700_000_000.0,
                      transport=mock({"/api/v3/account": {"canTrade": True, "balances": []}}, sent))
    await http.request("GET", "/api/v3/account", {"omitZeroBalances": "true"})
    await http.aclose()
    r = sent[0]
    q = r.url.query.decode()
    unsigned, sig = q.rsplit("&signature=", 1)
    assert unsigned == "omitZeroBalances=true&timestamp=1700000000000&recvWindow=5000"
    assert sig == hmac.new(SECRET.encode(), unsigned.encode(), hashlib.sha256).hexdigest()
    assert r.headers["X-MBX-APIKEY"] == KEY and r.url.host == "testnet.binance.vision"


@pytest.mark.parametrize("exchange,env,method,path", [
    ("bybit", "LIVE", "POST", "/v5/asset/withdraw/create"),
    ("bybit", "LIVE", "POST", "/v5/asset/transfer/inter-transfer"),
    ("bybit", "LIVE", "POST", "/v5/user/update-api"),
    ("bybit", "DEMO", "GET", "/v5/asset/deposit/query-address"),
    ("binance", "LIVE", "POST", "/sapi/v1/capital/withdraw/apply"),
    ("binance", "LIVE", "POST", "/sapi/v1/asset/transfer"),
    ("binance", "LIVE", "POST", "/fapi/v1/order"),
    ("binance", "TESTNET", "GET", "/sapi/v1/account/apiRestrictions"),
    ("binance", "LIVE", "GET", "/api/v3/order/../../sapi/v1/capital/withdraw/apply"),
])
async def test_transfers_withdrawals_and_unlisted_paths_never_reach_the_network(exchange, env, method, path):
    sent = []
    http = SignedHttp(exchange, env, KEY, SECRET, transport=mock({}, sent))
    with pytest.raises(BlockedRequest):
        await http.request(method, path, {"coin": "USDT", "amount": "1"})
    await http.aclose()
    assert sent == []


def live_view(**over):
    v = {"environment": "LIVE", "enabled": True, "status": "OK", "last_ok_at": NOW.isoformat(),
         "permissions": {"withdraw_enabled": False, "ip_restricted": True, "can_trade_spot": True}}
    v.update(over)
    return v


@pytest.mark.parametrize("exchange,path,params", [
    ("bybit", "/v5/order/create", {"category": "spot", "symbol": "BTCUSDT", "side": "Buy", "orderType": "Market", "qty": "1"}),
    ("bybit", "/v5/position/trading-stop", {"category": "linear", "symbol": "BTCUSDT", "stopLoss": "1"}),
    ("binance", "/api/v3/order", {"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": "1"}),
])
async def test_the_code_lock_refuses_every_live_order_before_sending(exchange, path, params):
    assert livelock.LIVE_ORDERS_UNLOCKED is False
    sent = []
    http = SignedHttp(exchange, "LIVE", KEY, SECRET, connection=live_view(), transport=mock({path: {}}, sent))
    with pytest.raises(livelock.LiveLocked, match="code_lock"):
        await http.request("POST", path, params)
    await http.aclose()
    assert sent == []


def test_every_per_connection_gate_must_also_pass(monkeypatch):
    monkeypatch.setattr(livelock, "LIVE_ORDERS_UNLOCKED", True)  # simulate a future unlock to test the gates
    livelock.require_live_orders(live_view(), NOW)
    for over, gate in [({"environment": "DEMO"}, "environment"), ({"enabled": False}, "enabled"),
                       ({"status": "FAILED"}, "tested"), ({"last_ok_at": (NOW - timedelta(hours=25)).isoformat()}, "tested"),
                       ({"status": "NEEDS_REENTRY"}, "credentials"),
                       ({"permissions": {"withdraw_enabled": True, "ip_restricted": True, "can_trade_spot": True}}, "no_withdrawal"),
                       ({"permissions": {"withdraw_enabled": None, "ip_restricted": True, "can_trade_spot": True}}, "no_withdrawal"),
                       ({"permissions": {"withdraw_enabled": False, "ip_restricted": False, "can_trade_spot": True}}, "ip_restricted"),
                       ({"permissions": {"withdraw_enabled": False, "ip_restricted": True}}, "can_trade")]:
        with pytest.raises(livelock.LiveLocked, match=gate):
            livelock.require_live_orders(live_view(**over), NOW)
    with pytest.raises(livelock.LiveLocked):
        livelock.require_live_orders(None, NOW)


async def test_errors_and_repr_never_show_the_key_or_secret():
    sent = []
    leak = httpx.Response(401, json={"code": -2015, "msg": f"Invalid API-key {KEY} signature=abcdef0123 {SECRET}"})
    http = SignedHttp("binance", "TESTNET", KEY, SECRET, transport=mock({"/api/v3/account": leak}, sent))
    with pytest.raises(ExchangeRequestError) as e:
        await http.request("GET", "/api/v3/account")
    await http.aclose()
    assert KEY not in str(e.value) and SECRET not in str(e.value) and "abcdef0123" not in str(e.value)
    assert SECRET not in repr(http) and KEY not in repr(http)


# ---- connections -----------------------------------------------------------

async def test_saved_keys_are_encrypted_hidden_and_never_shared_between_environments(sf):
    async with sf() as s:
        c = await connections.create(s, exchange="bybit", environment="DEMO", label="demo 1", api_key=KEY,
                                     api_secret=SECRET, actor="admin", env=ENV)
        await s.commit()
        raw = (await s.execute(text("SELECT key_enc, secret_enc, key_hint FROM crypto.api_connections"))).one()
        assert KEY not in raw[0] and SECRET not in raw[1] and raw[2] == "…3456"
        v = connections.view(c)
        assert not ({"key_enc", "secret_enc", "key_fingerprint"} & set(v)) and KEY not in json.dumps(v)
        assert SECRET not in json.dumps(v) and v["enabled"] is False and v["status"] == "UNTESTED"
        with pytest.raises(connections.ConnectionInputError, match="already saved"):
            await connections.create(s, exchange="bybit", environment="LIVE", label="live", api_key=KEY,
                                     api_secret="eeeeeeeeeeee0000", actor="admin", env=ENV)
        for bad in [dict(exchange="binance_us", environment="LIVE"), dict(exchange="bybit", environment="PAPER")]:
            with pytest.raises(connections.ConnectionInputError):
                await connections.create(s, label="x", api_key="AAAAAAAAAA1", api_secret="BBBBBBBBBB1",
                                         actor="admin", env=ENV, **bad)
        with pytest.raises(connections.ConnectionInputError):
            await connections.create(s, exchange="bybit", environment="DEMO", label="y", api_key="has space key",
                                     api_secret="BBBBBBBBBB1", actor="admin", env=ENV)


async def test_bybit_test_reads_permissions_and_flags_a_withdrawing_key(sf):
    sent = []
    routes = {"/v5/account/wallet-balance": bybit_ok({"list": [{"coin": [{"coin": "USDT", "walletBalance": "50000"},
                                                                         {"coin": "BTC", "walletBalance": "0"}]}]}),
              "/v5/user/query-api": bybit_ok({"readOnly": 0, "ips": ["203.0.113.7"],
                                              "permissions": {"Spot": ["SpotTrade"], "ContractTrade": [],
                                                              "Wallet": ["AccountTransfer", "Withdraw"]}})}
    async with sf() as s:
        c = await connections.create(s, exchange="bybit", environment="LIVE", label="main", api_key=KEY,
                                     api_secret=SECRET, actor="admin", env=ENV)
        c.enabled = True
        await s.flush()
        out = await connections.test(s, c, transport=mock(routes, sent), env=ENV, now=NOW)
        await s.commit()
    assert out["ok"] and c.status == "OK"
    p = c.permissions
    assert p["withdraw_enabled"] is True and p["ip_restricted"] is True and p["can_trade_spot"] is True
    assert p["can_trade_derivatives"] is False and p["balances"] == [{"asset": "USDT", "total": "50000"}]
    assert {r.url.host for r in sent} == {"api.bybit.com"} and all(r.method == "GET" for r in sent)
    failed = {g["id"] for g in out["live_gates"] if not g["ok"]}
    assert failed == {"code_lock", "no_withdrawal"}


async def test_binance_live_test_uses_key_restrictions_and_testnet_skips_them(sf):
    acct = {"canTrade": True, "balances": [{"asset": "USDT", "free": "10", "locked": "0"}]}
    restr = {"ipRestrict": True, "enableWithdrawals": False, "enableReading": True,
             "enableSpotAndMarginTrading": True, "enableFutures": False}
    async with sf() as s:
        live = await connections.create(s, exchange="binance", environment="LIVE", label="main", api_key=KEY,
                                        api_secret=SECRET, actor="admin", env=ENV)
        tn = await connections.create(s, exchange="binance", environment="TESTNET", label="tn",
                                      api_key="TNKEY000000000001", api_secret="TNSECRET0000000001", actor="admin", env=ENV)
        live.enabled = True
        await s.flush()
        sent_live, sent_tn = [], []
        out = await connections.test(s, live, transport=mock({"/api/v3/account": acct,
                                                              "/sapi/v1/account/apiRestrictions": restr}, sent_live),
                                     env=ENV, now=NOW)
        await connections.test(s, tn, transport=mock({"/api/v3/account": acct}, sent_tn), env=ENV, now=NOW)
        await s.commit()
    assert [r.url.path for r in sent_live] == ["/api/v3/account", "/sapi/v1/account/apiRestrictions"]
    assert [r.url.path for r in sent_tn] == ["/api/v3/account"]
    assert live.permissions["withdraw_enabled"] is False and live.permissions["ip_restricted"] is True
    assert [g["id"] for g in out["live_gates"] if not g["ok"]] == ["code_lock"]  # only the code lock remains
    assert tn.permissions["verified"] is False and tn.permissions["withdraw_enabled"] is None


async def test_a_failed_or_unreadable_connection_is_recorded_without_secrets(sf):
    geo = httpx.Response(403, text="<html>The Amazon CloudFront distribution is configured to block access from your country.</html>")
    async with sf() as s:
        c = await connections.create(s, exchange="bybit", environment="DEMO", label="d", api_key=KEY,
                                     api_secret=SECRET, actor="admin", env=ENV)
        out = await connections.test(s, c, transport=mock({"/v5/account/wallet-balance": geo}, []), env=ENV, now=NOW)
        assert out["ok"] is False and c.status == "FAILED" and "HTTP 403" in c.last_error and KEY not in c.last_error
        out2 = await connections.test(s, c, transport=mock({}, []), env={"JWT_SECRET": "z" * 40}, now=NOW)
        assert out2["ok"] is False and c.status == "NEEDS_REENTRY"
        await s.commit()
        events = (await s.execute(select(Event).where(Event.source == "connections"))).scalars().all()
        assert events and all(KEY not in json.dumps(e.detail) + e.message for e in events)
        assert (await s.execute(select(ApiConnection))).scalars().one().enabled is False


async def test_practice_orders_need_no_live_gate_but_stay_on_the_practice_host():
    sent = []
    http = SignedHttp("binance", "TESTNET", KEY, SECRET, transport=mock({"/api/v3/order": {"orderId": 1}}, sent))
    await http.request("POST", "/api/v3/order", {"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": "0.001"})
    await http.aclose()
    assert sent[0].url.host == "testnet.binance.vision"
    assert dict(parse_qsl(urlsplit(str(sent[0].url)).query))["side"] == "BUY"
