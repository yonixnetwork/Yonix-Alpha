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


def _run(monkeypatch, resolve, builder=None, sim=None):
    """Runs main() against a fake RPC holding one successful 24-account
    PumpSwap sell; returns what it printed."""
    import asyncio
    import base64

    recipients = ["5YxQFdt3Tr9zJLvkFccqXVUwhdTWJQc1fFg2YPbxvxeD"] * 8
    cfg_bytes = (p.GLOBAL_CONFIG_DISC + bytes(32) + bytes(16) + b"\x00" + bytes(32 * 8) + bytes(8) + bytes(64)
                 + bytes(32) + b"\x00" + bytes(32 * 7) + b"\x00"
                 + b"".join(bytes(Pubkey.from_string(r)) for r in recipients)
                 + bytes(8 + 32 + 1 + 1 + 8))
    sell = b58encode(p.SELL + struct.pack("<QQ", 1000, 1))
    tx = {"version": 1, "transaction": {"message": {"accountKeys": [], "instructions": [
        {"programId": p.PUMP_AMM, "accounts": REAL, "data": sell}]}},
        "meta": {"innerInstructions": [], "postTokenBalances": [
            {"owner": SELLER, "mint": MINT, "uiTokenAmount": {"amount": "500"}}]}}

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
            if method == "simulateTransaction" and sim is not None:
                return sim
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

    monkeypatch.setattr(chk, "get_settings", lambda: None)
    monkeypatch.setattr(chk, "make_engine", lambda s: Engine())
    monkeypatch.setattr(chk, "make_session_factory", lambda e: Session)
    monkeypatch.setattr(chk, "effective_rpc", effective_rpc)
    monkeypatch.setattr(chk.RpcManager, "create", classmethod(lambda cls, **kw: Rpc()))
    monkeypatch.setattr(chk.venues, "resolve", resolve)
    if builder is not None:
        monkeypatch.setattr(chk, "NativePumpBuilder", builder)
    assert asyncio.run(chk.main([])) == 0


SELLER = str(Pubkey.new_unique())
MINT = str(Pubkey.new_unique())
REAL = [str(Pubkey.new_unique()), SELLER, str(Pubkey.new_unique()), MINT] + [str(Pubkey.new_unique()) for _ in range(20)]


def test_falls_back_to_the_buyback_recipients_when_the_program_history_is_empty(monkeypatch, capsys):
    """Server 2026-10-07: the provider returned no signatures for the
    PumpSwap program itself; the tool then reads a buyback recipient's."""
    from yonixalpha_core.solana import venue as venues

    async def resolve(rpc, mint, **kw):
        return venues.Venue(kind=venues.PUMP_BONDING_CURVE, mint=mint, reason="still on the curve")

    _run(monkeypatch, resolve)
    out = capsys.readouterr().out
    assert f"signatures of {p.PUMP_AMM}: 0 returned" in out and "1 successful" in out
    assert "successful PumpSwap transactions read: 1" in out and "sell with 24 accounts: 1" in out
    assert "not compared" in out


def test_a_pumpswap_venue_is_compared_and_simulated(monkeypatch, capsys):
    """Server 2026-10-07: the resolver said PUMP_AMM but the tool compared
    the kind with the program id and skipped every sell."""
    from solders.hash import Hash
    from solders.instruction import AccountMeta, Instruction
    from solders.message import Message
    from solders.transaction import Transaction

    from yonixalpha_core.solana import venue as venues

    async def resolve(rpc, mint, **kw):
        return venues.Venue(kind=venues.PUMP_AMM_VENUE, mint=mint, reason="canonical PumpSwap pool", decimals=6)

    ours = REAL[:23] + [str(Pubkey.new_unique())]  # last account differs
    seen = {}

    class Builder:
        def __init__(self, rpc):
            pass

        async def build(self, req, v, exp, user):
            seen["req"] = req
            ix = Instruction(Pubkey.from_string(p.PUMP_AMM), p.SELL + struct.pack("<QQ", 250, 0),
                             [AccountMeta(Pubkey.from_string(k), k == SELLER, True) for k in ours])
            msg = Message.new_with_blockhash([ix], Pubkey.from_string(SELLER), Hash.default())

            class Built:
                tx = Transaction.new_unsigned(msg)
            return Built()

    sim = {"value": {"err": {"InstructionError": [0, {"Custom": 6053}]}, "logs": ["Program log: boom"]}}
    _run(monkeypatch, resolve, Builder, sim)
    out = capsys.readouterr().out
    assert "not compared" not in out
    assert seen["req"].amount == "0.00025"
    assert out.count(" same ") + out.count(" DIFF ") >= 24 and "DIFF" in out
    assert "simulated as this seller: FAILED" in out and "6053" in out and "boom" in out
