"""The read-only PumpSwap window check: page selection, the window filter,
other traders' sells and our own confirmed sells."""
import asyncio
import base64
import struct
from datetime import UTC, datetime

from solders.pubkey import Pubkey

from yonixalpha_core.solana import pump_tx as p
from yonixalpha_core.solana.codec import b58encode
from yonixalpha_core.tools import pumpswap_window_check as w

RECIPIENT = "5YxQFdt3Tr9zJLvkFccqXVUwhdTWJQc1fFg2YPbxvxeD"


def test_spread_keeps_first_and_last():
    assert w.spread(list(range(10)), 3) == [0, 4, 9] and w.spread([1, 2], 5) == [1, 2]
    assert w.parse_time("2026-10-06T22:00") == datetime(2026, 10, 6, 22, 0, tzinfo=UTC)


def test_counts_sells_inside_the_window_and_reads_our_confirmed_sells(monkeypatch, capsys):
    cfg = (p.GLOBAL_CONFIG_DISC + bytes(32) + bytes(16) + b"\x00" + bytes(32 * 8) + bytes(8) + bytes(64)
           + bytes(32) + b"\x00" + bytes(32 * 7) + b"\x00" + bytes(Pubkey.from_string(RECIPIENT)) * 8 + bytes(50))
    inside = int(datetime(2026, 10, 7, 1, 30, tzinfo=UTC).timestamp())
    older = int(datetime(2026, 10, 6, 20, 0, tzinfo=UTC).timestamp())
    accounts = [str(Pubkey.new_unique()) for _ in range(22)] + [RECIPIENT, str(Pubkey.new_unique())]
    sell = {"version": 0, "transaction": {"message": {"accountKeys": [], "instructions": [
        {"programId": p.PUMP_AMM, "accounts": accounts, "data": b58encode(p.SELL + struct.pack("<QQ", 5, 1))}]}},
        "meta": {"innerInstructions": []}}
    pages = []

    class Rpc:
        def replace_endpoints(self, specs):
            pass

        async def call(self, method, params, priority=None):
            if method == "getAccountInfo":
                return {"value": {"data": [base64.b64encode(cfg).decode(), "base64"]}}
            if method == "getSignaturesForAddress":
                pages.append(params[1])
                return [{"signature": "in1", "err": None, "blockTime": inside},
                        {"signature": "in2", "err": {"x": 1}, "blockTime": inside},
                        {"signature": "old", "err": None, "blockTime": older}]
            if method == "getTransaction":
                return sell
            raise AssertionError(method)

    class Result:
        def all(self):
            return [("ours1", datetime(2026, 10, 6, 22, 43, 56, tzinfo=UTC))]

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def execute(self, stmt):
            return Result()

    class Engine:
        async def dispose(self):
            pass

    async def effective_rpc(session, settings):
        return [{"url": "http://fake"}]

    monkeypatch.setattr(w, "get_settings", lambda: None)
    monkeypatch.setattr(w, "make_engine", lambda s: Engine())
    monkeypatch.setattr(w, "make_session_factory", lambda e: Session)
    monkeypatch.setattr(w, "effective_rpc", effective_rpc)
    monkeypatch.setattr(w.RpcManager, "create", classmethod(lambda cls, **kw: Rpc()))
    assert asyncio.run(w.main(["--pool", "POOL", "--mint", "MINT"])) == 0
    out = capsys.readouterr().out
    assert len(pages) == 1  # the oldest signature is before the window: no second page
    assert "transactions inside the window: 2" in out and "10-07 01:00  failed 1" in out and "10-07 01:00  ok     1" in out
    assert "10-07 01:00  24 accounts: 1" in out and f"{RECIPIENT} *: 1" in out
    assert "10-06 22:43:56 24 accounts; tail" in out and "5YxQFdt3*" in out
    assert "Nothing was signed or sent." in out
