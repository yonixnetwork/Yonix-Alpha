"""EVM transaction streams (master §9, §13): Nitro broadcast messages built
from real signed transactions are decoded (batches unrolled, hash, to,
selector, sender), sequence gaps / duplicates are counted, copy-target and
launchpad transactions are recorded with the time they were seen; the feed
client resumes at the next sequence number, falls back to the delayed feed
and labels it; the pending-transaction stream reports hash-only and refusing
providers instead of pretending to work. Local WebSocket servers only: the
real Robinhood feed is NOT VERIFIED here."""
import asyncio
import base64
import json

import websockets
from eth_account import Account
from eth_utils import to_checksum_address

from yonixalpha_core.chains.evm import streams as st

TARGET = Account.create()
OTHER = Account.create()
CURVE = "0x" + "c" * 40


class FakeRedis:
    def __init__(self) -> None:
        self.kv: dict[str, str] = {}

    async def set(self, k, v, ex=None):
        self.kv[k] = v

    async def get(self, k):
        return self.kv.get(k)


def signed(acct, to, nonce, data="0x12345678aabb", typ=2) -> bytes:
    tx = {"chainId": 4663, "nonce": nonce, "gas": 100000, "to": to_checksum_address(to), "value": 10 ** 15, "data": data}
    tx.update({"type": 2, "maxPriorityFeePerGas": 1, "maxFeePerGas": 10 ** 9} if typ == 2 else {"gasPrice": 10 ** 9})
    return bytes(acct.sign_transaction(tx).raw_transaction)


def batch(*txs: bytes) -> bytes:
    body = b"".join(len(b"\x04" + t).to_bytes(8, "big") + b"\x04" + t for t in txs)
    return b"\x03" + body


def broadcast(*msgs: tuple[int, bytes], ts: int = 1_790_000_000) -> str:
    return json.dumps({"version": 1, "messages": [
        {"sequenceNumber": seq, "message": {"message": {"header": {"kind": 3, "timestamp": ts}, "l2Msg": base64.b64encode(l2).decode()},
                                            "delayedMessagesRead": 1}, "signature": None} for seq, l2 in msgs]})


async def watch():
    return {TARGET.address.lower()}, {CURVE}


def test_signed_transactions_and_batches_decode():
    t1, t2 = signed(TARGET, CURVE, 0), signed(OTHER, "0x" + "d" * 40, 5, typ=0)
    txs, skipped = st.l2_transactions(batch(t1, t2) + b"")
    assert txs == [t1, t2] and skipped == 0
    d1, d2 = st.decode_signed_tx(t1), st.decode_signed_tx(t2)
    assert d1["to"] == CURVE and d1["selector"] == "0x12345678" and d1["value"] == 10 ** 15 and d1["type"] == 2
    assert d2["to"] == "0x" + "d" * 40 and d2["nonce"] == 5 and d2["type"] == 0
    assert st.recover_sender(t1) == TARGET.address.lower()
    assert st.l2_transactions(b"\x07abc") == ([], 1) and st.decode_signed_tx(b"\x02garbage") is None


async def test_feed_matches_sequence_and_delay():
    r = FakeRedis()
    feed = st.SequencerFeed("robinhood", st.StreamConfig(recover_budget_per_s=1), watch, r)
    by_target = signed(TARGET, "0x" + "e" * 40, 0)  # a router we do not know: found by its sender
    to_curve = signed(OTHER, CURVE, 1)
    noise = [signed(OTHER, "0x" + "f" * 40, i) for i in range(2, 6)]
    n = await feed.handle(broadcast((10, batch(by_target, *noise)), (11, b"\x04" + to_curve)), now=1_790_000_002.0)
    assert n == 2 and feed.stats.matched_copy_targets == 1 and feed.stats.matched_launchpads == 1
    assert feed.stats.senders_recovered + feed.stats.senders_skipped == 6  # 1 per second budget + the curve tx
    rec = json.loads(r.kv[f"yx:evm:seen:robinhood:{st.decode_signed_tx(to_curve)['hash']}"])
    assert rec["source"] == "sequencer_feed" and rec["match"] == ["launchpad"] and rec["from"] == OTHER.address.lower()
    assert feed.stats.delays == [2.0, 2.0]
    await feed.handle(broadcast((11, b"\x04" + to_curve), (15, b"\x03")))  # duplicate, then 3 missing
    assert feed.stats.duplicates == 1 and feed.stats.gaps == 1 and feed.stats.missing_messages == 3
    rep = feed.stats.report()
    assert rep["last_seq"] == 15 and rep["delay_s_median"] == 2.0 and "delays" not in rep
    assert await st.seen(r, "robinhood", st.decode_signed_tx(by_target)["hash"]) is not None


