"""Launchpad verification on the real chains (BSC, Robinhood Chain).

Read-only: it reads contract code, logs, blocks and eth_call quotes. It never
signs or sends a transaction, and it needs no wallet.

For every EVM launchpad in the registry it records evidence rows
(launchpad_checks, source "launchpad_verify") that the dashboard's
Launchpads page turns into a status:

  ACTIVE               every registered contract has code at its address
  DISCOVERY            launch events were decoded in the window (0 = FAIL)
  EVENTS               trade events were decoded, with no decode errors
  QUOTE                a buy quote and a sell quote of the bought amount
                       came back from the contracts for a recently traded token
  LIQUIDITY            that token's pool / curve holds quote liquidity
  MIGRATION_DETECTION  a migration seen in the window (or a graduated token
                       among the quoted ones) was confirmed by detect_migration

Nothing is recorded when a check could not run (RPC unavailable, no token to
quote, no migration in the window): an unavailable RPC is never evidence.
SAFETY, TX_MONITORING, BUY and SELL are never recorded here: SAFETY needs the
EVM safety checks of the discovery service, BUY / SELL / TX_MONITORING need a
real, authorized transaction.

On the server, in /opt/yonixalpha:

    docker compose --env-file .env -f infra/docker/docker-compose.yml \\
        -f infra/docker/docker-compose.prod.yml \\
        run --rm decision-engine python -m yonixalpha_core.tools.launchpad_verify [--hours 6] [--dry-run]
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from yonixalpha_core.chains.evm import EVM_LAUNCHPADS, adapter_for
from yonixalpha_core.chains.evm.launchpad import ScanResult
from yonixalpha_core.chains.evm import rpc_registry as evm_rpc_registry
from yonixalpha_core.chains.evm.rpc import EvmRpc, EvmRpcError, EvmRpcUnavailableError
from yonixalpha_core.chains.registry import LAUNCHPADS

SOURCE = "launchpad_verify"
PROBE_WEI = {"bsc": 10 ** 16, "robinhood": 2 * 10 ** 15}  # 0.01 BNB / 0.002 ETH
NOT_RECORDED = ("SAFETY", "TX_MONITORING", "BUY", "SELL")
RETRY_SECONDS = 15


@dataclass
class CheckResult:
    check: str
    ok: bool | None  # None: could not run, nothing is recorded
    detail: str
    evidence: dict[str, Any] = field(default_factory=dict)


def jsonable(v: Any) -> Any:
    if isinstance(v, dict):
        return {str(k): jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple, set)):
        return [jsonable(x) for x in v]
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, bool) or v is None or isinstance(v, (str, float)):
        return v
    if isinstance(v, int):
        return v if abs(v) < 2 ** 53 else str(v)
    return str(v)


async def window(rpc: EvmRpc, hours: float, max_blocks: int) -> tuple[int, int, float]:
    head = await rpc.block_number()
    sample = min(2000, head)
    t_head = int((await rpc.get_block(head))["timestamp"], 16)
    t_back = int((await rpc.get_block(head - sample))["timestamp"], 16)
    block_s = max(0.05, (t_head - t_back) / sample) if sample else 1.0
    blocks = min(max_blocks, int(hours * 3600 / block_s))
    return max(0, head - blocks), head, block_s


async def verify(adapter, from_block: int, to_block: int, probe: int) -> list[CheckResult]:
    spec = adapter.spec
    rpc: EvmRpc = adapter.rpc
    out: list[CheckResult] = []

    codes = {k: len((await rpc.get_code(a)) or "0x") // 2 - 1 for k, a in spec.contracts.items()}
    missing = [k for k, n in codes.items() if n <= 0]
    out.append(CheckResult("ACTIVE", not missing, "all contracts have code" if not missing else f"no code: {missing}",
                           {"code_bytes": codes, "chain_id": rpc.chain_id, "head": to_block}))

    emitters_before = len(await adapter._emitters() or [])
    first = await adapter.scan(from_block, to_block)
    res: ScanResult = first
    if len(await adapter._emitters() or []) != emitters_before:
        # contracts announced during the scan (curves / pools): read their trades too
        second = await adapter.scan(from_block, to_block)
        res.trades, res.decode_errors = second.trades, first.decode_errors + second.decode_errors
    blocks = {"from_block": from_block, "to_block": to_block}
    sample = res.launches[-1] if res.launches else None
    out.append(CheckResult("DISCOVERY", bool(res.launches), f"{len(res.launches)} launches decoded",
                           {**blocks, **res.summary(), "sample": sample and {"token": sample.token,
                                                                             "tx": sample.tx_hash}}))
    if res.decode_errors:
        out.append(CheckResult("EVENTS", False, f"{len(res.decode_errors)} logs failed to decode",
                               {**blocks, "errors": res.decode_errors[:5]}))
    elif res.trades:
        t = res.trades[-1]
        out.append(CheckResult("EVENTS", True, f"{len(res.trades)} trades decoded",
                               {**blocks, "trades": len(res.trades), "sample": t.event_id,
                                "buys": sum(x.is_buy for x in res.trades)}))
    else:
        out.append(CheckResult("EVENTS", None, "no trades in the window"))

    if not spec.supports_trading:
        out.append(CheckResult("QUOTE", None, "observe-only venue: not quoted"))
        return out
    tokens: list[str] = []
    for t in reversed(res.trades):
        if t.token not in tokens:
            tokens.append(t.token)
    quoted = None
    states: dict[str, Any] = {}
    attempts = []
    for token in tokens[:3]:
        try:
            states[token] = await adapter.get_token_state(token)
        except EvmRpcUnavailableError:
            raise
        except Exception as exc:  # noqa: BLE001
            attempts.append({"token": token, "state_error": str(exc)[:160]})
        buy = await adapter.quote_buy(token, probe)
        sell = await adapter.quote_sell(token, buy.amount_out) if buy.ok and buy.amount_out else None
        attempts.append({"token": token, "buy": vars(buy), "sell": sell and vars(sell)})
        if buy.ok and sell is not None and sell.ok:
            quoted = token
            break
    if not tokens:
        out.append(CheckResult("QUOTE", None, "no traded token in the window to quote"))
    else:
        out.append(CheckResult("QUOTE", quoted is not None,
                               f"buy + sell quoted for {quoted}" if quoted else "no token returned both quotes",
                               {"probe_in": probe, "attempts": attempts}))
    st = states.get(quoted) if quoted else None
    if st is not None:
        out.append(CheckResult("LIQUIDITY", bool(st.liquidity_quote and st.liquidity_quote > 0),
                               f"{st.liquidity_quote} quote in {st.stage}",
                               {"token": quoted, "stage": st.stage, "liquidity_quote": st.liquidity_quote,
                                "source": st.source}))

    mig_tokens = [m["token"] for m in res.migrations] + [t for t, s in states.items() if s.stage == "DEX"]
    for token in mig_tokens[-3:]:
        det = await adapter.detect_migration(token)
        if det:
            out.append(CheckResult("MIGRATION_DETECTION", True, f"migration of {token} confirmed",
                                   {"token": token, **det}))
            break
    else:
        out.append(CheckResult("MIGRATION_DETECTION", None,
                               "no migration in the window" if not mig_tokens else "migration not confirmed"))
    return out


def _args(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=6.0)
    ap.add_argument("--max-blocks", type=int, default=150_000)
    ap.add_argument("--chain", choices=("bsc", "robinhood"))
    ap.add_argument("--launchpad", choices=EVM_LAUNCHPADS)
    ap.add_argument("--dry-run", action="store_true", help="print only; record nothing")
    return ap.parse_args(argv)


async def main(argv: list[str] | None = None) -> int:
    from yonixalpha_core.chains import verification
    from yonixalpha_core.config import get_settings
    from yonixalpha_core.db.base import make_engine, make_session_factory
    from yonixalpha_core.db.redis import make_redis

    args = _args(argv)
    settings = get_settings()
    engine = make_engine(settings)
    session_factory = make_session_factory(engine)
    redis = make_redis(settings)
    now = datetime.now(timezone.utc)
    print(f"LAUNCHPAD VERIFY {now.isoformat()} (read-only: nothing is signed or sent)\n")
    keys = [k for k in EVM_LAUNCHPADS if (not args.launchpad or k == args.launchpad)
            and (not args.chain or LAUNCHPADS[k].chain.value == args.chain)]
    rpcs: dict[str, EvmRpc] = {}
    recorded = 0
    try:
        for key in keys:
            chain = LAUNCHPADS[key].chain.value
            if chain not in rpcs:  # dashboard providers first, then .env, then public
                async with session_factory() as session:
                    rpcs[chain] = await evm_rpc_registry.rpc_for(session, settings, chain)
            rpc = rpcs[chain]
            print(f"== {LAUNCHPADS[key].name} ({chain})")
            results = None
            for attempt in (1, 2):  # a brief network blip (both BSC nodes timed out at once) gets one retry
                try:
                    fb, tb, bt = await window(rpc, args.hours, args.max_blocks)
                    print(f"   blocks {fb}..{tb} (~{bt:.2f}s per block, {args.hours}h max)")
                    results = await verify(adapter_for(key, rpc), fb, tb, PROBE_WEI[chain])
                    break
                except (EvmRpcUnavailableError, EvmRpcError) as exc:
                    print(f"   RPC UNAVAILABLE: {exc}")
                    if attempt == 1:
                        print(f"   retrying in {RETRY_SECONDS}s")
                        await asyncio.sleep(RETRY_SECONDS)
            if results is None:
                print("   nothing recorded (an unavailable RPC is not evidence)\n")
                continue
            for r in results:
                word = "PASS" if r.ok else "FAIL" if r.ok is False else "NOT RUN"
                print(f"   {r.check:20} {word:8} {r.detail}")
            for c in NOT_RECORDED:
                print(f"   {c:20} {'NOT RUN':8} {'needs the EVM safety checks' if c == 'SAFETY' else 'needs a real authorized transaction'}")
            if not args.dry_run:
                async with session_factory() as session:
                    for r in results:
                        if r.ok is not None:
                            await verification.record(session, key, r.check, r.ok, jsonable(r.evidence), SOURCE, now)
                            recorded += 1
                    await session.commit()
            print()
        for rpc in rpcs.values():
            await rpc.publish_health(redis)
            for e in rpc.health()["endpoints"]:
                print(f"RPC {rpc.chain:10} {e['url']:40} {e['state']:12} ok={e['ok']} errors={e['errors']} "
                      f"429={e['rate_limited']} {e['last_error'] or ''}")
        print(f"\n{'DRY RUN: nothing recorded' if args.dry_run else f'{recorded} evidence rows recorded'}")
        return 0
    finally:
        for rpc in rpcs.values():
            await rpc.aclose()
        await redis.aclose()
        await engine.dispose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
