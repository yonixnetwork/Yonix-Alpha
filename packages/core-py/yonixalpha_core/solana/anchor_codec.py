"""Decoding Anchor accounts and events from layouts generated from an IDL
(dbc_layout, launchlab_layout): fixed-size Borsh structs, read field by field.

A layout module provides TYPES ({struct: [(field, type)]}, a type being a
primitive name, a struct name or [element type, length]),
ACCOUNT_DISCRIMINATORS, EVENT_DISCRIMINATORS and PROGRAM_ID.
"""

from __future__ import annotations

import struct
from types import ModuleType
from typing import Any

EVENT_IX_TAG = bytes([228, 69, 165, 46, 81, 203, 154, 29])  # Anchor emit_cpi! self-invocation prefix

_PRIM = {"u8": ("<B", 1), "bool": ("<?", 1), "u16": ("<H", 2), "u32": ("<I", 4), "u64": ("<Q", 8), "i64": ("<q", 8)}


class LayoutError(ValueError):
    """The bytes are not the expected account or are too short."""


def read(types: dict[str, list], t: Any, buf: bytes, off: int) -> tuple[Any, int]:
    if isinstance(t, list):
        out = []
        for _ in range(t[1]):
            v, off = read(types, t[0], buf, off)
            out.append(v)
        return out, off
    if t in _PRIM:
        fmt, n = _PRIM[t]
        if off + n > len(buf):
            raise LayoutError("account data too short")
        return struct.unpack_from(fmt, buf, off)[0], off + n
    if t == "u128":
        if off + 16 > len(buf):
            raise LayoutError("account data too short")
        return int.from_bytes(buf[off:off + 16], "little"), off + 16
    if t == "pubkey":
        from solders.pubkey import Pubkey

        if off + 32 > len(buf):
            raise LayoutError("account data too short")
        return str(Pubkey.from_bytes(buf[off:off + 32])), off + 32
    obj = {}
    for name, ft in types[t]:
        obj[name], off = read(types, ft, buf, off)
    return obj, off


def decode_account(layout: ModuleType, name: str, data: bytes) -> dict[str, Any]:
    """Account data -> dict (snake_case fields), after checking its discriminator."""
    disc = bytes(layout.ACCOUNT_DISCRIMINATORS[name])
    if data[:8] != disc:
        raise LayoutError(f"not a {name} account (discriminator {data[:8].hex()})")
    obj, _ = read(layout.TYPES, name, data, 8)
    return obj


def decode_event(layout: ModuleType, data: bytes) -> tuple[str, dict[str, Any]] | None:
    """An Anchor event from its 8-byte discriminator + data, with or without
    the emit_cpi! instruction tag in front; None if it is none of ours."""
    if data[:8] == EVENT_IX_TAG:
        data = data[8:]
    for name, disc in layout.EVENT_DISCRIMINATORS.items():
        if data[:8] == bytes(disc):
            obj, _ = read(layout.TYPES, name, data, 8)
            return name, obj
    return None


def cpi_events(layout: ModuleType, tx: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Events of one getTransaction result (encoding "json") that the
    layout's program emitted as self-invoked inner instructions (emit_cpi!),
    in execution order."""
    from yonixalpha_core.solana.codec import b58decode

    msg = (tx.get("transaction") or {}).get("message") or {}
    meta = tx.get("meta") or {}
    loaded = meta.get("loadedAddresses") or {}
    keys = list(msg.get("accountKeys") or []) + list(loaded.get("writable") or []) + list(loaded.get("readonly") or [])
    out = []
    for group in sorted(meta.get("innerInstructions") or [], key=lambda g: g.get("index", 0)):
        for ix in group.get("instructions") or []:
            idx = ix.get("programIdIndex")
            if idx is None or idx >= len(keys) or keys[idx] != layout.PROGRAM_ID:
                continue
            try:
                ev = decode_event(layout, b58decode(ix.get("data") or ""))
            except Exception:  # noqa: BLE001 - not an event of ours
                ev = None
            if ev is not None:
                out.append(ev)
    return out
