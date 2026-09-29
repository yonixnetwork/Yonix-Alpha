"""Minimal, exact ABI helpers on top of eth_abi: function selectors, call
encoding / output decoding, event topics and log decoding.

Signatures are written the way Solidity canonicalises them, e.g.
"tryBuy(address,uint256,uint256)" or, for a struct argument,
"quoteExactInput((address,address,uint256))". Event definitions keep the
parameter names and the `indexed` flags because the topic layout depends on
them; the names themselves do not change the topic hash.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from eth_abi import decode, encode
from eth_utils import keccak, to_checksum_address

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


def split_types(inner: str) -> list[str]:
    """Top-level comma split that keeps tuple types "(a,b)" together."""
    out, depth, cur = [], 0, ""
    for ch in inner:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur.strip())
    return out


def _parse(signature: str) -> tuple[str, list[str]]:
    name, _, rest = signature.partition("(")
    return name, split_types(rest[:-1])


def selector(signature: str) -> bytes:
    return keccak(text=signature)[:4]


def encode_call(signature: str, *args: Any) -> str:
    """0x-prefixed calldata for `signature` with `args`."""
    _, types = _parse(signature)
    return "0x" + (selector(signature) + encode(types, list(args))).hex()


def decode_output(types: list[str] | tuple[str, ...], data: str | bytes) -> tuple:
    raw = bytes.fromhex(data[2:] if data.startswith("0x") else data) if isinstance(data, str) else data
    return decode(list(types), raw)


def checksum(address: str) -> str:
    return to_checksum_address(address)


def topic_address(address: str) -> str:
    """An address as a 32-byte log topic (for eth_getLogs filters)."""
    return "0x" + "0" * 24 + address.lower().removeprefix("0x")


def _norm(v: Any) -> Any:
    if isinstance(v, str) and v.startswith("0x") and len(v) == 42:
        return to_checksum_address(v)
    if isinstance(v, bytes):
        return "0x" + v.hex()
    if isinstance(v, tuple):
        return tuple(_norm(x) for x in v)
    return v


@dataclass(frozen=True)
class EventDef:
    name: str
    inputs: tuple[tuple[str, str, bool], ...]  # (name, type, indexed)

    @property
    def signature(self) -> str:
        return f"{self.name}({','.join(t for _, t, _ in self.inputs)})"

    @property
    def topic(self) -> str:
        return "0x" + keccak(text=self.signature).hex()

    def decode(self, log: dict[str, Any]) -> dict[str, Any]:
        """Decodes one eth_getLogs entry. An indexed dynamic value (string,
        bytes, array, tuple) is only available as its keccak hash, returned
        as hex; everything else is the ABI value (addresses checksummed)."""
        topics = log.get("topics") or []
        if not topics or topics[0].lower() != self.topic:
            raise ValueError(f"log is not {self.name}")
        indexed = [(n, t) for n, t, i in self.inputs if i]
        if len(topics) - 1 != len(indexed):
            raise ValueError(f"{self.name}: expected {len(indexed)} indexed topics, got {len(topics) - 1}")
        out: dict[str, Any] = {}
        for (n, t), topic in zip(indexed, topics[1:]):
            if t in ("string", "bytes") or t.endswith("]") or t.startswith("("):
                out[n] = topic
            else:
                out[n] = _norm(decode([t], bytes.fromhex(topic[2:]))[0])
        plain = [(n, t) for n, t, i in self.inputs if not i]
        if plain:
            vals = decode_output([t for _, t in plain], log.get("data") or "0x")
            out.update({n: _norm(v) for (n, _), v in zip(plain, vals)})
        return out

    def encode_log(self, values: dict[str, Any], address: str, **extra: Any) -> dict[str, Any]:
        """Builds an eth_getLogs-shaped entry (used by tests and fixtures)."""
        topics = [self.topic]
        for n, t, i in self.inputs:
            if i:
                topics.append("0x" + encode([t], [values[n]]).hex())
        data = encode([t for _, t, i in self.inputs if not i], [values[n] for n, _, i in self.inputs if not i])
        return {"address": address, "topics": topics, "data": "0x" + data.hex(), **extra}


def event(name: str, *inputs: tuple[str, str] | tuple[str, str, bool]) -> EventDef:
    return EventDef(name, tuple((i[0], i[1], bool(i[2]) if len(i) > 2 else False) for i in inputs))


class EventSet:
    """topic0 -> EventDef, for decoding a mixed stream of logs."""

    def __init__(self, *events: EventDef) -> None:
        self.by_topic = {e.topic: e for e in events}
        self.by_name = {e.name: e for e in events}

    @property
    def topics(self) -> list[str]:
        return list(self.by_topic)

    def decode(self, log: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
        topics = log.get("topics") or []
        ev = self.by_topic.get(topics[0].lower()) if topics else None
        if ev is None:
            return None
        return ev.name, ev.decode(log)
