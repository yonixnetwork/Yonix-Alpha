"""What happened to a LIVE order, stage by stage, and — when the transaction
guard refused it — what the refused transaction actually contained (every
instruction's program, data prefix and account count; the unsigned
transaction is kept on the order for exactly this).

    docker compose --env-file .env -f infra/docker/docker-compose.yml \\
        -f infra/docker/docker-compose.prod.yml run --rm paper-trading \\
        python -m yonixalpha_core.tools.order_inspect [--last 5] [--mint MINT] [--order ORDER_ID]
"""

import argparse
import asyncio
import base64
import json

from solders.transaction import VersionedTransaction
from sqlalchemy import select

from yonixalpha_core.config import get_settings
from yonixalpha_core.db.base import make_engine, make_session_factory
from yonixalpha_core.db.models import ExecutionOrder

KNOWN = {
    "11111111111111111111111111111111": "System", "ComputeBudget111111111111111111111111111111": "ComputeBudget",
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA": "SPL Token", "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb": "Token-2022",
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL": "Associated Token", "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P": "Pump",
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA": "PumpSwap", "pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ": "Pump fees",
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4": "Jupiter v6",
}


def describe(tx_b64: str) -> list[str]:
    tx = VersionedTransaction.from_bytes(base64.b64decode(tx_b64))
    msg = tx.message
    keys = [str(k) for k in msg.account_keys]
    out = [f"fee payer {keys[0]}, signers required {msg.header.num_required_signatures}, "
           f"lookup tables {len(list(getattr(msg, 'address_table_lookups', []) or []))}"]
    for n, ix in enumerate(msg.instructions):
        prog = keys[ix.program_id_index] if ix.program_id_index < len(keys) else "(lookup table)"
        out.append(f"#{n} {prog} [{KNOWN.get(prog, 'NOT ALLOWED / unknown')}] data={bytes(ix.data)[:8].hex()} "
                   f"accounts={len(ix.accounts)}")
    return out


async def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--last", type=int, default=5)
    ap.add_argument("--mint")
    ap.add_argument("--order")
    a = ap.parse_args(argv)
    engine = make_engine(get_settings())
    async with make_session_factory(engine)() as s:
        q = select(ExecutionOrder).where(ExecutionOrder.mode == "LIVE").order_by(ExecutionOrder.created_at.desc())
        if a.order:
            q = q.where(ExecutionOrder.id == a.order)
        if a.mint:
            q = q.where(ExecutionOrder.mint == a.mint)
        orders = (await s.execute(q.limit(a.last))).scalars().all()
    await engine.dispose()
    for o in orders:
        res = o.result or {}
        print(f"=== {o.id} {o.side} {o.mint} status={o.status} route={o.route} at={o.created_at}")
        print("error:", o.error or res.get("error"))
        if res.get("venue"):
            print("venue:", json.dumps(res["venue"], default=str))
        for st in res.get("stages") or []:
            print("  stage", st.get("stage"), {k: v for k, v in st.items() if k not in ("stage", "at")})
        guard = o.guard or res.get("guard") or {}
        if guard:
            print("guard programs:", guard.get("programs"))
            print("guard violations:", guard.get("violations"))
            for ins in guard.get("instructions") or []:
                print("   ", ins)
        if res.get("unsigned_tx"):
            print("refused transaction (unsigned):")
            for line in describe(res["unsigned_tx"]):
                print("   ", line)
    if not orders:
        print("no LIVE orders found")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