async def test_feed_client_resumes_and_falls_back_to_the_delayed_feed():
    requested = []

    async def primary(ws):
        requested.append(ws.request.headers.get("Arbitrum-Requested-Sequence-Number"))
        if len(requested) == 1:
            await ws.send(broadcast((100, b"\x04" + signed(OTHER, CURVE, 0))))
        await ws.close()  # drops the connection each time

    async def delayed(ws):
        requested.append(("delayed", ws.request.headers.get("Arbitrum-Requested-Sequence-Number")))
        await ws.send(broadcast((101, b"\x04" + signed(OTHER, CURVE, 1))))
        await asyncio.sleep(5)

    async with websockets.serve(primary, "127.0.0.1", 0) as p_srv, websockets.serve(delayed, "127.0.0.1", 0) as d_srv:
        p_url = f"ws://127.0.0.1:{p_srv.sockets[0].getsockname()[1]}"
        d_url = f"ws://127.0.0.1:{d_srv.sockets[0].getsockname()[1]}"
        cfg = st.StreamConfig(robinhood_feed_url=p_url, robinhood_delayed_url=d_url, fallback_after_failures=1)
        feed = st.SequencerFeed("robinhood", cfg, watch, FakeRedis())
        stop = asyncio.Event()

        async def run():
            task = asyncio.create_task(feed.run(stop))
            for _ in range(200):
                await asyncio.sleep(0.05)
                if feed.stats.last_seq == 101:
                    break
            stop.set()
            task.cancel()

        orig_sleep = st._sleep

        async def fast_sleep(stop_ev, seconds):
            await orig_sleep(stop_ev, 0.01)

        st._sleep = fast_sleep
        try:
            await run()
        finally:
            st._sleep = orig_sleep
    # the primary drops after message 100: the delayed feed is asked for 101 (no gap, no replay)
    assert requested[0] is None and requested[1] == ("delayed", "101")
    assert feed.stats.last_seq == 101 and feed.stats.fallback_active and feed.stats.reconnects >= 1


async def test_pending_stream_full_hash_only_and_refused():
    async def server(mode):
        async def handler(ws):
            sub = json.loads(await ws.recv())
            assert sub["params"] == ["newPendingTransactions", True]
            if mode == "refuse":
                await ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "method not allowed on your plan"}}))
                return
            await ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "result": "0xsub"}))
            result = "0xabc" if mode == "hash" else {"hash": "0xAA", "from": TARGET.address, "to": CURVE, "value": "0x10",
                                                     "input": "0xdeadbeef00"}
            await ws.send(json.dumps({"jsonrpc": "2.0", "method": "eth_subscription", "params": {"subscription": "0xsub", "result": result}}))
            await asyncio.sleep(2)
        return handler

    orig_sleep = st._sleep
    states = {}
    for mode in ("full", "hash", "refuse"):
        async with websockets.serve(await server(mode), "127.0.0.1", 0) as srv:
            url = f"ws://127.0.0.1:{srv.sockets[0].getsockname()[1]}"
            r = FakeRedis()

            async def urls():
                return [url]

            stream = st.PendingTxStream("bsc", urls, watch, r)
            stop = asyncio.Event()

            async def fast_sleep(stop_ev, seconds):
                stop.set()

            st._sleep = fast_sleep
            try:
                task = asyncio.create_task(stream.run(stop))
                for _ in range(100):
                    await asyncio.sleep(0.02)
                    if stream.stats.matched or stream.stats.state in (st.LIMITED, st.REFUSED):
                        break
                stop.set()
                await asyncio.wait_for(task, 3)
            finally:
                st._sleep = orig_sleep
            states[mode] = (stream.stats, r)
    full, r_full = states["full"]
    assert full.matched_copy_targets == 1 and json.loads(r_full.kv["yx:evm:seen:bsc:0xaa"])["selector"] == "0xdeadbeef"
    assert states["hash"][0].state == st.LIMITED and "hashes only" in states["hash"][0].detail
    assert states["refuse"][0].state == st.REFUSED and "not allowed" in states["refuse"][0].detail


async def test_pending_stream_without_wss_is_not_configured():
    async def urls():
        return []

    stream = st.PendingTxStream("bsc", urls, watch, FakeRedis())
    stop = asyncio.Event()
    orig = st._sleep

    async def stop_sleep(stop_ev, seconds):
        stop.set()

    st._sleep = stop_sleep
    try:
        await stream.run(stop)
    finally:
        st._sleep = orig
    assert stream.stats.state == st.NOT_CONFIGURED and "confirmed trades" in stream.stats.detail


def test_settings_are_validated():
    cfg, errors = st.parse_config({"robinhood_feed_enabled": False, "recover_budget_per_s": 5})
    assert not errors and not cfg.robinhood_feed_enabled and cfg.recover_budget_per_s == 5
    _, errors = st.parse_config({"robinhood_feed_url": "http://x", "recover_budget_per_s": 0, "x": 1})
    assert len(errors) == 3


async def test_a_settings_or_redis_failure_never_stops_the_streams():
    """data-evm runs the streams in the same gather as discovery: a database
    error while reading settings / WSS URLs, or a Redis error while
    publishing, is recorded and retried instead of raising."""
    class BrokenRedis(FakeRedis):
        async def set(self, k, v, ex=None):
            raise ConnectionError("redis down")

    calls = {"n": 0}

    async def broken_urls():
        calls["n"] += 1
        raise RuntimeError("database unavailable")

    async def broken_cfg():
        raise RuntimeError("database unavailable")

    pending = st.PendingTxStream("bsc", broken_urls, watch, BrokenRedis())
    feed = st.SequencerFeed("robinhood", st.StreamConfig(robinhood_feed_enabled=False), watch, BrokenRedis(), reload=broken_cfg)
    stop = asyncio.Event()
    orig = st._sleep

    async def stop_sleep(stop_ev, seconds):
        stop.set()

    st._sleep = stop_sleep
    try:
        await asyncio.wait_for(pending.run(stop), 3)
        stop.clear()
        await asyncio.wait_for(feed.run(stop), 3)
    finally:
        st._sleep = orig
    assert calls["n"] == 1 and "database unavailable" in pending.stats.last_error
    assert feed.stats.state == st.DISABLED and "database unavailable" in feed.stats.last_error
