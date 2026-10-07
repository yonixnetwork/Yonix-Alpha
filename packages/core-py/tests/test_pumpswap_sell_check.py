"""The read-only PumpSwap sell check: instruction picking, the seller's
balance left after the sell, and the account-by-account comparison."""
import struct

from solders.pubkey import Pubkey

from yonixalpha_core.solana import pump_tx as p
from yonixalpha_core.solana.codec import b58encode
from yonixalpha_core.tools import pumpswap_sell_check as chk


def test_picks_pumpswap_sells_and_the_balance_left():
    sell = b58encode(p.SELL + struct.pack("<QQ", 1000, 1))
    tx = {"version": 1, "transaction": {"message": {"accountKeys": [], "instructions": [
        {"programId": p.PUMP_AMM, "accounts": ["pool", "seller", "cfg", "mintX"], "data": sell},
        {"programId": "Other111111111111111111111111111111111111111", "accounts": [], "data": sell}]}},
        "meta": {"innerInstructions": [], "postTokenBalances": [
            {"owner": "seller", "mint": "mintX", "uiTokenAmount": {"amount": "500"}}]}}
    got = chk.amm_instructions(tx)
    assert [(n, a) for n, a, _ in got] == [("sell", ["pool", "seller", "cfg", "mintX"])]
    assert chk.holder_after(tx, "seller", "mintX") == 500 and chk.holder_after(tx, "seller", "other") == 0


def test_compare_marks_differences_and_buyback_recipients():
    lines = chk.compare(["a", "b", "R"], ["a", "c"], {"R"})
    assert lines[0].split()[1] == "same" and lines[1].split()[1] == "DIFF"
    assert "R *" in lines[2] and lines[2].rstrip().endswith("ours -")


def test_falls_back_to_the_buyback_recipients_when_the_program_history_is_empty(monkeypatch, capsys):
    """Server 2026-10-07: the provider returned no signatures for the
    PumpSwap program itself; the tool then reads a buyback recipient's."""
    import asyncio
    import base64

    from yonixalpha_core.solana import venue as venues

    recipients = ["5YxQFdt3Tr9zJLvkFccqXVUwhdTWJQc1fFg2YPbxvxeD"] * 8
    cfg_bytes = (p.GLOBAL_CONFIG_DISC + bytes(32) + bytes(16) + b"\x00" + bytes(32 * 8) + bytes(8) + bytes(64)
                 + bytes(32) + b"\x00" + bytes(32 * 7) + b"\x00"
                 + b"".join(bytes(Pubkey.from_string(r)) for r in recipients)
                 + bytes(8 + 32 + 1 + 1 + 8))
    sell = b58encode(p.SELL + struct.pack("<QQ", 1000, 1))
    tx = {"version": 1, "transaction": {"message": {"accountKeys": [], "instructions": [
        {"programId": p.PUMP_AMM, "accounts": ["pool", "seller", "cfg", "mintX"], "data": sell}]}},
        "meta": {"innerInstructions": [], "postTokenBalances": [
            {"owner": "seller", "mint": "mintX", "uiTokenAmount": {"amount": "500"}}]}}

    class Rpc:
        def replace_endpoints(self, specs):
            pass

        async def call(self, method, params, priority=None):
            if method == "getAccountInfo":
                return {"value": {"data": [base64.b64encode(cfg_bytes).decode(), "base64"]}}
            if method == "getSignaturesForAddress":
                return [] if params[0] == p.PUMP_AMM else [{"signature": "s1", "err": None}]
            if method == "getTransaction":
                return tx
            raise AssertionError(method)

    class Engine:
        async def dispose(self):
            pass

    class Session:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *a):
            return False

    async def effective_rpc(session, settings):
        return [{"url": "http://fake"}]

    async def resolve(rpc, mint, **kw):
        return venues.Venue(kind=venues.PUMP_BONDING_CURVE, mint=mint, reason="still on the curve")

    monkeypatch.setattr(chk, "get_settings", lambda: None)
    monkeypatch.setattr(chk, "make_engine", lambda s: Engine())
    monkeypatch.setattr(chk, "make_session_factory", lambda e: Session)
    monkeypatch.setattr(chk, "effective_rpc", effective_rpc)
    monkeypatch.setattr(chk.RpcManager, "create", classmethod(lambda cls, **kw: Rpc()))
    monkeypatch.setattr(chk.venues, "resolve", resolve)
    assert asyncio.run(chk.main([])) == 0
    out = capsys.readouterr().out
    assert f"signatures of {p.PUMP_AMM}: 0 returned" in out and "1 successful" in out
    assert "successful PumpSwap transactions read: 1" in out and "sell with 4 accounts: 1" in out
    assert "not compared" in out
