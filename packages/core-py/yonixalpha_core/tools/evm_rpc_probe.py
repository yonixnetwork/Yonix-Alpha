"""Which EVM RPC endpoints can run launchpad discovery (read-only).

Discovery reads launchpad events with eth_getLogs. Many public nodes answer
eth_blockNumber but refuse eth_getLogs (seen on the server:
bsc-dataseed.binance.org "limit exceeded", bsc-rpc.publicnode.com HTTP 403),
so an endpoint is only useful for discovery if it serves logs. Each endpoint
gets the dashboard's TEST CONNECTION (chains/evm/rpc_registry.test_evm_rpc):
eth_chainId, eth_blockNumber and eth_getLogs over 10 / 100 / 1000 / 2000
blocks of the chain's busiest launchpad contract, ending a few blocks under
the head. An endpoint that accepts the request serves logs even when the
window holds none.

Endpoints tested: the configured ones (dashboard RPC Providers, then
BSC_RPC_URLS / ROBINHOOD_RPC_URLS, then the built-in public ones; printed
redacted) and a few public endpoints listed on chainlist.org as candidates. The candidates are NOT verified by this
project: a result here is one test from this server at this time, and a
public endpoint can change its limits without notice.

On the server, in /opt/yonixalpha:

    docker compose --env-file .env -f infra/docker/docker-compose.yml \\
        -f infra/docker/docker-compose.prod.yml \\
        run --rm decision-engine python -m yonixalpha_core.tools.evm_rpc_probe [--chain bsc]
"""

from __future__ import annotations

import argparse
import asyncio
import httpx

from yonixalpha_core.chains.base import Chain
from yonixalpha_core.chains.registry import CHAINS
from yonixalpha_core.redact import redact_url

CANDIDATES = {
    "bsc": ("https://bsc-dataseed1.defibit.io", "https://bsc-dataseed1.ninicoin.io", "https://bsc.drpc.org",
            "https://1rpc.io/bnb", "https://binance.llamarpc.com", "https://bsc.meowrpc.com"),
    "robinhood": (),
}


async def main() -> int:
    from yonixalpha_core.chains.evm import rpc_registry
    from yonixalpha_core.config import get_settings
    from yonixalpha_core.db.base import make_engine, make_session_factory

    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--chain", choices=rpc_registry.EVM_CHAINS, action="append")
    args = ap.parse_args()
    settings = get_settings()
    engine = make_engine(settings)
    serves_logs = 0
    try:
        async with make_session_factory(engine)() as session, httpx.AsyncClient(
                timeout=10.0, headers={"user-agent": "yonixalpha-rpc-probe"}) as client:
            for chain in args.chain or rpc_registry.EVM_CHAINS:
                spec = CHAINS[Chain(chain)]
                rows = await rpc_registry.endpoints(session, settings, chain)
                groups = [(f"{r['source']}{'' if r['enabled'] else ', disabled'}", r["url"]) for r in rows if r.get("url")]
                groups += [("candidate (chainlist, NOT VERIFIED)", u) for u in CANDIDATES[chain]]
                print(f"\n== {spec.name} (chain id {spec.evm_chain_id}), logs probe on "
                      f"{rpc_registry.LOGS_PROBE_CONTRACT[chain]}")
                seen: set[str] = set()
                for label, url in groups:
                    if url in seen:
                        continue
                    seen.add(url)
                    r = await rpc_registry.test_evm_rpc(client, url, chain)
                    ok = r["status"] == rpc_registry.CONNECTED
                    serves_logs += ok
                    status = "SERVES LOGS" if ok else ("NO LOGS" if r["status"] == rpc_registry.NO_LOGS else r["status"])
                    logs = ", ".join(f"{s} blocks {v}" for s, v in r["logs"].items())
                    print(f"   {status:<14} {redact_url(url):<45} [{label}] {r['detail']}" + (f" ({logs})" if logs else ""))
    finally:
        await engine.dispose()
    print("\nAdd an endpoint marked SERVES LOGS in the dashboard: RPC & Data Providers -> ADD RPC, chain BSC or"
          "\nRobinhood Chain (applied without a restart). Public endpoints change limits without notice; a keyed"
          "\nprovider on a paid plan is steadier (free tiers limit eth_getLogs to a few blocks).")
    return 0 if serves_logs else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
