"""Cost report: where the wallet's SOL went in each confirmed LIVE order, and
how much SOL sits in the wallet's empty token accounts. Read-only
(getTransaction, getTokenAccountsByOwner); signs nothing, prints no secret
and no RPC URL beyond scheme://host.

    docker compose --env-file .env -f infra/docker/docker-compose.yml \\
        -f infra/docker/docker-compose.prod.yml run --rm paper-trading \\
        python -m yonixalpha_core.tools.cost_report [--last 20] [--json]

Per order, from the confirmed transaction itself: trade amount, program
fees (protocol, creator, LP), network + priority fee, SOL deposited into
accounts the transaction created (a token account's deposit is rent,
returned only when the account is closed), refunds, and any residual the
items do not explain.
"""

import argparse
import asyncio
import json
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy import select

from yonixalpha_core import execution_analysis as xa
from yonixalpha_core.config import get_settings
from yonixalpha_core.db.base import make_engine, make_session_factory
from yonixalpha_core.db.models import ExecutionOrder, PaperPosition
from yonixalpha_core.redact import redact_url
from yonixalpha_core.solana.rpc import get_transaction_params
from yonixalpha_core.solana.rpc_registry import effective_rpc
from yonixalpha_core.tools.trade_report import wallet_public_key

LAMPORTS = Decimal(1_000_000_000)
TOKEN_PROGRAMS = ("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")


def sol(lamports: int | None) -> str:
    return "—" if lamports is None else f"{Decimal(lamports) / LAMPORTS:.9f}"


async def _call(http: httpx.AsyncClient, specs: list[dict], method: str, params: list) -> Any:
    """First endpoint that answers with a result (endpoints that refuse the
    method or the plan are skipped)."""
    last = None
    for spec in specs:
        try:
            r = await http.post(spec["url"], json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=15)
            body = r.json()
        except (httpx.HTTPError, ValueError) as exc:
            last = f"{redact_url(spec['url'])}: {type(exc).__name__}"
            continue
        if body.get("result") is not None:
            return body["result"]
        last = f"{redact_url(spec['url'])}: {json.dumps(body.get('error'))[:120]}"
    raise RuntimeError(f"{method}: no endpoint answered ({last})")


def empty_accounts(results: list[Any]) -> dict[str, Any]:
    """Token accounts holding nothing (closable: their rent comes back) and
    accounts holding only a remainder, from getTokenAccountsByOwner."""
    empty, remainder = [], []
    for res in results:
        for acc in (res or {}).get("value") or []:
            a = acc.get("account") or {}
            info = ((a.get("data") or {}).get("parsed") or {}).get("info") or {}
            amount = int((info.get("tokenAmount") or {}).get("amount") or 0)
            row = {"account": acc.get("pubkey"), "mint": info.get("mint"), "lamports": int(a.get("lamports") or 0), "amount": amount}
            (empty if amount == 0 else remainder).append(row)
    return {"empty": empty, "empty_lamports": sum(r["lamports"] for r in empty),
            "with_balance": remainder, "with_balance_lamports": sum(r["lamports"] for r in remainder)}


async def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--last", type=int, default=20)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    settings = get_settings()
    wallet = wallet_public_key(settings)
    if not wallet:
        print("no live wallet configured")
        return 1
    engine = make_engine(settings)
    async with make_session_factory(engine)() as s:
        specs = await effective_rpc(s, settings)
        orders = (await s.execute(select(ExecutionOrder).where(
            ExecutionOrder.mode == "LIVE", ExecutionOrder.status == "CONFIRMED", ExecutionOrder.signature.is_not(None))
            .order_by(ExecutionOrder.created_at.desc()).limit(a.last))).scalars().all()
        symbols = {}
        for o in orders:
            p = await s.get(PaperPosition, o.position_id) if o.position_id else None
            symbols[o.id] = p.symbol if p is not None else o.mint[:8]
    await engine.dispose()
    if not specs:
        print("no RPC endpoint configured")
        return 1

    rows: list[dict[str, Any]] = []
    async with httpx.AsyncClient() as http:
        for o in orders:
            row: dict[str, Any] = {"at": o.created_at.isoformat()[:19], "side": o.side, "symbol": symbols[o.id],
                                   "signature": o.signature, "size": o.amount}
            try:
                tx = await _call(http, specs, "getTransaction", get_transaction_params(o.signature))
                event = xa.own_trade_event(list((tx.get("meta") or {}).get("logMessages") or []), wallet, o.mint)
                row["costs"] = xa.cost_breakdown(tx, wallet, o.mint, event)
            except Exception as exc:  # noqa: BLE001 - reported per order
                row["error"] = f"{type(exc).__name__}: {exc}"[:200]
            rows.append(row)
        try:
            accounts = empty_accounts([await _call(http, specs, "getTokenAccountsByOwner",
                                                   [wallet, {"programId": prog}, {"encoding": "jsonParsed", "commitment": "confirmed"}])
                                       for prog in TOKEN_PROGRAMS])
        except Exception as exc:  # noqa: BLE001
            accounts = {"error": f"{type(exc).__name__}: {exc}"[:200]}

    if a.json:
        print(json.dumps({"orders": rows, "token_accounts": accounts}, default=str, indent=1))
        return 0
    for r in rows:
        print(f"=== {r['at']} {r['side']:4} {r['symbol']:12} {r['signature']}")
        if "error" in r:
            print(f"    unavailable: {r['error']}")
            continue
        c = r["costs"]
        print(f"    wallet change {sol(c['wallet_change_lamports'])} SOL = trade {sol(c['trade_lamports'])}"
              f" | program fees {sol(c['program_fees_lamports'])} | network fee {sol(c['network_fee_lamports'])}"
              f" (priority {sol(c['priority_fee_lamports'])}) | new-account deposits {sol(c['net_deposits_lamports'])}"
              f" | residual {sol(c['residual_lamports'])}")
        for d in c["deposits"]:
            print(f"      deposit {sol(d['lamports'])} SOL into {d['account']} ({d['what']}, {d['space']} bytes)")
        for f in c["refunds"]:
            print(f"      refund  {sol(f['lamports'])} SOL from closed {f['account']}")
        if c["trade_lamports"]:
            extra = -c["wallet_change_lamports"] - c["trade_lamports"] if r["side"] == "BUY" else None
            if extra is not None:
                print(f"    paid beyond the trade: {sol(extra)} SOL = {Decimal(extra) / Decimal(c['trade_lamports']) * 100:.2f}% of it")
    print()
    if "error" in accounts:
        print(f"TOKEN ACCOUNTS: unavailable ({accounts['error']})")
    else:
        print(f"TOKEN ACCOUNTS of the wallet: {len(accounts['empty'])} empty, holding {sol(accounts['empty_lamports'])} SOL of rent "
              "(returned to the wallet if those accounts are closed); "
              f"{len(accounts['with_balance'])} with a token balance, holding {sol(accounts['with_balance_lamports'])} SOL of rent")
        for e in accounts["empty"][:30]:
            print(f"    empty {e['account']}  mint {e['mint']}  {sol(e['lamports'])} SOL")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
