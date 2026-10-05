"""Checks the Meteora DBC read path (solana.dbc) against the real chain
(master §7). Read-only: a few dozen RPC reads, nothing signed or sent.

    $C run --rm decision-engine python -m yonixalpha_core.tools.dbc_verify [--pools 5] [--swaps 40]

1. Recent DBC transactions -> their swap events (EvtSwap / EvtSwap2) -> the
   most active pools.
2. Each pool's VirtualPool and PoolConfig accounts are decoded (layout:
   discriminator, the pool's config address, curve points ascending, the
   current sqrt price inside [start, migration]).
3. Consecutive swaps of the same pool: the first swap's next_sqrt_price is
   the second swap's starting price. The second swap's curve step is
   replayed from there and compared exactly: next sqrt price and curve
   amount. Exact in and partial fill walk from the chain's own post-fee
   input, exact out from the output (see replay). (The fee itself depends
   on the dynamic-fee state of that moment, not known afterwards; the fee
   math is checked against the SDK in the tests.)

Prints PASS / FAIL per check. Until it passes on the server, DBC quotes are
NOT VERIFIED against the chain, and the venue stays OBSERVE ONLY.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
from collections import Counter, defaultdict
from typing import Any

import httpx

from yonixalpha_core.config import get_settings
from yonixalpha_core.db.base import make_engine, make_session_factory
from yonixalpha_core.solana import dbc
from yonixalpha_core.solana.dbc_layout import PROGRAM_ID
from yonixalpha_core.solana.rpc import RpcManager, RpcRequestError, get_transaction_params
from yonixalpha_core.solana.rpc_registry import effective_rpc
from yonixalpha_core.solana.txversion import UnsupportedTransactionLayout


def check_pool(pool: dict[str, Any], config: dict[str, Any], config_address: str) -> list[str]:
    """Layout sanity of one decoded pool / config pair (empty: PASS)."""
    problems = []
    st = pool["pool_state"]
    if st["config"] != config_address:
        problems.append(f"pool.config {st['config']} != the config account {config_address}")
    pts = [p["sqrt_price"] for p in config["curve"] if p["sqrt_price"]]
    if not pts or any(b <= a for a, b in zip(pts, pts[1:])):
        problems.append("curve sqrt prices are not strictly ascending")
    if pts and not (config["sqrt_start_price"] <= st["sqrt_price"] <= max(pts[-1], config["migration_sqrt_price"])):
        problems.append("current sqrt price outside [start, last curve point]")
    return problems


EXACT_IN, PARTIAL_FILL, EXACT_OUT = 0, 1, 2  # swap2 SwapMode


def replay(config: dict[str, Any], prev: dict[str, Any], ev_name: str, ev: dict[str, Any]) -> dict[str, Any]:
    """Replays the curve part of swap `ev` from the price `prev` left the pool
    at and compares exactly, independent of the fee state of that moment
    (not known after the fact):
      - exact in (swap, swap2 ExactIn) and partial fill: the chain's own
        post-fee input walks the curve; the next sqrt price and the curve
        output (fee on input: the output itself; fee on output: the output
        plus the fees) must match, with nothing left over (a partial fill
        reports the input it consumed);
      - exact out (swap2 ExactOut): the output before an output fee walks the
        curve backwards; the next sqrt price and the curve input must match."""
    res = ev["swap_result"]
    start = prev["swap_result"]["next_sqrt_price"]
    direction = ev["trade_direction"]
    fees_on_input = dbc.fee_mode(config["collect_fee_mode"], direction)
    fees = res["trading_fee"] + res["protocol_fee"] + res["referral_fee"]
    mode = ev["swap_parameters"]["swap_mode"] if ev_name == "EvtSwap2" else EXACT_IN
    if mode == EXACT_OUT:
        curve_out = res["output_amount"] if fees_on_input else res["output_amount"] + fees
        if direction == dbc.BASE_TO_QUOTE:
            curve_in, nxt = dbc.base_to_quote_from_amount_out(config, start, curve_out)
        else:
            curve_in, nxt = dbc.quote_to_base_from_amount_out(config, start, curve_out)
        return {"mode": "exact_out", "next_sqrt_price_equal": nxt == res["next_sqrt_price"],
                "curve_amount_equal": curve_in == res["excluded_fee_input_amount"],
                "ours": curve_in, "chain": res["excluded_fee_input_amount"]}
    if ev_name == "EvtSwap":
        curve_in = res["actual_input_amount"] if fees_on_input else ev["params"]["amount_in"]
    else:
        curve_in = res["excluded_fee_input_amount"] if fees_on_input else res["included_fee_input_amount"]
    if direction == dbc.BASE_TO_QUOTE:
        out, nxt, left = dbc.base_to_quote_from_amount_in(config, start, curve_in)
    else:
        out, nxt, left = dbc.quote_to_base_from_amount_in(config, start, curve_in, config["migration_sqrt_price"])
    expected_out = res["output_amount"] if fees_on_input else res["output_amount"] + fees
    return {"mode": "partial_fill" if mode == PARTIAL_FILL else "exact_in",
            "next_sqrt_price_equal": nxt == res["next_sqrt_price"] and left == 0,
            "curve_amount_equal": out == expected_out, "ours": out, "chain": expected_out}


async def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pools", type=int, default=5)
    ap.add_argument("--swaps", type=int, default=40, help="transactions read per pool")
    a = ap.parse_args(argv)
    settings = get_settings()
    engine = make_engine(settings)
    async with make_session_factory(engine)() as session:
        specs = await effective_rpc(session, settings)
    await engine.dispose()
    if not specs:
        print("no RPC endpoint configured")
        return 1
    failures = 0
    async with httpx.AsyncClient() as http:
        rpc = RpcManager.create(client=http, primary_url=specs[0]["url"])
        rpc.replace_endpoints(specs)

        unreadable: Counter = Counter()

        async def swaps_of(address: str, limit: int) -> list[tuple[int, str, dict[str, Any]]]:
            sigs = await rpc.call("getSignaturesForAddress", [address, {"limit": limit}], priority="background")
            out = []  # chronological: the RPC lists newest first (also within a slot)
            for s in reversed(sigs or []):
                if s.get("err"):
                    continue
                try:  # jsonParsed: every transaction version (v1 exists on mainnet)
                    tx = await rpc.call("getTransaction", get_transaction_params(s["signature"], "jsonParsed"),
                                        priority="background")
                    events = dbc.swap_events(tx or {})
                except (RpcRequestError, UnsupportedTransactionLayout) as exc:
                    unreadable[type(exc).__name__] += 1
                    continue
                for name, ev in events:
                    out.append((tx.get("slot") or 0, name, ev))
            return out

        recent = await swaps_of(PROGRAM_ID, 60)
        by_pool: dict[str, int] = defaultdict(int)
        for _slot, _name, ev in recent:
            by_pool[ev["pool"]] += 1
        print(f"DBC swaps found in the latest program transactions: {len(recent)} in {len(by_pool)} pools")
        if not recent:
            print("FAIL: no swap event decoded (event layout, or no recent DBC swap)")
            return 1
        for pool_addr, _n in sorted(by_pool.items(), key=lambda kv: -kv[1])[:a.pools]:
            accs = await rpc.call("getMultipleAccounts", [[pool_addr], {"encoding": "base64"}], priority="background")
            raw = (accs or {}).get("value", [None])[0]
            if not raw:
                print(f"  {pool_addr}: account not found")
                continue
            try:
                pool = dbc.decode_account("VirtualPool", base64.b64decode(raw["data"][0]))
                cfg_addr = pool["pool_state"]["config"]
                cres = await rpc.call("getMultipleAccounts", [[cfg_addr], {"encoding": "base64"}], priority="background")
                craw = ((cres or {}).get("value") or [None])[0]
                if not craw:
                    raise dbc.DbcError(f"config account {cfg_addr} not found")
                config = dbc.decode_account("PoolConfig", base64.b64decode(craw["data"][0]))
            except dbc.DbcError as exc:
                print(f"  pool {pool_addr}: layout FAIL {exc}")
                failures += 1
                continue
            problems = check_pool(pool, config, cfg_addr)
            print(f"  pool {pool_addr}: layout {'PASS' if not problems else 'FAIL ' + '; '.join(problems)}")
            failures += bool(problems)
            evs = await swaps_of(pool_addr, a.swaps)
            pairs = [(evs[i][2], evs[i + 1][1], evs[i + 1][2]) for i in range(len(evs) - 1)
                     if evs[i][2]["pool"] == pool_addr and evs[i + 1][2]["pool"] == pool_addr]
            ok_price = ok_out = 0
            modes: dict[str, int] = defaultdict(int)
            for prev, name, ev in pairs:
                try:
                    r = replay(config, prev, name, ev)
                except dbc.DbcError as exc:
                    print(f"    swap replay error: {exc}")
                    continue
                ok_price += r["next_sqrt_price_equal"]
                ok_out += r["curve_amount_equal"]
                modes[r["mode"]] += 1
            if pairs:
                verdict = "PASS" if ok_price == ok_out == len(pairs) else "FAIL"
                failures += verdict == "FAIL"
                print(f"    consecutive swaps replayed: {len(pairs)}; next sqrt price equal {ok_price}/{len(pairs)}, "
                      f"curve amount equal {ok_out}/{len(pairs)} [{verdict}] ({dict(modes)})")
            else:
                print("    no consecutive swaps in the sample: quote not checked on this pool")
        if unreadable:
            print(f"  transactions not readable (skipped): {dict(unreadable)}")
    print("RESULT:", "PASS" if not failures else f"FAIL ({failures})")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
