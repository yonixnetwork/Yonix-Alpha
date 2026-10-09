"""Why LIVE is or is not trading: every switch on the path from a Solana
decision to a live order, and what the gate decided over the last hours.
Read-only: no order, no transaction, no secret printed.

    $C exec api python -m yonixalpha_core.tools.live_check [--hours 24]

A decision goes to the live wallet only when ALL of these hold
(yonixalpha_core.safety.gate._live_target and _mode_findings):
  - the environment locks: TRADING_ENABLED=true, LIVE_TRADING_ENABLED=true,
    PAPER_TRADING=false
  - the global mode is LIVE
  - the strategy (solana_fresh / solana_migration / solana_momentum) is AUTO
    or MANUAL, not PAPER or OFF
  - the live order worker (paper-trading) reports ready: wallet synced,
    balance above the reserve
  - the kill switch is off, and the chain / new-entries switches are on
  - and the gate finds the token executable (every safety check passed)
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from yonixalpha_core import kill_switch, live_trading
from yonixalpha_core.chains import controls
from yonixalpha_core.chains.base import Chain
from yonixalpha_core.config import get_settings
from yonixalpha_core.db.base import make_engine, make_session_factory
from yonixalpha_core.db.redis import make_redis
from yonixalpha_core.safety import store

ENGINES = ("solana_fresh", "solana_migration", "solana_momentum")

DECISIONS_SQL = """
SELECT engine, coalesce(execution_target, '?') AS target, executable, count(*)
FROM risk_assessments WHERE evaluated_at >= :since AND engine LIKE 'solana%'
GROUP BY 1, 2, 3 ORDER BY 1, 2, 3
"""

MODE_CODES_SQL = """
SELECT f->>'code' AS code, count(*)
FROM (SELECT assessment FROM risk_assessments
      WHERE evaluated_at >= :since AND engine LIKE 'solana%' ORDER BY evaluated_at DESC LIMIT 20000) r,
     jsonb_array_elements(r.assessment -> 'findings') f
WHERE f->>'code' IN ('LIVE_NOT_READY', 'LIVE_NOT_PERMITTED', 'STRATEGY_OFF', 'MANUAL_MODE', 'AUTO_NO_APPROVAL',
                     'KILL_SWITCH', 'CHAIN_DISABLED', 'NEW_ENTRIES_DISABLED')
