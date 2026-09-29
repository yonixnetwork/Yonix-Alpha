"""The EVM half of the trading wallet: one account for BSC and Robinhood
Chain. Only the public address ever leaves this module; the private key (if
configured) is used solely to check that it belongs to that address."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

from eth_account import Account
from eth_utils import is_address, to_checksum_address

from yonixalpha_core.chains.evm.rpc import EvmRpc, EvmRpcUnavailableError


def account(settings: Any) -> dict[str, Any]:
    """{"configured", "address", "key_configured", "status", "detail"} —
    status OK / WATCH_ONLY / MISMATCH / INVALID / NOT_CONFIGURED."""
    addr = getattr(settings, "EVM_WALLET_ADDRESS", None)
    key = getattr(settings, "EVM_WALLET_PRIVATE_KEY", None)
    key = key.get_secret_value() if hasattr(key, "get_secret_value") else key
    out: dict[str, Any] = {"configured": bool(addr or key), "address": None, "key_configured": bool(key)}
    if addr and not is_address(addr):
        return {**out, "status": "INVALID", "detail": "EVM_WALLET_ADDRESS is not a 0x address"}
    derived = None
    if key:
        try:
            derived = Account.from_key(key).address
        except Exception:  # noqa: BLE001 - never echo the key or its parse error
            return {**out, "status": "INVALID", "detail": "EVM_WALLET_PRIVATE_KEY is not a valid secp256k1 key"}
    if addr and derived and to_checksum_address(addr) != derived:
        return {**out, "address": to_checksum_address(addr), "status": "MISMATCH",
                "detail": "the private key does not belong to EVM_WALLET_ADDRESS"}
    address = derived or (to_checksum_address(addr) if addr else None)
    if address is None:
        return {**out, "status": "NOT_CONFIGURED", "detail": "set EVM_WALLET_ADDRESS (server .env)"}
    return {**out, "address": address, "status": "OK" if derived else "WATCH_ONLY",
            "detail": "key matches the address" if derived else "address only (no signing key: none is needed for paper)"}


async def native_balances(address: str, rpcs: dict[str, EvmRpc]) -> dict[str, dict[str, Any]]:
    async def one(chain: str, rpc: EvmRpc) -> tuple[str, dict[str, Any]]:
        try:
            wei = await asyncio.wait_for(rpc.get_balance(address), 8)
            return chain, {"status": "LIVE", "balance": str(Decimal(wei) / Decimal(10) ** 18)}
        except (EvmRpcUnavailableError, asyncio.TimeoutError) as exc:
            return chain, {"status": "UNAVAILABLE", "balance": None, "detail": str(exc)[:160] or "timeout"}

    return dict(await asyncio.gather(*(one(c, r) for c, r in rpcs.items())))
