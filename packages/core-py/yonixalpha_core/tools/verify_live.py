"""Read-only live verification of every external data source the safety
gate depends on. Run it on the server, where the APIs are reachable:

    docker compose --env-file .env -f infra/docker/docker-compose.yml \\
        -f infra/docker/docker-compose.prod.yml \\
        run --rm decision-engine python -m yonixalpha_core.tools.verify_live --seconds 60

It only reads: no transactions, no orders, no wallet keys, no database or
Redis writes. Provider URLs are printed as scheme://host only.

Each check reports VERIFIED (observed working on live data), FAILED (tried
and got a wrong or error result), or NOT VERIFIED (could not be attempted,
e.g. no sample available in the listening window).
"""

import argparse
import asyncio
import base64
import json
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import httpx
import websockets

from yonixalpha_core.config import get_settings
from yonixalpha_core.redact import redact_text, redact_url
from yonixalpha_core.solana.assembler import token_account_owners
from yonixalpha_core.solana.market_data import DexScreenerClient, JupiterClient, RateBudget
from yonixalpha_core.solana.pumpfun import PUMP_PROGRAM_ID, decode_bonding_curve, decode_log_events
from yonixalpha_core.solana.rpc import RpcManager
from yonixalpha_core.solana.token_safety import parse_holders, parse_mint_account

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
BONK = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"


class Report:
    def __init__(self, secrets: list[str | None]):
        self.rows: list[dict[str, Any]] = []
        self.secrets = secrets

    def add(self, check: str, status: str, detail: str) -> None:
        detail = redact_text(detail, self.secrets)
        self.rows.append({"check": check, "status": status, "detail": detail})
        print(f"[{status:>12}] {check}: {detail}", flush=True)


async def listen_pump(ws_url: str, seconds: int) -> dict[str, Any]:
    out: dict[str, Any] = {"notifications": 0, "events": {}, "samples": {}, "error": None, "subscribed": False}
    deadline = time.monotonic() + seconds
    try:
        ws = await websockets.connect(ws_url, max_size=2**22, close_timeout=2)
        try:
            await ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "logsSubscribe",
                                      "params": [{"mentions": [PUMP_PROGRAM_ID]}, {"commitment": "confirmed"}]}))
            while time.monotonic() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=max(deadline - time.monotonic(), 0.1))
                except asyncio.TimeoutError:
                    break
                msg = json.loads(raw)
                if msg.get("id") == 1:
                    out["subscribed"] = "result" in msg
                    if "error" in msg:
                        out["error"] = f"subscribe rejected: {msg['error']}"
                        break
                    continue
                if msg.get("method") != "logsNotification":
                    continue
                out["notifications"] += 1
                value = msg["params"]["result"]["value"]
                if value.get("err") is not None:
                    continue
                for kind, fields in decode_log_events(value.get("logs", [])):
                    out["events"][kind] = out["events"].get(kind, 0) + 1
                    out["samples"].setdefault(kind, fields)
        finally:
            # Drop the connection instead of the closing handshake: on the
            # pump.fun firehose the server's close frame queues behind
            # unread notifications, and a graceful close can wait for it.
            ws.transport.abort()
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


