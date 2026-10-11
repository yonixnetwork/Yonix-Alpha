"""Data retention: preview, enable, disable or run now (yonixalpha_core.retention).

  python -m yonixalpha_core.tools.retention                 preview (nothing is deleted)
  python -m yonixalpha_core.tools.retention --enable        turn the hourly cleanup on (3 days)
  python -m yonixalpha_core.tools.retention --enable --days 5
  python -m yonixalpha_core.tools.retention --disable
  python -m yonixalpha_core.tools.retention --run-now       one pass now (must be enabled)
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone

from yonixalpha_core import retention
from yonixalpha_core.config import get_settings
from yonixalpha_core.db.base import make_engine, make_session_factory


async def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--enable", action="store_true")
    ap.add_argument("--disable", action="store_true")
    ap.add_argument("--days", type=int, default=None)
    ap.add_argument("--run-now", action="store_true")
    a = ap.parse_args(argv)
    engine = make_engine(get_settings())
    sf = make_session_factory(engine)
    now = datetime.now(timezone.utc)
    try:
        if a.enable or a.disable:
            async with sf() as s:
                cur = await retention.load_settings(s)
                v = await retention.save_settings(s, a.enable and not a.disable, a.days or cur["days"], "operator (tool)")
            print(f"data retention: {'ON' if v['enabled'] else 'OFF'}, keep {v['days']} days")
        if a.run_now:
            out = await retention.run(sf, now)
            print(out.get("skipped") and "not run: retention is OFF (use --enable first)" or
                  "deleted: " + ", ".join(f"{k} {n}" for k, n in out["deleted"].items()))
        if not (a.enable or a.disable or a.run_now):
            p = await retention.preview(sf, now, a.days)
            print(f"PREVIEW (nothing deleted). Retention is {'ON' if p['enabled'] else 'OFF'}; keep {p['days']} days.")
            print("Would delete now:")
            for k, n in p["would_delete"].items():
                print(f"  {k:24s} {n}")
            print("(old candidates and tokens are counted only once their old decisions / events are gone,")
            print(" so the first runs delete more of them than this preview shows)")
            print("Never deleted: " + ", ".join(p["kept_tables"]))
    finally:
        await engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
