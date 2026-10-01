"""Who the stored EVM trades are attributed to (read-only, master §12 / M9).

Launchpad events name the contract that called the launchpad. Through a router
that is the router, not the wallet (pons-terminal's TradeRouter calls
curve.buy(..., recipient=user), so CurveBuy.buyer is the router). Wallet
profiles, smart-wallet discovery and copy detection read `evm_trades.trader`,
so a router there looks like one very active "wallet".

Measured per chain and launchpad over the last --days days:
  - trades, distinct traders, share of the busiest traders;
  - for each of the --top busiest traders: contract or wallet (eth_getCode),
    a known router label, and (Pons curves) how often recipient != buyer;
  - Pons curve buys whose recipient differs from the buyer, in total.

Nothing is written. On the server, in /opt/yonixalpha:

    docker compose --env-file .env -f infra/docker/docker-compose.yml \\
        -f infra/docker/docker-compose.prod.yml \\
        exec -T data-evm python -m yonixalpha_core.tools.trader_attribution [--days 3] [--top 12]
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select


async def report(session, get_code, days: float, top_n: int, now: datetime | None = None) -> list[str]:
    """Report lines. `get_code(chain, address)` returns the address's code."""
    from yonixalpha_core.chains.evm.known_routers import router_label
    from yonixalpha_core.db.models import EvmTrade

    since = (now or datetime.now(timezone.utc)) - timedelta(days=days)
    out: list[str] = []
    recipient_differs = func.lower(EvmTrade.extra["recipient"].astext) != func.lower(EvmTrade.trader)
    groups = (await session.execute(
        select(EvmTrade.chain, EvmTrade.launchpad, func.count(), func.count(func.distinct(EvmTrade.trader)))
        .where(EvmTrade.at >= since).group_by(EvmTrade.chain, EvmTrade.launchpad)
        .order_by(EvmTrade.chain, func.count().desc()))).all()
    if not groups:
        return [f"no EVM trades stored in the last {days:g} days"]
    for chain, launchpad, n, distinct in groups:
        scope = (EvmTrade.chain == chain, EvmTrade.launchpad == launchpad, EvmTrade.at >= since)
        out.append(f"== {chain} / {launchpad}: {n} trades, {distinct} distinct traders (last {days:g} days)")
        top = (await session.execute(select(EvmTrade.trader, func.count()).where(*scope)
                                     .group_by(EvmTrade.trader).order_by(func.count().desc()).limit(top_n))).all()
        for trader, k in top:
            try:
                code = await get_code(chain, trader)
                kind = "CONTRACT" if code and code not in ("0x", "0x0") else "wallet"
            except Exception as exc:  # noqa: BLE001 - reported, the report goes on
                kind = f"unknown ({type(exc).__name__})"
            label = router_label(chain, trader)
            line = f"    {trader}  {k:>6} trades ({k / n:.1%})  {kind}" + (f"  [{label}]" if label else "")
            if launchpad == "pons_v2":
                diff = (await session.execute(select(func.count()).where(
                    *scope, EvmTrade.trader == trader, EvmTrade.is_buy.is_(True), recipient_differs))).scalar_one()
                line += f"  buys with recipient != buyer: {diff}"
            out.append(line)
        if launchpad == "pons_v2":
            buys, diff = (await session.execute(select(func.count(), func.count().filter(recipient_differs))
                                                .where(*scope, EvmTrade.is_buy.is_(True)))).one()
            out.append(f"    curve buys: {buys}; recipient differs from buyer (router-mediated): {diff}"
                       + (f" ({diff / buys:.1%})" if buys else ""))
    return out


async def main() -> int:
    from yonixalpha_core.chains.evm import rpc_registry
    from yonixalpha_core.config import get_settings
    from yonixalpha_core.db.base import make_engine, make_session_factory

    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--days", type=float, default=3)
    ap.add_argument("--top", type=int, default=12)
    args = ap.parse_args()
    settings = get_settings()
    engine = make_engine(settings)
    rpcs: dict = {}
    try:
        async with make_session_factory(engine)() as session:
            async def get_code(chain: str, address: str) -> str:
                if chain not in rpcs:
                    rpcs[chain] = await rpc_registry.rpc_for(session, settings, chain)
                return await rpcs[chain].get_code(address)

            for line in await report(session, get_code, args.days, args.top):
                print(line)
    finally:
        for r in rpcs.values():
            await r.aclose()
        await engine.dispose()
    print("\nA CONTRACT among the busiest traders is a router or bot contract credited as a wallet: wallet profiles "
          "and copy detection see it, not the wallets behind it. Send this output back before attribution changes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
