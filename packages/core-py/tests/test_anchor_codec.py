"""Anchor account / event decoding shared by the DBC and LaunchLab read paths:
events found in a getTransaction result, in order, ours only."""

import struct

import pytest

from yonixalpha_core.solana import anchor_codec, dbc, dbc_layout, launchlab, launchlab_layout
from yonixalpha_core.solana.codec import b58encode

_FMT = {"u8": "<B", "bool": "<?", "u16": "<H", "u32": "<I", "u64": "<Q", "i64": "<q"}
KEY = "So11111111111111111111111111111111111111112"


def encode(types, t, value) -> bytes:
    """Borsh encoding of a value in a generated layout (the reverse of read)."""
    if isinstance(t, list):
        return b"".join(encode(types, t[0], v) for v in value)
    if t in _FMT:
        return struct.pack(_FMT[t], value)
    if t == "u128":
        return value.to_bytes(16, "little")
    if t == "pubkey":
        from solders.pubkey import Pubkey

        return bytes(Pubkey.from_string(value))
    return b"".join(encode(types, ft, value[name]) for name, ft in types[t])


def sample(types, t, n=[0]):  # noqa: B006 - a counter, so every field differs
    if isinstance(t, list):
        return [sample(types, t[0]) for _ in range(t[1])]
    if t == "pubkey":
        return KEY
    if t == "bool":
        return True
    if t in _FMT or t == "u128":
        n[0] += 1
        return {"u8": n[0] % 200, "u16": n[0] % 60_000, "u32": n[0] * 1_003}.get(t, n[0] * 1_000_003)
    return {name: sample(types, ft) for name, ft in types[t]}


def _event(layout, name):
    value = sample(layout.TYPES, name)
    data = anchor_codec.EVENT_IX_TAG + bytes(layout.EVENT_DISCRIMINATORS[name]) + encode(layout.TYPES, name, value)
    return value, b58encode(data)


def test_events_round_trip_and_are_found_in_a_transaction():
    swap, swap_data = _event(dbc_layout, "EvtSwap2")
    trade, trade_data = _event(launchlab_layout, "TradeEvent")
    keys = ["Payer1111111111111111111111111111111111111", dbc_layout.PROGRAM_ID]
    tx = {"transaction": {"message": {"accountKeys": keys}},
          "meta": {"loadedAddresses": {"writable": [], "readonly": [launchlab_layout.PROGRAM_ID]},
                   "innerInstructions": [
                       {"index": 0, "instructions": [
                           {"programIdIndex": 1, "data": swap_data},
                           {"programIdIndex": 0, "data": swap_data},  # another program: ignored
                           {"programIdIndex": 1, "data": b58encode(b"\x01" * 12)}]},  # not an event: ignored
                       {"index": 1, "instructions": [{"programIdIndex": 2, "data": trade_data}]}]}}
    assert dbc.swap_events(tx) == [("EvtSwap2", swap)]
    assert launchlab.trade_events(tx) == [trade]
    assert anchor_codec.decode_event(dbc_layout, b"\x00" * 16) is None


def test_accounts_check_discriminator_and_length():
    value = sample(launchlab_layout.TYPES, "GlobalConfig")
    data = bytes(launchlab_layout.ACCOUNT_DISCRIMINATORS["GlobalConfig"]) + encode(launchlab_layout.TYPES, "GlobalConfig", value)
    assert anchor_codec.decode_account(launchlab_layout, "GlobalConfig", data) == value
    with pytest.raises(anchor_codec.LayoutError, match="not a PoolState"):
        anchor_codec.decode_account(launchlab_layout, "PoolState", data)
    with pytest.raises(anchor_codec.LayoutError, match="too short"):
        anchor_codec.decode_account(launchlab_layout, "GlobalConfig", data[:-1])
