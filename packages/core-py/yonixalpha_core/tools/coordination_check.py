"""Checks the launch-coordination assumptions against the real chains (read-only).

What this sandboxed code could not verify, measured on the server:

  1. Which entrypoints real Pons V2 launches use: the selector of each recent
     launch transaction (factory.launchToken, the exemption-list overload,
     launchTokenFor, the launchAndBuy router, or something unrecognised), and
     how many declared exemption lists are non-empty.
  2. Whether the deployed curves expose currentSnipeTaxBps(address) and the
     node serves it at a past block (needed to confirm "special treatment").
  3. Whether the funding explorer answers (Robinhood Chain Blockscout; BSC
     Etherscan API V2 when ETHERSCAN_API_KEY is set).
  4. One full assessment per chain of the most recent launch, with its
     findings, so the result can be compared with the explorer by hand.

Nothing is written except the explorer funder cache (evm_wallet_funders).
On the server, in /opt/yonixalpha:

    docker compose --env-file .env -f infra/docker/docker-compose.yml \\
        -f infra/docker/docker-compose.prod.yml \\
        exec -T data-evm python -m yonixalpha_core.tools.coordination_check [--launches 30]
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone

import httpx
from sqlalchemy import desc, select

from yonixalpha_core import launch_coordination as lc


async def main() -> int:
    from yonixalpha_core.chains.evm import adapter_for, rpc_registry
    from yonixalpha_core.config import get_settings
    from yonixalpha_core.db.base import make_engine, make_session_factory
    from yonixalpha_core.db.models import EvmToken, EvmTrade

    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--launches", type=int, default=30)
    args = ap.parse_args()
    settings = get_settings()
    engine = make_engine(settings)
    try:
        async with make_session_factory(engine)() as session, httpx.AsyncClient(timeout=15.0) as client:
            cfg = await lc.load_config(session)
            # 1. Pons V2 launch entrypoints.
            rpc = await rpc_registry.rpc_for(session, settings, "robinhood")
            rows = (await session.execute(select(EvmToken).where(
                EvmToken.chain == "robinhood", EvmToken.launchpad == "pons_v2", EvmToken.created_tx.is_not(None))
                .order_by(desc(EvmToken.created_at)).limit(args.launches))).scalars().all()
            via, sels, lists, sizes = Counter(), Counter(), 0, []
            for r in rows:
                tx = await rpc.call("eth_getTransactionByHash", [r.created_tx])
                d = lc.decode_pons_v2_launch((tx or {}).get("input") or "")
                via[d.get("via") or "UNRECOGNISED"] += 1
                sels[d["selector"]] += 1
                if d.get("declared"):
                    lists += 1
                    sizes.append(len(d["declared"]))
            print(f"[1] Pons V2: last {len(rows)} launches by entrypoint: {dict(via)}")
            print(f"    selectors: {dict(sels)}")
            print(f"    launches with a non-empty exemption list: {lists}" + (f" (sizes {sorted(sizes)})" if sizes else ""))

            # 2. currentSnipeTaxBps at a past block.
            snipe_row = next((r for r in rows if (r.venue or {}).get("curve")), None)
            if snipe_row is None:
                print("[2] no Pons V2 launch with a known curve: snipe-tax view NOT VERIFIED")
            else:
                first = (await session.execute(select(EvmTrade).where(
                    EvmTrade.chain == "robinhood", EvmTrade.token == snipe_row.token, EvmTrade.is_buy.is_(True))
                    .order_by(EvmTrade.at).limit(1))).scalar_one_or_none()
                block = first.block if first is not None and first.block else snipe_row.created_block
                holder = ((first.extra or {}).get("recipient") or first.trader) if first is not None else lc.SNIPE_REFERENCE
                r = await lc._snipe(rpc, snipe_row.venue["curve"], holder.lower(), block)
                print(f"[2] currentSnipeTaxBps on {snipe_row.token} at block {block}: {r}")

            # 3. Explorers.
            probe = (await session.execute(select(EvmTrade.trader).where(EvmTrade.chain == "robinhood")
                                           .order_by(desc(EvmTrade.at)).limit(1))).scalar()
            if probe:
                f = await lc.first_funding(client, "robinhood", probe.lower(), None)
                print(f"[3] Robinhood Blockscout first funding of {probe}: {f.get('status')} {f.get('funder') or ''} "
                      f"{f.get('detail') or ''}")
            bprobe = (await session.execute(select(EvmTrade.trader).where(EvmTrade.chain == "bsc")
                                            .order_by(desc(EvmTrade.at)).limit(1))).scalar()
            if bprobe:
                f = await lc.first_funding(client, "bsc", bprobe.lower(), settings.ETHERSCAN_API_KEY)
                print(f"[3] BSC Etherscan first funding of {bprobe}: {f.get('status')} {f.get('funder') or ''} "
                      f"{f.get('detail') or f.get('source') or ''}")

            # 4. One assessment per chain.
            for chain in ("robinhood", "bsc"):
                crpc = rpc if chain == "robinhood" else await rpc_registry.rpc_for(session, settings, chain)
                row = (await session.execute(select(EvmToken).where(
                    EvmToken.chain == chain, EvmToken.created_tx.is_not(None))
                    .order_by(desc(EvmToken.created_at)).offset(5).limit(1))).scalar_one_or_none()
                if row is None:
                    continue
                res = await lc.assess(session, adapter_for(row.launchpad, crpc), row, datetime.now(timezone.utc), cfg,
                                      client=client, etherscan_key=settings.ETHERSCAN_API_KEY)
                print(f"[4] {chain} {row.launchpad} {row.token}: {res['status']} -> {res['action']}")
                for c in res["checks"]:
                    print(f"      {c['check']:<30} {c['status']:<15} {c['message'][:110]}")
                await session.commit()  # the funder cache only
    finally:
        await engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
