"""PumpSwap window check (read-only): did other traders' PumpSwap sells of
one pool succeed while ours failed, and which buyback recipient did they
pass? Also prints the PumpSwap sell accounts of our own last confirmed
sells of the mint, next to theirs.

    $C run --rm paper-trading python -m yonixalpha_core.tools.pumpswap_window_check \\
        --pool <pool> --mint <mint> [--since 2026-10-06T22:00] [--until 2026-10-07T08:30]

Written 2026-10-07: from 22:44 to 07:44 UTC every sell of ours for one
pool (7wmm) failed with 6053 BuybackFeeRecipientNotAuthorized on the same
RPC endpoint that had confirmed our sells minutes earlier; no Pump program
upgrade or GlobalConfig admin change happened in between.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
from collections import Counter
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import select

from yonixalpha_core.config import get_settings
from yonixalpha_core.db.base import make_engine, make_session_factory
from yonixalpha_core.db.models import ExecutionOrder
from yonixalpha_core.solana import pump_tx as p
from yonixalpha_core.solana.rpc import RpcManager, get_transaction_params
from yonixalpha_core.solana.rpc_registry import effective_rpc
from yonixalpha_core.tools.pumpswap_sell_check import amm_instructions


def parse_time(text: str) -> datetime:
    t = datetime.fromisoformat(text)
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def spread(items: list, n: int) -> list:
    """At most n items, evenly spaced over the list (first and last kept)."""
    if len(items) <= n:
        return items
    step = (len(items) - 1) / (n - 1)
    return [items[round(i * step)] for i in range(n)]


def hour(ts: int) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%m-%d %H:00")


def tail(accounts: list[str], buyback: set[str]) -> str:
    """The accounts after the 21 fixed ones; * marks a GlobalConfig buyback recipient."""
    return ", ".join(a[:8] + ("*" if a in buyback else "") for a in accounts[21:]) or "-"


async def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pool", required=True)
    ap.add_argument("--mint", required=True)
    ap.add_argument("--since", default="2026-10-06T22:00")
    ap.add_argument("--until", default="2026-10-07T08:30")
    ap.add_argument("--pages", type=int, default=30, help="signature pages of 1000 read, newest first")
    ap.add_argument("--reads", type=int, default=150, help="transactions inside the window read in full")
    a = ap.parse_args(argv)
    since, until = parse_time(a.since), parse_time(a.until)
    settings = get_settings()
    engine = make_engine(settings)
    async with make_session_factory(engine)() as session:
        specs = await effective_rpc(session, settings)
        ours = list((await session.execute(
            select(ExecutionOrder.signature, ExecutionOrder.created_at).where(
                ExecutionOrder.mode == "LIVE", ExecutionOrder.side == "SELL", ExecutionOrder.mint == a.mint,
                ExecutionOrder.status == "CONFIRMED", ExecutionOrder.signature.is_not(None))
            .order_by(ExecutionOrder.created_at.desc()).limit(3))).all())
    await engine.dispose()
    if not specs:
        print("no RPC endpoint configured")
        return 1
    async with httpx.AsyncClient() as http:
        rpc = RpcManager.create(client=http, primary_url=specs[0]["url"])
        rpc.replace_endpoints(specs)
        cfg_raw = base64.b64decode((await rpc.call("getAccountInfo", [p.amm_global_config_pda(), {"encoding": "base64"}]))
                                   ["value"]["data"][0])
        buyback = set(p.decode_amm_global_config(cfg_raw).buyback_fee_recipients)
        print(f"window {since:%Y-%m-%d %H:%M} to {until:%Y-%m-%d %H:%M} UTC, pool {a.pool}")

        inside: list[dict] = []
        before, oldest, pages = None, None, 0
        while pages < a.pages:
            opts: dict[str, Any] = {"limit": 1000}
            if before:
                opts["before"] = before
            try:
                got = await rpc.call("getSignaturesForAddress", [a.pool, opts]) or []
            except Exception as exc:  # noqa: BLE001
                print(f"signatures page {pages + 1}: error {type(exc).__name__}: {str(exc)[:120]}")
                break
            pages += 1
            if not got:
                break
            for s in got:
                t = s.get("blockTime")
                if t is not None and since.timestamp() <= t <= until.timestamp():
                    inside.append(s)
            before, oldest = got[-1]["signature"], got[-1].get("blockTime")
            if oldest is not None and oldest < since.timestamp():
                break
        reached = datetime.fromtimestamp(oldest, UTC).strftime("%Y-%m-%d %H:%M") if oldest else "?"
        print(f"signature pages read: {pages}, oldest reached {reached} UTC; transactions inside the window: {len(inside)}")
        if oldest is not None and oldest > since.timestamp():
            print("  NOTE: the window start was not reached; raise --pages")
        per_hour: Counter = Counter()
        for s in inside:
            per_hour[(hour(s["blockTime"]), "failed" if s.get("err") else "ok")] += 1
        for (h, k), c in sorted(per_hour.items()):
            print(f"  {h}  {k:6s} {c}")

        sells_ok: Counter = Counter()
        recipients: Counter = Counter()
        tails: Counter = Counter()
        others: dict[str, str] = {}
        ok_inside = [s for s in inside if not s.get("err")]
        for s in spread(sorted(ok_inside, key=lambda x: x["blockTime"]), a.reads):
            try:
                tx = await rpc.call("getTransaction", get_transaction_params(s["signature"], "jsonParsed"))
            except Exception:  # noqa: BLE001
                continue
            for name, accounts, _raw in amm_instructions(tx or {}):
                if name == "sell":
                    sells_ok[(hour(s["blockTime"]), len(accounts))] += 1
                    recipients[accounts[-2] if len(accounts) > 22 else "-"] += 1
                    tails[tail(accounts, buyback)] += 1
                elif name.startswith("other:") and name not in others:
                    others[name] = datetime.fromtimestamp(s["blockTime"], UTC).strftime("%m-%d %H:%M:%S")
        print(f"\nsuccessful transactions read in full: {min(len(ok_inside), a.reads)} of {len(ok_inside)}")
        print("successful PumpSwap sells inside the window (hour, accounts):")
        for (h, n), c in sorted(sells_ok.items()):
            print(f"  {h}  {n} accounts: {c}")
        if not sells_ok:
            print("  none found")
        print("buyback recipient they passed (account before last; * = in GlobalConfig):")
        for r, c in recipients.most_common():
            print(f"  {r}{' *' if r in buyback else ''}: {c}")
        print("their tail after the 21 fixed accounts:")
        for t, c in tails.most_common(5):
            print(f"  {t}: {c}")
        print("other PumpSwap instructions inside the window (first seen):", others or "none")

        print("\nour last confirmed sells of this mint:")
        for sig, at in ours:
            try:
                tx = await rpc.call("getTransaction", get_transaction_params(sig, "jsonParsed"))
            except Exception as exc:  # noqa: BLE001
                print(f"  {at:%m-%d %H:%M:%S} {sig[:16]}... not read ({type(exc).__name__})")
                continue
            for name, accounts, _raw in amm_instructions(tx or {}):
                if name == "sell":
                    print(f"  {at:%m-%d %H:%M:%S} {len(accounts)} accounts; tail {tail(accounts, buyback)}")
        if not ours:
            print("  none recorded")
    print("\nNothing was signed or sent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
