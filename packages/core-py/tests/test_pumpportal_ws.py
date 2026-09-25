"""PumpPortal data feed against a scripted WebSocket (no network)."""

import asyncio
import json
import os

import pytest_asyncio
from pydantic import SecretStr
from redis.asyncio import from_url

from yonixalpha_core.solana import pump_stream, pumpportal_ws

MINT = "7" * 43 + "p"


@pytest_asyncio.fixture
async def redis():
    r = from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/15"), decode_responses=True)
    await r.flushdb()
    yield r
    await r.flushdb()
    await r.aclose()


class FakeWs:
    def __init__(self, incoming, stop):
        self.incoming, self.sent, self.stop = list(incoming), [], stop

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def send(self, text):
        self.sent.append(json.loads(text))

    async def recv(self):
        if not self.incoming:
            self.stop.set()
            await asyncio.sleep(0)
            raise asyncio.TimeoutError
        return self.incoming.pop(0)


def test_key_goes_only_into_the_url():
    assert pumpportal_ws.connection_url(None) == "wss://pumpportal.fun/api/data"
    assert pumpportal_ws.connection_url(SecretStr("abc")) == "wss://pumpportal.fun/api/data?api-key=abc"


async def test_free_feed_subscribes_and_stores_creates_and_migrations(redis):
    stop = asyncio.Event()
    msgs = [json.dumps({"message": "Successfully subscribed to keys."}),
            json.dumps({"txType": "create", "mint": MINT, "name": "Pipe", "symbol": "PIPE", "traderPublicKey": "C" * 44,
                        "initialBuy": 1000, "solAmount": 0.5, "marketCapSol": 30, "signature": "s1", "pool": "pump"}),
            json.dumps({"txType": "migrate", "mint": MINT, "signature": "s2", "pool": "pump-amm"}),
            "not json", json.dumps({"txType": "create", "mint": "short"})]
    urls = []

    def connect(url):
        urls.append(url)
        ws = FakeWs(msgs, stop)
        connect.ws = ws
        return ws

    feed = pumpportal_ws.PumpPortalFeed(redis, None, connect=connect)
    await feed.session(stop)
    assert urls == ["wss://pumpportal.fun/api/data"]
    assert connect.ws.sent == [{"method": "subscribeNewToken"}, {"method": "subscribeMigration"}]  # no metered calls
    assert await redis.zscore(pumpportal_ws.NEW, MINT) is not None
    assert await redis.zscore(pumpportal_ws.MIGRATED, MINT) is not None
    assert json.loads(await redis.get(pumpportal_ws.meta_key(MINT)))["symbol"] == "PIPE"
    assert await pumpportal_ws.heartbeat(redis) is not None


async def test_with_a_key_held_mints_get_trade_subscriptions(redis):
    stop = asyncio.Event()
    trade = json.dumps({"txType": "sell", "mint": MINT, "solAmount": 1.2, "tokenAmount": 5, "traderPublicKey": "T" * 44,
                        "signature": "s3"})

    def connect(url):
        connect.url = url
        connect.ws = FakeWs([trade], stop)
        return connect.ws

    async def held():
        return {MINT}

    feed = pumpportal_ws.PumpPortalFeed(redis, SecretStr("k"), held_mints=held, connect=connect)
    await feed.session(stop)
    assert connect.url.endswith("?api-key=k")
    assert {"method": "subscribeTokenTrade", "keys": [MINT]} in connect.ws.sent
    rows = [json.loads(x) for x in await redis.lrange(pumpportal_ws.trades_key(MINT), 0, -1)]
    assert rows[0]["side"] == "sell" and rows[0]["solAmount"] == 1.2


async def test_coverage_compares_with_the_on_chain_stream(redis):
    now = 1_800_000_000.0
    other = "8" * 43 + "p"
    await redis.zadd(pumpportal_ws.NEW, {MINT: now - 300, other: now - 300, "9" * 44: now - 10})  # last one in grace
    await redis.zadd(pump_stream.RECENT, {MINT: now - 299})
    cov = await pumpportal_ws.coverage(redis, now)
    assert cov == {"announced": 2, "seen_on_chain": 1, "coverage": 0.5}


async def test_disconnect_is_logged_without_the_url(redis, capsys):
    stop = asyncio.Event()

    def connect(url):
        stop.set()
        raise OSError(f"failed to connect to {url}")

    await pumpportal_ws.PumpPortalFeed(redis, SecretStr("secret-key"), connect=connect).run(stop)
    out = capsys.readouterr()
    assert "secret-key" not in out.out + out.err
    assert await redis.hget(pumpportal_ws.STATS, "disconnects") == "1"
