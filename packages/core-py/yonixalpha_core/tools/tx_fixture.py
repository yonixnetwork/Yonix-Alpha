"""Capture a real transaction as the RPC renders it, to build versioned
regression fixtures from mainnet data instead of inventing layouts.
Read-only (getSignaturesForAddress + getTransaction); public chain data.

    docker compose --env-file .env -f infra/docker/docker-compose.yml \\
        -f infra/docker/docker-compose.prod.yml run --rm paper-trading \\
        python -m yonixalpha_core.tools.tx_fixture [--version 1] [--address <program>] [--signature SIG] [--encoding jsonParsed]

Prints one transaction of the requested version (scanning the address's
recent signatures), with every endpoint's answer for it.
"""

import argparse
import asyncio
import json

import httpx

from yonixalpha_core.config import get_settings
from yonixalpha_core.db.base import make_engine, make_session_factory
from yonixalpha_core.redact import redact_url
from yonixalpha_core.solana.rpc import get_transaction_params
from yonixalpha_core.solana.rpc_registry import effective_rpc

PUMP_AMM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"


async def _call(http, url: str, method: str, params: list):
    r = await http.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=15)
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, {"error": f"HTTP {r.status_code}, not JSON"}


async def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", default="1", help='"legacy", 0 or 1')
    ap.add_argument("--address", default=PUMP_AMM)
    ap.add_argument("--signature")
    ap.add_argument("--encoding", default="jsonParsed", choices=("json", "jsonParsed"))
    ap.add_argument("--scan", type=int, default=100)
    a = ap.parse_args(argv)
    settings = get_settings()
    engine = make_engine(settings)
    async with make_session_factory(engine)() as s:
        specs = await effective_rpc(s, settings)
    await engine.dispose()
    if not specs:
        print("no RPC endpoint configured")
        return 1
    async with httpx.AsyncClient() as http:
        url = specs[0]["url"]
        sigs = [a.signature] if a.signature else [
            x["signature"] for x in ((await _call(http, url, "getSignaturesForAddress", [a.address, {"limit": a.scan}]))[1]
                                     .get("result") or [])]
        found = None
        for sig in sigs:
            _, body = await _call(http, url, "getTransaction", get_transaction_params(sig, encoding=a.encoding))
            tx = body.get("result")
            if isinstance(tx, dict) and str(tx.get("version", "legacy")) == str(a.version):
                found = (sig, tx)
                break
        if found is None:
            print(f"no version-{a.version} transaction among {len(sigs)} recent signatures of {a.address}")
            return 2
        sig, tx = found
        print(f"# version {a.version} transaction {sig} ({a.encoding}), from {redact_url(url)}")
        print(json.dumps(tx, indent=1))
        print("\n# every endpoint's answer for the same signature:")
        for spec in specs:
            code, body = await _call(http, spec["url"], "getTransaction", get_transaction_params(sig, encoding=a.encoding))
            res = body.get("result")
            print(f"#   {spec['label']:14} HTTP {code}: " + (f"version {res.get('version')}" if isinstance(res, dict)
                                                          else f"error {json.dumps(body.get('error'))[:200]}"))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
