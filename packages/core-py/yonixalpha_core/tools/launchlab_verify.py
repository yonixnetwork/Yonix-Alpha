"""Checks the Raydium LaunchLab read path (solana.launchlab) against the real
chain (master §7). Read-only: about a hundred RPC reads, nothing signed or sent.

    $C run --rm decision-engine python -m yonixalpha_core.tools.launchlab_verify [--txs 100]

1. Recent LaunchLab transactions -> their TradeEvents.
2. The pools traded, their GlobalConfig and PlatformConfig accounts are
   decoded; layout check: the pool's own total_base_sell / virtual amounts
   equal those in its events (both fixed for a pool on its curve), and its
   config / platform accounts decode with the right discriminator.
3. Every trade is replayed from the pool amounts the event itself reports
   from before the trade, with the event's own fees, and compared exactly:
   - buy exact in: quote in less fees -> base out (a buy larger than the
     rest of the curve fills the rest: base out -> quote in);
   - buy exact out: base out -> quote in less fees;
   - sell exact in: base in -> quote out plus fees;
   - sell exact out: quote out plus fees -> base in;
   and the pool's change (real_*_after - real_*_before) must be the curve
   amounts. Per curve type, as the linear price curve is NOT VERIFIED until
   this passes for it (solana.launchlab refuses to quote it until then).
4. Coverage: curve types, Token-2022 flags and statuses of the pools seen,
   so it is clear which of them quote_buy / quote_sell can serve.

Prints PASS / FAIL per check. Until it passes on the server, LaunchLab quotes
are NOT VERIFIED against the chain, and the venue stays OBSERVE ONLY.
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
from yonixalpha_core.solana import launchlab as ll
from yonixalpha_core.solana.launchlab_layout import PROGRAM_ID
from yonixalpha_core.solana.rpc import RpcManager
from yonixalpha_core.solana.rpc_registry import effective_rpc


def replay(curve_type: int, ev: dict[str, Any]) -> dict[str, Any]:
    """Replays one TradeEvent's curve step (see module doc). Returns the
    comparison: curve_equal (the traded amounts), state_equal (the pool's
    change), with ours / chain for a report."""
    pre = {"virtual_base": ev["virtual_base"], "virtual_quote": ev["virtual_quote"],
           "real_base": ev["real_base_before"], "real_quote": ev["real_quote_before"]}
    fees = ev["protocol_fee"] + ev["platform_fee"] + ev["creator_fee"] + ev["share_fee"]
    amount_in, amount_out = ev["amount_in"], ev["amount_out"]
    d_base = ev["real_base_after"] - ev["real_base_before"]
    d_quote = ev["real_quote_after"] - ev["real_quote_before"]
    if ev["trade_direction"] == ll.BUY:
        net_in = amount_in - fees
        remaining = ev["total_base_sell"] - ev["real_base_before"]
        if ev["exact_in"] and not (amount_out == remaining and ll.curve_buy_exact_in(curve_type, pre, net_in) >= remaining):
            kind, ours, chain = "buy_exact_in", ll.curve_buy_exact_in(curve_type, pre, net_in), amount_out
        else:  # exact out, or an exact-in buy that filled the rest of the curve
            kind = "buy_exact_out" if not ev["exact_in"] else "buy_fill_rest"
            ours, chain = ll.curve_buy_exact_out(curve_type, pre, amount_out), net_in
        state_equal = d_base == amount_out and d_quote == net_in
    else:
        if ev["exact_in"]:
            gross = ll.curve_sell_exact_in(curve_type, pre, amount_in)
            kind, ours, chain = "sell_exact_in", gross - fees, amount_out
            state_equal = -d_base == amount_in and -d_quote == gross
        else:
            gross = amount_out + fees
            kind, ours, chain = "sell_exact_out", ll.curve_sell_exact_out(curve_type, pre, gross), amount_in
            state_equal = -d_base == amount_in and -d_quote == gross
    return {"kind": kind, "curve_equal": ours == chain, "state_equal": state_equal, "ours": ours, "chain": chain}


def check_pool(pool: dict[str, Any], events: list[dict[str, Any]]) -> list[str]:
    """Layout check of a decoded pool against its own events (empty: PASS)."""
    problems = []
    for ev in events:
        if ev["total_base_sell"] != pool["total_base_sell"]:
            problems.append(f"total_base_sell {pool['total_base_sell']} != the event's {ev['total_base_sell']}")
            break
        if pool["status"] == ll.FUND and (ev["virtual_base"], ev["virtual_quote"]) != (pool["virtual_base"], pool["virtual_quote"]):
            problems.append("virtual amounts differ from the event's")
            break
    return problems


async def _accounts(rpc, addresses: list[str]) -> dict[str, bytes | None]:
    out: dict[str, bytes | None] = {}
    for i in range(0, len(addresses), 100):
        chunk = addresses[i:i + 100]
        res = await rpc.call("getMultipleAccounts", [chunk, {"encoding": "base64"}], priority="background")
        for addr, acc in zip(chunk, (res or {}).get("value") or [None] * len(chunk)):
            out[addr] = base64.b64decode(acc["data"][0]) if acc else None
    return out


async def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--txs", type=int, default=100, help="latest program transactions read")
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
        sigs = await rpc.call("getSignaturesForAddress", [PROGRAM_ID, {"limit": a.txs}], priority="background")
        events: list[dict[str, Any]] = []
        for s in reversed(sigs or []):
            if s.get("err"):
                continue
            tx = await rpc.call("getTransaction", [s["signature"], {"encoding": "json", "maxSupportedTransactionVersion": 0}],
                                priority="background")
            events.extend(ll.trade_events(tx or {}))
        by_pool: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for ev in events:
            by_pool[ev["pool_state"]].append(ev)
        print(f"LaunchLab TradeEvents in the latest {len(sigs or [])} program transactions: {len(events)} in {len(by_pool)} pools")
        if not events:
            print("FAIL: no TradeEvent decoded (event layout, or no recent LaunchLab trade)")
            return 1
        raw_pools = await _accounts(rpc, list(by_pool))
        pools, layout_problems = {}, Counter()
        for k, v in raw_pools.items():
            if not v:
                continue
            try:
                pools[k] = ll.decode_account("PoolState", v)
            except ll.LaunchLabError as exc:
                layout_problems[f"PoolState {k}: {exc}"] += 1
        cfg_addrs = sorted({p["global_config"] for p in pools.values()} | {p["platform_config"] for p in pools.values()})
        raw_cfg = await _accounts(rpc, cfg_addrs)
        configs, platforms = {}, {}
        for p in pools.values():
            for addr, name, store in ((p["global_config"], "GlobalConfig", configs), (p["platform_config"], "PlatformConfig", platforms)):
                if addr in store:
                    continue
                try:
                    store[addr] = ll.decode_account(name, raw_cfg.get(addr) or b"")
                except ll.LaunchLabError as exc:
                    layout_problems[f"{name} {addr}: {exc}"] += 1
        coverage = Counter()
        results: dict[int, Counter] = defaultdict(Counter)
        first_mismatch: dict[str, dict[str, Any]] = {}
        for addr, evs in by_pool.items():
            pool = pools.get(addr)
            if pool is None:
                coverage["pool account not found (closed?)"] += 1
                continue
            for problem in check_pool(pool, evs):
                layout_problems[f"pool {addr}: {problem}"] += 1
            config = configs.get(pool["global_config"])
            if config is None:
                continue
            ct = config["curve_type"]
            coverage[f"curve {ll.CURVE_NAMES.get(ct, ct)}"] += 1
            coverage[f"token_program_flag {pool['token_program_flag']}"] += 1
            coverage[f"status {pool['status']}"] += 1
            try:
                ll.check_quotable(pool, config)
                coverage["quotable now"] += 1
            except ll.LaunchLabError as exc:
                coverage[f"not quotable: {exc}"] += 1
            for ev in evs:
                try:
                    r = replay(ct, ev)
                except ll.LaunchLabError as exc:
                    results[ct]["error " + str(exc)] += 1
                    continue
                results[ct]["trades"] += 1
                results[ct][r["kind"]] += 1
                ok = r["curve_equal"] and r["state_equal"]
                results[ct]["equal"] += ok
                if not ok:
                    first_mismatch.setdefault(f"{ll.CURVE_NAMES.get(ct, ct)} {r['kind']}", {**r, "pool": addr})
        verdict = "PASS" if not layout_problems else "FAIL"
        failures += bool(layout_problems)
        print(f"  layout (pools {len(pools)}, configs {len(configs)}, platforms {len(platforms)}): {verdict}")
        for problem, n in layout_problems.most_common(10):
            print(f"    {problem} (x{n})")
        print("  coverage: " + ", ".join(f"{k} {v}" for k, v in sorted(coverage.items())))
        for ct, c in sorted(results.items()):
            n, eq = c["trades"], c["equal"]
            v = "PASS" if n and eq == n else "FAIL"
            failures += v == "FAIL"
            detail = ", ".join(f"{k} {c[k]}" for k in sorted(c) if k not in ("trades", "equal"))
            print(f"  {ll.CURVE_NAMES.get(ct, ct)}: trades replayed {n}, exactly equal {eq}/{n} [{v}] ({detail})")
        for k, r in first_mismatch.items():
            print(f"    first mismatch, {k}: ours {r['ours']} chain {r['chain']} state_equal {r['state_equal']} pool {r['pool']}")
    print("RESULT:", "PASS" if not failures else f"FAIL ({failures})")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
