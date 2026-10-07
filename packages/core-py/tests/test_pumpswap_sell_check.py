"""The read-only PumpSwap sell check: instruction picking, the seller's
balance left after the sell, and the account-by-account comparison."""
import struct

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
