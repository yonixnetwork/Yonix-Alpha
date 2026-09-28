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
