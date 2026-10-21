"""Why is momentum not trading? Read-only: stage counters, the A-H verdict
(yonixalpha_core.momentum_pipeline) and the latest momentum candidates with
the stage each reached and its final blocker. Nothing is written.

    $C exec -T api python -m yonixalpha_core.tools.momentum_pipeline [--hours 24] [--tokens 25]

The scan / prefilter counters exist from the deploy that added them (hourly
buckets, kept 48 h); before that the verdict uses the database stages only.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timedelta, timezone

from yonixalpha_core import execution_funnel, momentum_pipeline
from yonixalpha_core.config import get_settings
from yonixalpha_core.db.base import make_engine, make_session_factory
from yonixalpha_core.db.redis import make_redis


def _detail(d) -> str:
    if d is None:
        return "not recorded"
    if isinstance(d, dict):
        return ", ".join(f"{k} {json.dumps(v, default=str) if isinstance(v, dict) else v}" for k, v in d.items())
    return str(d)


async def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="why momentum is or is not trading (read-only)")
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--tokens", type=int, default=25)
    args = ap.parse_args(argv)
    settings = get_settings()
    engine = make_engine(settings, pool_size=1, max_overflow=0)
    sf = make_session_factory(engine)
    redis = make_redis(settings)
    now = datetime.now(timezone.utc)
    try:
        async with sf() as s:
            r = await momentum_pipeline.report(s, redis, now, args.hours)
            print(f"MOMENTUM PIPELINE, last {args.hours} h, at {r['at'][:19]} UTC")
            print(f"  strategy mode {r['mode']}, global mode {r['global_mode']}; stage counters: {r['funnel_counters']}")
            print(f"  last momentum gate decision ever: {(r['last_decision_ever'] or 'never')[:19]}")
            print("\nSTAGES")
            for st in r["stages"]:
                print(f"  {st['stage']:<10} {str(st['value']) if st['value'] is not None else '-':>8}   {_detail(st['detail'])}")
            if r["blockers"]:
                print("\nWHY DECISIONS WERE REFUSED (finding code, category, count)")
                for b in r["blockers"][:15]:
                    print(f"  {b['code']:<34} {b['category']:<10} {b['n']}")
            print("\nVERDICT (first applies; all listed)")
            for f in r["findings"]:
                print(f"  {f['code']:<20} [{f['kind']}] {f['message']}")
            if args.tokens:
                ladder = await execution_funnel.pipeline(s, now - timedelta(hours=args.hours), token_limit=args.tokens,
                                                         candidate_engine="momentum")
                print(f"\nLATEST MOMENTUM CANDIDATES (stage reached, final blocker), newest first: {len(ladder['tokens'])}")
                for t in ladder["tokens"]:
                    fb = t.get("final_blocker") or {}
                    print(f"  {str(t['promoted_at'])[:19]}  {(t['symbol'] or '')[:12]:<12} {t['mint']}  {t['stage']:<18} "
                          f"{fb.get('code') or '-'}")
                if not ladder["tokens"]:
                    print("  none in the window")
    finally:
        await redis.aclose()
        await engine.dispose()
    print("\nNothing was written. One token in detail: python -m yonixalpha_core.tools.execution_funnel --mint <MINT>")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
