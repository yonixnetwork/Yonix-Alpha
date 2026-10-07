"""Pump change history (read-only): when the Pump, PumpSwap and Pump fee
programs were last upgraded, and the latest transactions of the PumpSwap
GlobalConfig admin, named by instruction (for example update_buyback_config).
Times inside the window when our PumpSwap sells failed are marked.

    $C run --rm paper-trading python -m yonixalpha_core.tools.pump_change_history [--txs 25]

Written 2026-10-07: from 2026-10-06 22:44 to 2026-10-07 07:44 UTC every
PumpSwap sell our native builder built failed with 6053
BuybackFeeRecipientNotAuthorized; the same builder's sell for the same pool
simulated OK afterwards. A program upgrade or a buyback-config change by
Pump inside that window would explain it.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import struct
from datetime import UTC, datetime
from typing import Any

import httpx
from solders.pubkey import Pubkey

from yonixalpha_core.config import get_settings
from yonixalpha_core.db.base import make_engine, make_session_factory
from yonixalpha_core.solana import pump_tx as p
from yonixalpha_core.solana.codec import b58decode, b58encode
from yonixalpha_core.solana.rpc import RpcManager, get_transaction_params
from yonixalpha_core.solana.rpc_registry import effective_rpc
from yonixalpha_core.solana.txversion import instructions

UPGRADEABLE_LOADER = "BPFLoaderUpgradeab1e11111111111111111111111"
FAILURE_WINDOW = (datetime(2026, 10, 6, 22, 0, tzinfo=UTC), datetime(2026, 10, 7, 8, 30, tzinfo=UTC))
# pump_amm IDL (pump-swap-sdk 1.20.0) instructions that only the admin signs.
ADMIN_INSTRUCTIONS = (
    "admin_set_coin_creator", "admin_set_coin_creator_fee_editable", "admin_update_token_incentives", "create_config",
    "disable", "set_boost_authority", "set_reserved_fee_recipients", "toggle_boost", "toggle_cashback_enabled",
    "toggle_mayhem_mode", "update_admin", "update_buyback_config", "update_creator_fee_config", "update_fee_config",
)


def anchor_disc(name: str) -> bytes:
    return hashlib.sha256(f"global:{name}".encode()).digest()[:8]


NAMES = {anchor_disc(n): n for n in ADMIN_INSTRUCTIONS}


def programdata_address(program: str) -> str:
    return str(Pubkey.find_program_address([bytes(Pubkey.from_string(program))], Pubkey.from_string(UPGRADEABLE_LOADER))[0])


def last_deploy_slot(data: bytes) -> int | None:
    """ProgramData account: u32 tag 3, u64 slot of the last deploy, Option<Pubkey> authority."""
    if len(data) < 12 or struct.unpack_from("<I", data, 0)[0] != 3:
        return None
    return struct.unpack_from("<Q", data, 4)[0]


def when(ts: int | None) -> str:
    if ts is None:
        return "time unknown"
    t = datetime.fromtimestamp(ts, UTC)
    mark = "  <- inside the sell-failure window" if FAILURE_WINDOW[0] <= t <= FAILURE_WINDOW[1] else ""
    return t.strftime("%Y-%m-%d %H:%M:%S UTC") + mark


def amm_names(tx: dict[str, Any]) -> list[str]:
    out = []
    for prog, _accounts, data in instructions(tx):
        if prog == p.PUMP_AMM:
            raw = b58decode(data)
            out.append(NAMES.get(raw[:8], "other:" + raw[:8].hex()))
    return out


async def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--txs", type=int, default=25, help="latest admin transactions read")
    a = ap.parse_args(argv)
    settings = get_settings()
    engine = make_engine(settings)
    async with make_session_factory(engine)() as session:
        specs = await effective_rpc(session, settings)
    await engine.dispose()
    if not specs:
        print("no RPC endpoint configured")
        return 1
    async with httpx.AsyncClient() as http:
        rpc = RpcManager.create(client=http, primary_url=specs[0]["url"])
        rpc.replace_endpoints(specs)
        print(f"sell-failure window checked: {FAILURE_WINDOW[0]:%Y-%m-%d %H:%M} to {FAILURE_WINDOW[1]:%Y-%m-%d %H:%M} UTC")
        print("\nLast program upgrade:")
        for label, program in (("PumpSwap (pump-amm)", p.PUMP_AMM), ("Pump bonding curve", p.PUMP), ("Pump fees", p.PUMP_FEE)):
            addr = programdata_address(program)
            try:
                res = await rpc.call("getAccountInfo", [addr, {"encoding": "base64", "dataSlice": {"offset": 0, "length": 48}}])
                slot = last_deploy_slot(base64.b64decode(((res or {}).get("value") or {})["data"][0]))
                ts = await rpc.call("getBlockTime", [slot]) if slot is not None else None
                print(f"  {label}: slot {slot}, {when(ts)}")
            except Exception as exc:  # noqa: BLE001
                print(f"  {label}: error {type(exc).__name__}: {str(exc)[:120]}")
            try:
                sigs = await rpc.call("getSignaturesForAddress", [addr, {"limit": 5}]) or []
                for s in sigs:
                    print(f"     upgrade tx {s['signature'][:20]}... {when(s.get('blockTime'))}{' FAILED' if s.get('err') else ''}")
            except Exception as exc:  # noqa: BLE001
                print(f"     upgrade transactions: error {type(exc).__name__}")
        raw = base64.b64decode((await rpc.call("getAccountInfo", [p.amm_global_config_pda(), {"encoding": "base64"}]))
                               ["value"]["data"][0])
        admin = b58encode(raw[8:40])
        print(f"\nPumpSwap GlobalConfig admin {admin}: latest {a.txs} transactions")
        try:
            sigs = await rpc.call("getSignaturesForAddress", [admin, {"limit": a.txs}]) or []
        except Exception as exc:  # noqa: BLE001
            print(f"  error {type(exc).__name__}: {str(exc)[:120]}")
            sigs = []
        if not sigs:
            print("  none returned")
        for s in sigs:
            names = "?"
            try:
                tx = await rpc.call("getTransaction", get_transaction_params(s["signature"], "jsonParsed"))
                names = ", ".join(amm_names(tx)) or "no PumpSwap instruction"
            except Exception as exc:  # noqa: BLE001
                names = f"not read ({type(exc).__name__})"
            print(f"  {when(s.get('blockTime'))}  {names}{'  FAILED' if s.get('err') else ''}")
    print("\nNothing was signed or sent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
