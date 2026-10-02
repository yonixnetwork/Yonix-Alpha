"""What an address seen as a trader is (master §18-28, M10b).

Launchpad events name whoever called the launchpad. On the server
(2026-10-02, tools.trader_attribution) 9 of the 12 busiest Flap "traders"
were contracts (the busiest alone 40.8% of 872k trades in 3 days), and on
Pons the busiest addresses were routers. A contract cannot sign a
transaction, so it is a router or bot through which wallets trade, never a
wallet to profile, follow or copy.

  WALLET            no code
  DELEGATED_WALLET  an EOA with an EIP-7702 delegation designator
                    (code = 0xef0100 || delegate, 23 bytes): still a wallet
                    that signs its own transactions
  CONTRACT          any other code

Results are stored in evm_address_kinds. A CONTRACT stays a contract; a
WALLET can gain a 7702 delegation, so wallets are re-checked after
RECHECK. Lookups are bounded per call (eth_getCode on public endpoints).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core.logging import get_logger

log = get_logger("evm.address_kinds")

WALLET, DELEGATED, CONTRACT = "WALLET", "DELEGATED_WALLET", "CONTRACT"
DELEGATION_PREFIX = "0xef0100"
RECHECK = timedelta(days=7)
MAX_LOOKUPS = 300  # new eth_getCode calls per resolve()


def classify_code(code: str | None) -> tuple[str, str | None, int]:
    """(kind, delegate, code size in bytes) from eth_getCode's result."""
    c = (code or "0x").lower()
    size = max(0, (len(c) - 2) // 2)
    if size == 0:
        return WALLET, None, 0
    if c.startswith(DELEGATION_PREFIX) and size == 23:
        return DELEGATED, "0x" + c[len(DELEGATION_PREFIX):], size
    return CONTRACT, None, size


async def resolve(session: AsyncSession, rpc, chain: str, addresses: Iterable[str], now: datetime,
                  max_lookups: int = MAX_LOOKUPS) -> dict[str, str]:
    """address (lower) -> kind for every address known or looked up now;
    addresses beyond the lookup budget, or whose lookup failed, are absent
    (unknown, never assumed to be wallets). Caller commits."""
    from yonixalpha_core.db.models import EvmAddressKind as K

    addrs = list(dict.fromkeys(a.lower() for a in addresses if a))
    known: dict[str, Any] = {}
    for i in range(0, len(addrs), 1000):
        chunk = addrs[i:i + 1000]
        for r in (await session.execute(select(K).where(K.chain == chain, K.address.in_(chunk)))).scalars():
            known[r.address] = r
    out = {a: r.kind for a, r in known.items()}
    stale = [a for a in addrs if a not in known or (known[a].kind != CONTRACT and now - known[a].checked_at > RECHECK)]
    looked = 0
    for a in stale[:max_lookups]:
        try:
            code = await rpc.get_code(a)
        except Exception as exc:  # noqa: BLE001 - unknown for now, retried next time
            log.info("address_kinds.lookup_failed", chain=chain, error=type(exc).__name__)
            continue
        kind, delegate, size = classify_code(code)
        stmt = insert(K).values(chain=chain, address=a, kind=kind, delegate=delegate, code_size=size, checked_at=now)
        await session.execute(stmt.on_conflict_do_update(index_elements=["chain", "address"], set_={
            "kind": kind, "delegate": delegate, "code_size": size, "checked_at": now}))
        out[a] = kind
        looked += 1
    if looked:
        log.info("address_kinds.resolved", chain=chain, looked_up=looked, pending=max(0, len(stale) - max_lookups))
    return out