GROUP BY 1 ORDER BY 2 DESC
"""

ORDERS_SQL = """
SELECT side, status, count(*), max(created_at) FROM execution_orders
WHERE mode = 'LIVE' AND created_at >= :since GROUP BY 1, 2 ORDER BY 1, 2
"""

LAST_ERROR_SQL = """
SELECT created_at, side, status, left(coalesce(error, ''), 200) FROM execution_orders
WHERE mode = 'LIVE' AND status NOT IN ('CONFIRMED', 'PENDING') ORDER BY created_at DESC LIMIT 3
"""


def yes(v: bool) -> str:
    return "yes" if v else "NO"


async def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="why LIVE is or is not trading (read-only)")
    ap.add_argument("--hours", type=int, default=24)
    args = ap.parse_args(argv)
    settings = get_settings()
    engine = make_engine(settings, pool_size=1, max_overflow=0)
    sf = make_session_factory(engine)
    redis = make_redis(settings)
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=args.hours)
    blockers: list[str] = []
    try:
        print("1. Environment locks (.env)")
        locks = {"TRADING_ENABLED": bool(settings.TRADING_ENABLED), "LIVE_TRADING_ENABLED": bool(settings.LIVE_TRADING_ENABLED),
                 "PAPER_TRADING=false": not bool(settings.PAPER_TRADING)}
        for k, v in locks.items():
            print(f"  {k}: {yes(v)}")
            if not v:
                blockers.append(f".env: {k} is not set")
        print(f"  wallet key configured: {yes(bool(getattr(settings, 'WALLET_PRIVATE_KEY', None)))} (the key is never printed)")
        if not getattr(settings, "WALLET_PRIVATE_KEY", None):
            blockers.append(".env: WALLET_PRIVATE_KEY is not set")

        async with sf() as s:
            gm = await store.load_global_mode(s)
            modes = {e: (await store.load_strategy_mode(s, e)).value for e in ENGINES}
            ctl = await controls.load(s)
            live_settings = await live_trading.load_live_settings(s)
            print("\n2. Dashboard modes and switches")
            print(f"  global mode: {gm.value}")
            if gm.value != "LIVE":
                blockers.append(f"global mode is {gm.value}, not LIVE (Settings / Risk: global mode)")
            for e, m in modes.items():
                print(f"  strategy {e}: {m}")
            if not any(m in ("AUTO", "MANUAL") for m in modes.values()):
                blockers.append("no Solana strategy is AUTO or MANUAL (Fresh / Migrated / Momentum pages: mode)")
            for key in ("new_entries", "sniper", "chain:solana"):
                on = ctl.get(key, {}).get("enabled", True)
                print(f"  switch {key}: {'ON' if on else 'OFF'}")
                if not on:
                    blockers.append(f"switch {key} is OFF (Launchpads: trading controls)")
            for lp in ("pumpfun", "pumpswap"):
                print(f"  launchpad {lp}: {controls.launchpad_mode(ctl, lp, Chain.SOLANA)}")
            engaged = await kill_switch.is_engaged(redis)
            print(f"  kill switch: {'ENGAGED — ' + str(await kill_switch.get_reason(redis)) if engaged else 'off'}")
            if engaged:
                blockers.append("kill switch is ENGAGED")

            print("\n3. Live order worker (paper-trading) and wallet")
            ready, why = await live_trading.live_readiness(redis, settings, now)
            raw = await redis.get(live_trading.READY_KEY)
            st = json.loads(raw) if raw else {}
            print(f"  worker status: {st.get('status', 'no report')} {('— ' + str(st.get('reason'))) if st.get('reason') else ''}"
                  f"{(' (at ' + str(st.get('at'))[:19] + ')') if st.get('at') else ''}")
            w = await redis.get(live_trading.WALLET_KEY)
            wallet = json.loads(w) if w else {}
            print(f"  wallet balance: {wallet.get('sol', 'unknown')} SOL (synced {str(wallet.get('at', 'never'))[:19]}); "
                  f"reserve kept {live_settings.min_sol_reserve} SOL")
            print(f"  ready for a live order: {yes(ready)}{(' — ' + why) if why else ''}")
            if not ready:
                blockers.append(f"live worker not ready: {why}")

            print(f"\n4. Gate decisions, last {args.hours} h (engine, target, executable, count)")
            rows = (await s.execute(text(DECISIONS_SQL), {"since": since})).all()
            for r in rows:
                print(f"  {r[0]:<18} {r[1]:<6} {'executable' if r[2] else 'refused':<10} {r[3]:>7}")
            if not rows:
                print("  none (is the decision engine running?)")
            live_exec = sum(r[3] for r in rows if r[1] == "LIVE" and r[2])
            paper_exec = sum(r[3] for r in rows if r[1] == "PAPER" and r[2])
            print(f"  executable decisions sent to LIVE: {live_exec}, to PAPER: {paper_exec}")
            if paper_exec and not live_exec:
                blockers.append(f"{paper_exec} executable decision(s) went to PAPER: the mode / lock / readiness above "
                                "was not all set when they were made")
            codes = (await s.execute(text(MODE_CODES_SQL), {"since": since})).all()
            if codes:
                print("  mode / readiness findings: " + ", ".join(f"{c} {n}" for c, n in codes))

            print(f"\n5. LIVE orders, last {args.hours} h")
            orders = (await s.execute(text(ORDERS_SQL), {"since": since})).all()
            for r in orders:
                print(f"  {r[0]} {r[1]}: {r[2]} (latest {str(r[3])[:19]})")
            if not orders:
                print("  none")
            for r in (await s.execute(text(LAST_ERROR_SQL))).all():
                print(f"  last failure {str(r[0])[:19]} {r[1]} {r[2]}: {r[3]}")

        print("\n6. Verdict")
        if blockers:
            print("  LIVE is NOT trading because:")
            for b in blockers:
                print(f"   - {b}")
        else:
            print("  every switch allows LIVE. If no live order was placed, no token passed the gate as executable "
                  "(see section 4 and the Execution Funnel page for the reasons).")
    finally:
        await redis.aclose()
        await engine.dispose()
    print("\nNothing was written.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
