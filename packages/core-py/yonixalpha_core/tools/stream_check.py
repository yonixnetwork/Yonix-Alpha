"""Is the fresh-token stream complete? Measures one minute of pump.fun
events from the RPC WebSocket next to PumpPortal's independent new-token
feed. Read-only: nothing is written, no transaction, no secret printed.

    $C exec engine-solana-discovery python -m yonixalpha_core.tools.stream_check [--seconds 60]
"""

from __future__ import annotations

import argparse
import asyncio
import time

from yonixalpha_core.config import get_settings
from yonixalpha_core.db.redis import make_redis
from yonixalpha_core.solana import pump_stream, pumpportal_ws, stream_guard


async def snapshot(redis) -> dict[str, int]:
    out = {f"stream_{k}": v for k, v in (await pump_stream.stats(redis)).items()}
    out.update({f"pp_{k}": int(v) for k, v in (await redis.hgetall(pumpportal_ws.STATS)).items()})
    out.update({f"gap_{k}": int(v) for k, v in (await redis.hgetall(stream_guard.GAP_STATS)).items()})
    return out


async def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="fresh-token stream completeness (read-only)")
    ap.add_argument("--seconds", type=int, default=60)
    args = ap.parse_args(argv)
    redis = make_redis(get_settings())
    try:
        now = time.time()
        hb = await pump_stream.heartbeat(redis)
        pp = await pumpportal_ws.heartbeat(redis)
        print("1. Last event received")
        print(f"  RPC WebSocket (pump.fun program logs): {f'{now - hb.timestamp():.0f}s ago' if hb else 'never'}")
        print(f"  PumpPortal feed: {f'{now - pp:.0f}s ago' if pp else 'never'}")

        print(f"\n2. Events in the next {args.seconds}s (measuring...)")
        a = await snapshot(redis)
        await asyncio.sleep(args.seconds)
        b = await snapshot(redis)
        per_min = 60 / args.seconds
        rows = [("RPC WebSocket notifications", "stream_notifications"), ("RPC WebSocket new tokens", "stream_create"),
                ("RPC WebSocket trades", "stream_trade"), ("PumpPortal new tokens", "pp_create"),
                ("new tokens read from chain (gap fill)", "gap_filled")]
        for label, key in rows:
            print(f"  {label:<40} {round((b.get(key, 0) - a.get(key, 0)) * per_min):>7} per minute")

        print("\n3. Share of PumpPortal's new tokens the RPC WebSocket delivered itself")
        for window in (600, 3600):
            cov = await stream_guard.stream_coverage(redis, time.time(), window)
            share = f"{cov['coverage']:.0%}" if cov["coverage"] is not None else "not measurable yet"
            print(f"  last {window // 60:>2} min: {cov['delivered_by_stream']} of {cov['announced']} ({share})")
        problem = await stream_guard.stream_problem(redis)
        measured = (await stream_guard.stream_coverage(redis, time.time(), 3600))["announced"] > 0

        print("\n4. Verdict")
        if problem:
            print(f"  PROBLEM: {problem}.")
            print("  The discovery service reconnects the WebSocket to the next provider by itself (at most every "
                  f"{stream_guard.RECONNECT_COOLDOWN // 60} min) and reads the missed new tokens from chain.")
        elif not measured:
            print("  NOT MEASURABLE: PumpPortal announced no new token yet, so there is nothing to compare with.")
        else:
            print("  the RPC WebSocket delivers the new tokens PumpPortal announces.")
    finally:
        await redis.aclose()
    print("\nNothing was written.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
