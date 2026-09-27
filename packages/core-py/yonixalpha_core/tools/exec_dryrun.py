"""Execution dry run: resolve the venue for a mint from chain state, build
the transaction exactly as live execution would, run the transaction guard,
and simulate it on the configured RPC — WITHOUT signing or sending
anything. This is how the native Pump / PumpSwap builder (and the Jupiter
path) is verified against the real chain: the simulation runs the actual
on-chain programs against the actual accounts.

On the server, in /opt/yonixalpha:

    docker compose --env-file .env -f infra/docker/docker-compose.yml \\
        -f infra/docker/docker-compose.prod.yml run --rm paper-trading \\
        python -m yonixalpha_core.tools.exec_dryrun <MINT> [--sol 0.01] [--sell-tokens N] [--builder native|pumpportal]

Only the wallet's PUBLIC key is used (WALLET_PUBLIC_KEY, or derived from
the configured key); no signature is ever produced here.
"""

import argparse
import asyncio
import base64
import json
from decimal import Decimal

import httpx

from yonixalpha_core.config import get_settings
from yonixalpha_core.db.base import make_engine, make_session_factory
from yonixalpha_core.solana import venue as venues
from yonixalpha_core.solana.market_data import JupiterClient, RateBudget
from yonixalpha_core.solana.pumpportal import PumpPortalClient, TradeRequest
from yonixalpha_core.solana.rpc import RpcManager
from yonixalpha_core.solana.rpc_registry import effective_rpc
from yonixalpha_core.solana.tx_builders import BuildError, JupiterBuilder, NativePumpBuilder, PumpPortalBuilder
from yonixalpha_core.solana.txguard import GuardExpectation, inspect


def _pubkey(settings) -> str:
    if getattr(settings, "WALLET_PUBLIC_KEY", None):
        return settings.WALLET_PUBLIC_KEY
    from yonixalpha_core.solana.wallet import load_wallet

    w = load_wallet(settings)
    if w is None:
        raise SystemExit("no wallet configured (WALLET_PUBLIC_KEY / WALLET_PRIVATE_KEY)")
    return w.pubkey


async def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("mint")
    ap.add_argument("--sol", default="0.01", help="buy size in SOL (default 0.01)")
    ap.add_argument("--sell-tokens", help="dry-run a SELL of this many whole tokens instead of a buy")
    ap.add_argument("--slippage", default="10", help="percent (default 10)")
    ap.add_argument("--builder", choices=("native", "pumpportal"), default="native")
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
        wallet = _pubkey(settings)
        jup = JupiterClient(http, settings.JUPITER_API_KEY, RateBudget(30))
        side = "sell" if a.sell_tokens else "buy"
        amount = a.sell_tokens or a.sol
        req = TradeRequest(wallet, side, a.mint, str(amount), side == "buy", Decimal(a.slippage), Decimal("0.0001"), "pump")
        lamports = int(Decimal(a.sol) * 1_000_000_000)
        v = await venues.resolve(rpc, a.mint, side=side, amount_raw=lamports if side == "buy" else None, jupiter=jup,
                                 slippage_bps=int(Decimal(a.slippage) * 100))
        print("VENUE:", json.dumps(v.summary(), indent=1, default=str))
        if not v.executable:
            print(f"NOT EXECUTABLE: {v.kind} — {v.reason}")
            return 2
        exp = GuardExpectation(wallet=wallet, mint=a.mint, side=side, venue=v.kind,
                               max_sol_in_lamports=int(lamports * (1 + Decimal(a.slippage) / 100)) if side == "buy" else None,
                               max_tokens_in=None, max_fee_transfer_lamports=int(lamports * Decimal("0.01")),
                               max_priority_fee_lamports=2_000_000)
        if side == "sell":
            exp.max_tokens_in = int(Decimal(a.sell_tokens) * Decimal(10) ** (v.decimals or 6))
        builder = (JupiterBuilder(jup, rpc) if v.kind == venues.JUPITER_ROUTE
                   else PumpPortalBuilder(PumpPortalClient(http)) if a.builder == "pumpportal" else NativePumpBuilder(rpc))
        try:
            built = await builder.build(req, v, exp, wallet)
        except BuildError as exc:
            print(f"BUILD FAILED ({type(builder).__name__}): {exc}")
            return 3
        keys = [str(k) for k in built.tx.message.account_keys]
        print(f"BUILT by {built.provider}: {len(built.tx.message.instructions)} instructions", json.dumps(built.detail, default=str))
        for ix in built.tx.message.instructions:
            prog = keys[ix.program_id_index] if ix.program_id_index < len(keys) else "?"
            print(f"  {prog}  data={bytes(ix.data)[:8].hex()}  accounts={len(ix.accounts)}")
        report = inspect(built.tx, exp, built.loaded)
        print("GUARD:", "PASSED" if report.ok else "REJECTED", "; ".join(report.violations))
        wire = base64.b64encode(bytes(built.tx)).decode()
        sim = await rpc.call("simulateTransaction", [wire, {"encoding": "base64", "sigVerify": False,
                                                            "replaceRecentBlockhash": True, "commitment": "confirmed"}])
        value = (sim or {}).get("value") or {}
        print("SIMULATION:", "OK" if value.get("err") is None else f"FAILED {value.get('err')}",
              f"units={value.get('unitsConsumed')}")
        for line in (value.get("logs") or [])[-25:]:
            print("   ", line)
        print("Nothing was signed or sent.")
        return 0 if report.ok and value.get("err") is None else 3


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
