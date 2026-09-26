"""TEST CONNECTION classification against mocked providers: every result
class, and no secret in any result."""

import asyncio
from types import SimpleNamespace

import httpx

from yonixalpha_core import provider_tests as pt

KEY = "HELIUSKEY123456789"


def settings(**kw):
    base = dict(SOLANA_RPC_URL=f"https://rpc.test/?api-key={KEY}", SOLANA_WS_URL=None, SOLANA_RPC_BACKUP_URL=None,
                SOLANA_WS_BACKUP_URL=None, HELIUS_API_KEY=KEY, JUPITER_API_KEY=None, PUMPPORTAL_API_KEY=None,
                BINANCE_API_KEY="bk", BINANCE_API_SECRET="bs", BINANCE_TESTNET=True, BYBIT_API_KEY=None, BYBIT_API_SECRET=None,
                BYBIT_TESTNET=False, HYPERLIQUID_ACCOUNT_ADDRESS=None, HYPERLIQUID_API_WALLET_PRIVATE_KEY=None,
                HYPERLIQUID_TESTNET=False, MT5_BRIDGE_URL=None, MT5_BRIDGE_TOKEN=None, TELEGRAM_BOT_TOKEN="123:tgtokenABC",
                TELEGRAM_CHAT_ID="-100", WALLET_PRIVATE_KEY=None)
    base.update(kw)
    return SimpleNamespace(**base)


def client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def run(name, s, handler):
    async with client(handler) as c:
        return await pt.test_provider(name, s, c)


async def test_connected_only_on_a_real_answer():
    r = await run("solana_rpc", settings(), lambda req: httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": 123}))
    assert r["status"] == pt.CONNECTED and r["detail"] == "getSlot=123"
    r = await run("helius", settings(), lambda req: httpx.Response(200, json={"result": 9}))
    assert r["status"] == pt.CONNECTED and KEY not in str(r)


async def test_each_failure_class():
    assert (await run("solana_rpc", settings(), lambda req: httpx.Response(429)))["status"] == pt.RATE_LIMITED
    assert (await run("solana_rpc", settings(), lambda req: httpx.Response(401)))["status"] == pt.AUTH_FAILED
    assert (await run("solana_rpc", settings(), lambda req: httpx.Response(503)))["status"] == pt.UNAVAILABLE
    assert (await run("solana_rpc", settings(SOLANA_RPC_URL=None), lambda req: httpx.Response(200)))["status"] == pt.INVALID
    assert (await run("nope", settings(), lambda req: httpx.Response(200)))["status"] == pt.INVALID

    def timeout(req):
        raise httpx.ReadTimeout("slow", request=req)
    assert (await run("solana_rpc", settings(), timeout))["status"] == pt.TIMEOUT


async def test_exchange_auth_and_rate_limit_codes():
    auth = await run("binance", settings(), lambda req: httpx.Response(401, json={"code": -2015, "msg": "Invalid API-key"}))
    assert auth["status"] == pt.AUTH_FAILED
    rate = await run("binance", settings(), lambda req: httpx.Response(429, json={"code": -1003, "msg": "Too many"}))
    assert rate["status"] == pt.RATE_LIMITED
    ok = await run("binance", settings(), lambda req: httpx.Response(200, json=[{"asset": "USDT", "availableBalance": "5000"}]))
    assert ok["status"] == pt.CONNECTED and "TESTNET" in ok["detail"] and "5000" in ok["detail"]
    missing = await run("bybit", settings(), lambda req: httpx.Response(200))
    assert missing["status"] == pt.INVALID


async def test_telegram_getme_never_sends_a_message():
    seen = []

    def h(req):
        seen.append(req.url.path)
        return httpx.Response(200, json={"ok": True, "result": {"username": "yx_bot"}})
    r = await run("telegram", settings(), h)
    assert r["status"] == pt.CONNECTED and "@yx_bot" in r["detail"] and all(p.endswith("/getMe") for p in seen)
    bad = await run("telegram", settings(), lambda req: httpx.Response(401, json={"ok": False}))
    assert bad["status"] == pt.AUTH_FAILED and "tgtokenABC" not in str(bad)


async def test_secrets_are_redacted_from_every_detail():
    def leak(req):
        raise httpx.ConnectError(f"failed for {req.url}", request=req)
    r = await run("solana_rpc", settings(), leak)
    assert r["status"] == pt.UNAVAILABLE and KEY not in r["detail"] and "https://rpc.test" in r["detail"]


async def test_the_whole_test_is_bounded_by_the_timeout(monkeypatch):
    monkeypatch.setattr(pt, "TEST_TIMEOUT_SECONDS", 0.05)

    async def slow(req):
        await asyncio.sleep(1)
        return httpx.Response(200, json={"result": 1})
    r = await run("solana_rpc", settings(), slow)
    assert r["status"] == pt.TIMEOUT
