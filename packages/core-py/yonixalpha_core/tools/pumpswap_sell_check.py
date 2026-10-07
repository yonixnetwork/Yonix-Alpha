"""PumpSwap sell check (read-only): how successful PumpSwap sells by other
traders look on chain now, next to the sell our native builder builds for
the same seller, pool and a smaller amount, which is then simulated as that
seller. Nothing is signed or sent (a simulation needs no signature).

    $C run --rm paper-trading python -m yonixalpha_core.tools.pumpswap_sell_check [--txs 150]

Written 2026-10-07: from 2026-10-06 22:44 UTC every PumpSwap sell of ours
failed with 6053 BuybackFeeRecipientNotAuthorized, although the recipient
came from the GlobalConfig list. Account by account, the comparison shows
what the program now expects.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import struct
from collections import Counter
from decimal import Decimal
from typing import Any

import httpx

from yonixalpha_core.config import get_settings
from yonixalpha_core.db.base import make_engine, make_session_factory
from yonixalpha_core.solana import pump_tx as p
from yonixalpha_core.solana import venue as venues
from yonixalpha_core.solana.codec import b58decode
from yonixalpha_core.solana.pumpportal import TradeRequest
from yonixalpha_core.solana.rpc import RpcManager, get_transaction_params
from yonixalpha_core.solana.rpc_registry import effective_rpc
from yonixalpha_core.solana.tx_builders import BuildError, NativePumpBuilder
from yonixalpha_core.solana.txguard import GuardExpectation
from yonixalpha_core.solana.txversion import instructions

NAMES = {p.BUY.hex(): "buy", p.SELL.hex(): "sell", "c62e1552b4d9e870": "buy_exact_quote_in"}


def amm_instructions(tx: dict[str, Any]) -> list[tuple[str, list[str], bytes]]:
    """(name, accounts, data) of every PumpSwap instruction, outer and inner."""
    out = []
    for prog, accounts, data in instructions(tx):
        if prog == p.PUMP_AMM:
            raw = b58decode(data)
            out.append((NAMES.get(raw[:8].hex(), "other:" + raw[:8].hex()), accounts, raw))
    return out


def holder_after(tx: dict[str, Any], owner: str, mint: str) -> int:
    """The owner's raw balance of `mint` after the transaction (0 if none)."""
    for b in (tx.get("meta") or {}).get("postTokenBalances") or []:
        if b.get("owner") == owner and b.get("mint") == mint:
            return int(((b.get("uiTokenAmount") or {}).get("amount")) or 0)
    return 0


def compare(real: list[str], ours: list[str], buyback: set[str]) -> list[str]:
    """Side by side, account by account; * marks a GlobalConfig buyback recipient."""
    def tag(a: str | None) -> str:
        return "-" if a is None else a + (" *" if a in buyback else "")
    lines = []
    for i in range(max(len(real), len(ours))):
        r = real[i] if i < len(real) else None
        o = ours[i] if i < len(ours) else None
        lines.append(f"  {i:2d} {'same' if r == o else 'DIFF'}  chain {tag(r):50s} ours {tag(o)}")
    return lines


async def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--txs", type=int, default=150, help="latest PumpSwap transactions read")
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
        cfg_raw = base64.b64decode((await rpc.call("getAccountInfo", [p.amm_global_config_pda(), {"encoding": "base64"}]))
                                   ["value"]["data"][0])
        cfg = p.decode_amm_global_config(cfg_raw)
        buyback = set(cfg.buyback_fee_recipients)
        print(f"GlobalConfig {len(cfg_raw)} bytes; buyback recipients: {sorted(buyback)}")
        sigs = await rpc.call("getSignaturesForAddress", [p.PUMP_AMM, {"limit": a.txs}])
        kinds, errors, sells, programs = Counter(), Counter(), [], Counter()
        read = 0
        for sg in sigs or []:
            if sg.get("err"):
                continue
            try:
                tx = await rpc.call("getTransaction", get_transaction_params(sg["signature"], "jsonParsed"))
            except Exception as exc:  # noqa: BLE001
                errors[type(exc).__name__] += 1
                continue
            if not tx:
                errors["no transaction returned"] += 1
                continue
            read += 1
            for prog, _accounts, _data in instructions(tx):
                programs[prog] += 1
            for name, accounts, raw in amm_instructions(tx):
                kinds[(name, len(accounts))] += 1
                if name == "sell" and len(accounts) > 3:
                    left = holder_after(tx, accounts[1], accounts[3])
                    if left > 0:
                        sells.append((sg["signature"], accounts, raw, left))
        print(f"successful PumpSwap transactions read: {read}; errors: {dict(errors)}")
        for k, c in kinds.most_common(12):
            print(f"  {k[0]} with {k[1]} accounts: {c}")
        print("programs with raw instructions in them:", dict(programs.most_common(8)))
        if not sells:
            print("no successful sell whose seller still holds tokens: nothing to compare")
            return 2
        builder = NativePumpBuilder(rpc)
        for sig, real, raw, left in sells[:2]:
            user, mint = real[1], real[3]
            print(f"\nsell {sig}: seller {user}, mint {mint}, sold {struct.unpack_from('<Q', raw, 8)[0]} raw, holds {left} raw")
            v = await venues.resolve(rpc, mint, side="sell")
            if v.kind != venues.PUMP_AMM or v.decimals is None:
                print(f"  our venue resolver says {v.kind}: {v.reason}; not compared")
                continue
            amount = left // 2 or left
            req = TradeRequest(user, "sell", mint, str(Decimal(amount) / Decimal(10) ** v.decimals), False, Decimal("20"),
                               Decimal("0.0001"), "pump-amm")
            exp = GuardExpectation(wallet=user, mint=mint, side="sell", venue=v.kind, max_tokens_in=amount)
            try:
                built = await builder.build(req, v, exp, user)
            except BuildError as exc:
                print(f"  our builder: BUILD FAILED {exc}")
                continue
            keys = [str(k) for k in built.tx.message.account_keys]
            ours = next([keys[i] for i in ix.accounts] for ix in built.tx.message.instructions
                        if keys[ix.program_id_index] == p.PUMP_AMM and bytes(ix.data)[:8] == p.SELL)
            print("\n".join(compare(real, ours, buyback)))
            sim = await rpc.call("simulateTransaction", [base64.b64encode(bytes(built.tx)).decode(),
                                                         {"encoding": "base64", "sigVerify": False,
                                                          "replaceRecentBlockhash": True, "commitment": "confirmed"}])
            value = (sim or {}).get("value") or {}
            print("  our sell simulated as this seller:", "OK" if value.get("err") is None else f"FAILED {value.get('err')}")
            for line in (value.get("logs") or [])[-6:]:
                print("     ", line)
    print("\nNothing was signed or sent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
