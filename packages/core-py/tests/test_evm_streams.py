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
import time
from pathlib import Path

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
    feed = st.SequencerFeed("robinhood", st.StreamConfig(recover_budget_per_s=1, verify_feed_signatures=False), watch, r)
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
        cfg = st.StreamConfig(robinhood_feed_url=p_url, robinhood_delayed_url=d_url, fallback_after_failures=1,
                              verify_feed_signatures=False)  # synthetic frames are unsigned
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
    # the primary drops after message 100: the delayed feed is asked for 100, the last one seen (a number past
    # the server's tail would replay its whole backlog); 101 follows with no gap and no duplicate counted
    assert requested[0] is None and requested[1] == ("delayed", "100")
    assert feed.stats.duplicates == 0 and feed.stats.gaps == 0
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


FIXTURE = Path(__file__).parent / "fixtures" / "robinhood_feed" / "frames.jsonl"  # real mainnet frames, see README there
SEQUENCER = "0xdaa526086787d9debe1d7f3ffdb1fe50cf8687f4"


def test_real_feed_messages_are_signed_by_the_sequencer():
    """Nitro's feed-signature preimage, checked on real Robinhood mainnet
    messages: every one recovers the sequencer key; the wrong chain id or one
    changed byte recovers some other address (the preimage binds them)."""
    entries = [m for line in FIXTURE.read_text().splitlines() for m in (json.loads(line).get("messages") or [])]
    assert len(entries) == 11
    assert {st.feed_signer(e, 4663) for e in entries} == {SEQUENCER}
    assert SEQUENCER not in {st.feed_signer(e, 4664) for e in entries}
    tampered = json.loads(json.dumps(entries[0]))
    tampered["message"]["message"]["header"]["timestamp"] += 1
    assert st.feed_signer(tampered, 4663) not in (SEQUENCER, None)
    assert st.feed_signer({**entries[0], "signatureV2": None}, 4663) is None


async def test_real_frames_through_the_feed_with_verification():
    """Real frames end to end: verified, sequenced without gaps, decoded and
    matched (the busiest contract in the capture stands in for a launchpad);
    a forged frame is dropped before it moves the sequence."""
    lines = FIXTURE.read_text().splitlines()
    msgs = [m for line in lines for m in st.parse_broadcast(line)]
    busiest = max({(st.decode_signed_tx(t) or {}).get("to") for m in msgs for t in m["txs"]} - {None},
                  key=lambda a: sum((st.decode_signed_tx(t) or {}).get("to") == a for m in msgs for t in m["txs"]))

    async def w():
        return set(), {busiest}

    r = FakeRedis()
    feed = st.SequencerFeed("robinhood", st.StreamConfig(), w, r)
    forged = json.loads(lines[1])
    forged["messages"][0]["sequenceNumber"] += 1000
    await feed.handle(json.dumps(forged), now=float(msgs[0]["timestamp"]) + 1)
    assert feed.stats.unverified == 1 and feed.stats.last_seq is None
    for line in lines:
        await feed.handle(line, now=float(msgs[-1]["timestamp"]) + 1)
    assert feed.stats.verification == "VERIFIED" and feed.stats.unverified == 1
    assert feed.stats.messages == 11 and feed.stats.gaps == 0 and feed.stats.last_seq == msgs[-1]["seq"]
    assert feed.stats.matched_launchpads > 0
    rec = json.loads(next(v for k, v in r.kv.items() if k.startswith("yx:evm:seen:robinhood:")))
    assert rec["to"] == busiest and rec["from"].startswith("0x") and rec["source"] == "sequencer_feed"