async def main(seconds: int, as_json: bool) -> int:
    settings = get_settings()
    report = Report([settings.SOLANA_RPC_URL, settings.SOLANA_WS_URL, settings.SOLANA_RPC_BACKUP_URL,
                     settings.SOLANA_WS_BACKUP_URL, settings.JUPITER_API_KEY, settings.HELIUS_API_KEY])
    print(f"verify_live {datetime.now(timezone.utc).isoformat()} rpc={redact_url(settings.SOLANA_RPC_URL)} "
          f"ws={redact_url(settings.SOLANA_WS_URL)} jupiter={'keyed' if settings.JUPITER_API_KEY else 'free (lite-api)'}")

    async with httpx.AsyncClient() as http:
        rpc = None
        if settings.SOLANA_RPC_URL:
            rpc = RpcManager.create(client=http, primary_url=settings.SOLANA_RPC_URL, backup_url=settings.SOLANA_RPC_BACKUP_URL)
            try:
                t0 = time.monotonic()
                slot = await rpc.call("getSlot")
                report.add("solana_rpc", "VERIFIED", f"getSlot={slot} in {(time.monotonic() - t0) * 1000:.0f} ms")
            except Exception as exc:  # noqa: BLE001
                report.add("solana_rpc", "FAILED", str(exc.__cause__ or exc))
                rpc = None
        else:
            report.add("solana_rpc", "NOT VERIFIED", "SOLANA_RPC_URL not set")

        pump: dict[str, Any] = {}
        if settings.SOLANA_WS_URL:
            print(f"listening to pump.fun program logs for {seconds}s ...", flush=True)
            pump = await listen_pump(settings.SOLANA_WS_URL, seconds)
            ev, n = pump["events"], pump["notifications"]
            if pump["error"] and not n:
                report.add("pump_stream", "FAILED", pump["error"])
            elif n == 0:
                report.add("pump_stream", "FAILED",
                           "subscribed but no notifications — provider may not support logsSubscribe mentions on this program")
            elif not ev.get("trade"):
                report.add("pump_stream", "FAILED",
                           f"{n} notifications but no TradeEvent decoded from 'Program data:' logs — events may be emitted "
                           "via self-CPI only; decoding then needs transaction inner instructions")
            else:
                report.add("pump_stream", "VERIFIED", f"{n} notifications; decoded {ev}")
        else:
            report.add("pump_stream", "NOT VERIFIED", "SOLANA_WS_URL not set")

        samples = pump.get("samples", {})
        sample_mint = (samples.get("create") or samples.get("trade") or {}).get("mint")
        trade = samples.get("trade")
        if trade:
            fee = trade.get("fee_basis_points"), trade.get("creator_fee_basis_points")
            report.add("pump_trade_fields", "VERIFIED" if "virtual_sol_reserves" in trade and fee[0] is not None else "FAILED",
                       f"virtual reserves present={'virtual_sol_reserves' in trade}, fee_bps={fee[0]}, creator_fee_bps={fee[1]}, "
                       f"quote_mint={trade.get('quote_mint', 'absent (older layout)')}")

        if rpc and sample_mint:
            try:
                res = await rpc.call("getAccountInfo", [sample_mint, {"encoding": "jsonParsed", "commitment": "confirmed"}])
                t = parse_mint_account(res.get("value"), datetime.now(timezone.utc), "rpc")
                report.add("mint_parse", "VERIFIED",
                           f"{sample_mint}: program={t.token_program[:8]}.. decimals={t.decimals} mint_auth={t.mint_authority} "
                           f"freeze_auth={t.freeze_authority} extensions={t.extensions}")
                largest = (await rpc.call("getTokenLargestAccounts", [sample_mint, {"commitment": "confirmed"}]))["value"]
                largest, owners = await token_account_owners(rpc, largest)
                curve_addr = (samples.get("create") or {}).get("bonding_curve")
                h = parse_holders(largest, owners, t.supply_raw, {curve_addr} if curve_addr else set(), None,
                                  datetime.now(timezone.utc), "rpc")
                report.add("holders_parse", "VERIFIED",
                           f"{len(largest)} largest accounts, excluded curve accounts={h.excluded_pool_accounts}, "
                           f"top1={h.top1_share:.4f} top10={h.top10_share:.4f}"
                           + ("" if curve_addr else " (curve address unknown: no create event sampled)"))
            except Exception as exc:  # noqa: BLE001
                report.add("mint_or_holders_parse", "FAILED", f"{type(exc).__name__}: {exc}")
            curve_addr = (samples.get("create") or {}).get("bonding_curve")
            if curve_addr:
                try:
                    res = await rpc.call("getAccountInfo", [curve_addr, {"encoding": "base64", "commitment": "confirmed"}])
                    state = decode_bonding_curve(base64.b64decode(res["value"]["data"][0]))
                    report.add("bonding_curve_decode", "VERIFIED" if state else "FAILED",
                               f"complete={state.complete} real_sol={state.real_liquidity_sol()} "
                               f"price={state.price_sol(6)} SOL/token (assuming 6 decimals)" if state else "did not decode")
                except Exception as exc:  # noqa: BLE001
                    report.add("bonding_curve_decode", "FAILED", f"{type(exc).__name__}: {exc}")
            else:
                report.add("bonding_curve_decode", "NOT VERIFIED", "no CreateEvent in window to learn a curve address from")
        elif not sample_mint:
            report.add("mint_parse", "NOT VERIFIED", "no pump.fun mint sampled from the stream")

        jup = JupiterClient(http, settings.JUPITER_API_KEY, RateBudget(30))
        q = await jup.quote("So11111111111111111111111111111111111111112", USDC, 10_000_000, 100)
        if q.status == "ok":
            report.add("jupiter_quote", "VERIFIED",
                       f"0.01 SOL -> {Decimal(q.out_amount) / Decimal(10**6)} USDC via {q.labels}, "
                       f"priceImpactPct={q.data.get('priceImpactPct')!r}")
            quote, evidence = await jup.execution_quote(BONK, Decimal("0.1"), Decimal("0.01"), 100)
            ok = quote.buy_route_available and quote.sell_route_available and quote.round_trip_loss_bps is not None
            report.add("jupiter_round_trip", "VERIFIED" if ok else "FAILED",
                       f"BONK 0.1 SOL: entry_impact={quote.entry_impact_bps} exit_impact={quote.exit_impact_bps} "
                       f"round_trip={quote.round_trip_loss_bps} bps")
        else:
            report.add("jupiter_quote", "FAILED", f"{q.status}: {q.error}")

        dex = DexScreenerClient(http, RateBudget(60))
        pool, err = await dex.pool(BONK)
        if pool:
            report.add("dexscreener_pool", "VERIFIED",
                       f"BONK/SOL {pool.dex_id} liquidity_sol={pool.liquidity_sol} price_sol={pool.price_sol} "
                       f"h1 buys/sells={pool.buys_h1}/{pool.sells_h1}")
        else:
            report.add("dexscreener_pool", "FAILED", err or "no SOL-quoted pair parsed")

    if as_json:
        print(json.dumps(report.rows, indent=2))
    return 0 if all(r["status"] != "FAILED" for r in report.rows) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seconds", type=int, default=60, help="how long to listen to the pump.fun stream")
    parser.add_argument("--json", action="store_true", help="also print the results as JSON")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.seconds, args.json)))
