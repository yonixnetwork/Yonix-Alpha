"""Which EVM RPC endpoints can run launchpad discovery (read-only).

Discovery reads launchpad events with eth_getLogs. Many public nodes answer
eth_blockNumber but refuse eth_getLogs (seen on the server:
bsc-dataseed.binance.org "limit exceeded", bsc-rpc.publicnode.com HTTP 403),
so an endpoint is only useful for discovery if it serves logs. For each
endpoint this checks eth_chainId, eth_blockNumber and eth_getLogs over the last
10 / 100 / 1000 / 2000 blocks of one launchpad contract.

Endpoints tested: the configured ones (BSC_RPC_URLS / ROBINHOOD_RPC_URLS,
printed redacted), the built-in public ones, and a few public endpoints listed
on chainlist.org as candidates. The candidates are NOT verified by this
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
from typing import Any

import httpx

from yonixalpha_core.chains.base import Chain
from yonixalpha_core.chains.registry import CHAINS, LAUNCHPADS
from yonixalpha_core.redact import redact_text, redact_url

CANDIDATES = {
    "bsc": ("https://bsc-dataseed1.defibit.io", "https://bsc-dataseed1.ninicoin.io", "https://bsc.drpc.org",
            "https://1rpc.io/bnb", "https://binance.llamarpc.com", "https://bsc.meowrpc.com"),
    "robinhood": (),
}
SPANS = (10, 100, 1000, 2000)  # 2000: the span the service asks for


async def _rpc(client: httpx.AsyncClient, url: str, method: str, params: list | None = None) -> Any:
    r = await client.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []})
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    body = r.json()
    if isinstance(body, dict) and body.get("error"):
        err = body["error"]
        raise RuntimeError(str(err.get("message", err) if isinstance(err, dict) else err)[:120])
    return body.get("result")


async def probe(client: httpx.AsyncClient, url: str, chain_id: int, contract: str | None) -> dict[str, Any]:
    out: dict[str, Any] = {"url": redact_url(url), "chain": None, "head": None, "logs": {}}
    try:
        cid = int(await _rpc(client, url, "eth_chainId"), 16)
        out["chain"] = "OK" if cid == chain_id else f"WRONG ({cid})"
        if cid != chain_id:
            return out
        head = int(await _rpc(client, url, "eth_blockNumber"), 16)
        out["head"] = head
    except Exception as exc:  # noqa: BLE001 - reported per endpoint
        out["error"] = redact_text(f"{type(exc).__name__}: {exc}", [url])[:160]
        return out
    for span in SPANS if contract else ():
        try:
            logs = await _rpc(client, url, "eth_getLogs",
                              [{"address": contract, "fromBlock": hex(max(0, head - span + 1)), "toBlock": hex(head)}])
            out["logs"][span] = f"OK ({len(logs or [])} logs)"
        except Exception as exc:  # noqa: BLE001
            out["logs"][span] = "REFUSED: " + redact_text(str(exc), [url])[:80]
    return out


async def main() -> int:
    from yonixalpha_core.config import get_settings

    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--chain", choices=("bsc", "robinhood"), action="append")
    args = ap.parse_args()
    settings = get_settings()
    serves_logs = 0
    async with httpx.AsyncClient(timeout=10.0, headers={"user-agent": "yonixalpha-rpc-probe"}) as client:
        for chain in args.chain or ("bsc", "robinhood"):
            spec = CHAINS[Chain(chain)]
            contract = next((a for lp in LAUNCHPADS.values() if lp.chain == spec.chain for a in lp.contracts.values()), None)
            configured = [u.strip() for u in (getattr(settings, f"{chain.upper()}_RPC_URLS", None) or "").split(",")
                          if u.strip()]
            groups = (("configured", configured), ("built-in public", list(spec.public_rpc)),
                      ("candidate (chainlist, NOT VERIFIED)", list(CANDIDATES[chain])))
            print(f"\n== {spec.name} (chain id {spec.evm_chain_id}), logs probe on {contract}")
            seen: set[str] = set()
            for label, urls in groups:
                for url in urls:
                    if url in seen:
                        continue
                    seen.add(url)
                    r = await probe(client, url, spec.evm_chain_id, contract)
                    logs = ", ".join(f"{s} blocks {v}" for s, v in r["logs"].items())
                    ok = bool(r["logs"]) and all(v.startswith("OK") for v in r["logs"].values())
                    serves_logs += ok
                    status = "SERVES LOGS" if ok else ("NO LOGS" if r["head"] is not None else "DOWN")
                    detail = r.get("error") or "chain {} head {}; {}".format(r["chain"], r["head"], logs)
                    print(f"   {status:<12} {r['url']:<45} [{label}] {detail}")
    print("\nAn endpoint marked SERVES LOGS can be added in Settings -> Providers (BSC_RPC_URLS / ROBINHOOD_RPC_URLS);"
          "\nthen press TEST CONNECTION. Public endpoints change limits without notice; a keyed provider is steadier.")
    return 0 if serves_logs else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
