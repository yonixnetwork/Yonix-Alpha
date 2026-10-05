"""Transaction versions in RPC responses.

getTransaction declares MAX_SUPPORTED_TRANSACTION_VERSION (solana.rpc) and
the node renders the transaction as JSON, so nothing here decodes binary
transactions. What differs between versions is the *message* layout; the
meta (balances, fees, log messages) does not. Parsers that only read meta
and logs accept every version the node returns; parsers that read the
message check the version with `require_version` and fail closed on one
they were not written for, instead of misreading it.
"""

from typing import Any


class UnsupportedTransactionLayout(ValueError):
    pass


def version_of(tx: dict[str, Any] | None) -> str | int | None:
    """"legacy", 0, 1, ... as the node reports it; None for no transaction."""
    if not tx:
        return None
    v = tx.get("version", "legacy")
    return v if v == "legacy" else int(v)


def require_version(tx: dict[str, Any] | None, allowed: tuple) -> str | int | None:
    v = version_of(tx)
    if v is not None and v not in allowed:
        raise UnsupportedTransactionLayout(f"transaction version {v} is not one this parser reads ({list(allowed)})")
    return v


JSON_LAYOUT_VERSIONS = ("legacy", 0)  # message layouts whose index-based "json" rendering is known


def instructions(tx: dict[str, Any] | None, *, outer: bool = True, inner: bool = True) -> list[tuple[str, list[str], str]]:
    """(program id, account addresses, base58 data) of a getTransaction
    result's instructions: outer ones first, then the inner ones in execution
    order. Instructions the node parsed itself (system, token programs) carry
    no raw data and are left out.

    encoding "jsonParsed": the node resolved every program id and account to
    an address, so this reads any transaction version. encoding "json": ids
    are indices into accountKeys + loadedAddresses, a message layout known
    for legacy and v0 only; another version raises
    UnsupportedTransactionLayout instead of being misread."""
    if not tx:
        return []
    msg = (tx.get("transaction") or {}).get("message") or {}
    meta = tx.get("meta") or {}
    raw = list(msg.get("instructions") or []) if outer else []
    if inner:
        for group in sorted(meta.get("innerInstructions") or [], key=lambda g: g.get("index", 0)):
            raw += group.get("instructions") or []
    keys: list[str] | None = None
    out = []
    for ix in raw:
        if "programId" in ix:  # jsonParsed: resolved by the node
            if "data" not in ix:
                continue  # parsed by the node (no raw data)
            out.append((ix["programId"], list(ix.get("accounts") or []), ix["data"]))
            continue
        if keys is None:
            require_version(tx, JSON_LAYOUT_VERSIONS)
            loaded = meta.get("loadedAddresses") or {}
            keys = [k if isinstance(k, str) else k.get("pubkey") for k in msg.get("accountKeys") or []]
            keys += list(loaded.get("writable") or []) + list(loaded.get("readonly") or [])
        try:
            out.append((keys[ix["programIdIndex"]], [keys[i] for i in ix.get("accounts") or []], ix.get("data") or ""))
        except (IndexError, KeyError, TypeError):
            continue  # a malformed instruction is skipped, never guessed
    return out
