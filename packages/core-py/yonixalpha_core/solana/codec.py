"""Minimal base58 and Borsh primitives for decoding Anchor accounts/events.

Pure Python, no dependencies: the only things this codebase decodes are a
handful of fixed pump.fun layouts, which doesn't justify a compiled
Solana SDK in every service image on a 2GB droplet.
"""

import struct

_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_INDEX = {c: i for i, c in enumerate(_ALPHABET)}


def b58encode(data: bytes) -> str:
    n = int.from_bytes(data, "big")
    out = []
    while n > 0:
        n, rem = divmod(n, 58)
        out.append(_ALPHABET[rem])
    leading = len(data) - len(data.lstrip(b"\x00"))
    return "1" * leading + "".join(reversed(out))


def b58decode(text: str) -> bytes:
    n = 0
    for ch in text:
        if ch not in _INDEX:
            raise ValueError(f"invalid base58 character {ch!r}")
        n = n * 58 + _INDEX[ch]
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    leading = len(text) - len(text.lstrip("1"))
    return b"\x00" * leading + body


class TruncatedData(Exception):
    """Raised when a layout asks for more bytes than remain. Anchor programs
    append fields over time, so older accounts/events are legitimately
    shorter — callers treat this as "field absent", not corruption."""


class BorshReader:
    def __init__(self, data: bytes, offset: int = 0):
        self.data = data
        self.offset = offset

    @property
    def remaining(self) -> int:
        return len(self.data) - self.offset

    def _take(self, n: int) -> bytes:
        if self.remaining < n:
            raise TruncatedData(f"need {n} bytes at offset {self.offset}, have {self.remaining}")
        chunk = self.data[self.offset : self.offset + n]
        self.offset += n
        return chunk

    def u8(self) -> int:
        return self._take(1)[0]

    def bool(self) -> bool:
        value = self.u8()
        if value not in (0, 1):
            raise ValueError(f"invalid bool byte {value}")
        return value == 1

    def u64(self) -> int:
        return struct.unpack("<Q", self._take(8))[0]

    def i64(self) -> int:
        return struct.unpack("<q", self._take(8))[0]

    def pubkey(self) -> str:
        return b58encode(self._take(32))

    def string(self) -> str:
        length = struct.unpack("<I", self._take(4))[0]
        if length > 10_000:
            raise ValueError(f"implausible string length {length}")
        return self._take(length).decode("utf-8", errors="replace")


# Pubkey::default(), i.e. 32 zero bytes. pump.fun uses it in `quote_mint`
# to mean "SOL-paired".
DEFAULT_PUBKEY = b58encode(b"\x00" * 32)