async def test_backlog_reorg_and_resume_handling():
    """First-connection backlog is sequenced but not matched or timed; a
    re-sent sequence number with another block hash is a reorg; the message
    asked for on resume is dropped quietly."""
    feed = st.SequencerFeed("robinhood", st.StreamConfig(verify_feed_signatures=False), watch, FakeRedis())
    now = 1_790_000_100.0

    def frame(seq, ts, bh, l2=b"\x04" + signed(OTHER, CURVE, 0)):
        return json.dumps({"version": 1, "messages": [{"sequenceNumber": seq, "blockHash": bh, "message": {"message": {
            "header": {"kind": 3, "timestamp": ts}, "l2Msg": base64.b64encode(l2).decode()}}}]})

    feed._drain_until = time.monotonic() + 30
    await feed.handle(frame(1, now - 120, "0xa1"), now=now)  # two minutes old: replayed history
    assert feed.stats.backlog_skipped == 1 and feed.stats.matched == 0 and feed.stats.delays == []
    await feed.handle(frame(2, now - 1, "0xa2"), now=now)  # live: drain over
    assert feed.stats.matched_launchpads == 1 and feed.stats.delays == [1.0] and feed._drain_until == 0.0
    await feed.handle(frame(2, now - 1, "0xb2"), now=now)  # same number, different block
    assert feed.stats.reorgs == 1 and feed.stats.duplicates == 0
    feed._resume_seq = 2
    await feed.handle(frame(2, now - 1, "0xb2"), now=now)  # the resume message
    await feed.handle(frame(3, now, "0xa3"), now=now)
    assert feed.stats.duplicates == 0 and feed.stats.last_seq == 3 and feed.stats.messages == 3


async def test_trader_attribution_report_flags_router_mediated_trades():
    """tools.trader_attribution on the real schema: the busiest Pons trader is
    a known router contract, its buys name another recipient; nothing is
    written."""
    import os
    from datetime import datetime, timedelta, timezone

    from sqlalchemy.ext.asyncio import create_async_engine

    from yonixalpha_core.db import models  # noqa: F401
    from yonixalpha_core.db.base import Base, make_session_factory
    from yonixalpha_core.db.models import EvmTrade
    from yonixalpha_core.tools.trader_attribution import report

    router = "0xb8f70e2acf34185a8e72d7bd33e7242da1e63b06"
    wallet = "0x" + "ab" * 20
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    engine = create_async_engine(os.environ.get(
        "DATABASE_URL", "postgresql+asyncpg://yonixalpha:yonixalpha_test_pw@localhost:5432/yonixalpha_test"))
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    try:
        async with make_session_factory(engine)() as s:
            for i in range(5):
                s.add(EvmTrade(event_id=f"r{i}", chain="robinhood", launchpad="pons_v2", token="0x" + "11" * 20,
                               trader=router, is_buy=True, token_amount=1, quote_amount=1, at=now - timedelta(hours=1),
                               extra={"recipient": "0x" + f"{i + 1:040x}"}))
            s.add(EvmTrade(event_id="w", chain="robinhood", launchpad="pons_v2", token="0x" + "11" * 20, trader=wallet,
                           is_buy=True, token_amount=1, quote_amount=1, at=now - timedelta(hours=1),
                           extra={"recipient": wallet}))
            s.add(EvmTrade(event_id="old", chain="robinhood", launchpad="pons_v2", token="0x" + "11" * 20, trader=wallet,
                           is_buy=True, token_amount=1, quote_amount=1, at=now - timedelta(days=9), extra={}))
            await s.commit()

            async def get_code(chain, address):
                return "0x6080" if address == router else "0x"

            lines = await report(s, get_code, 3, 5, now=now)
        text = "\n".join(lines)
        assert "robinhood / pons_v2: 6 trades, 2 distinct traders" in text
        first = lines[1]
        assert router in first and "CONTRACT" in first and "pons-terminal TradeRouter V2" in first
        assert "buys with recipient != buyer: 5" in first
        assert "wallet" in lines[2] and "recipient != buyer: 0" in lines[2]
        assert "router-mediated): 5 (83.3%)" in lines[-1]
    finally:
        await engine.dispose()
