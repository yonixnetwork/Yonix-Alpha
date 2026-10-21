"""Are entries too late? Read-only comparison of entry approaches on the same
chronological data: the shadow entry strategies and baselines recorded in
entry_signals (entry_intel), scored by the shared labeller (executable
return of a reference-size trade after fees, curve impact, measured latency
and fixed costs; entry_outcomes). Nothing is written or retrained.

    $C exec -T api python -m yonixalpha_core.tools.entry_compare [--days 14]

1. Every strategy and baseline: all signals and a hold-out (the last 40 %
   of the signals by time), against CURRENT_GATE_ENTRY (what the gate
   actually approved). Includes the result without the single best signal,
   so one outlier cannot carry a strategy. The stored entry_eval frozen test
   period is shown and flagged when it is too short to mean anything.
2. Earliest vs gate, paired by token: for tokens with a gate entry, the
   earliest earlier signal of any strategy for the same token and lifecycle
   ("A"), against the gate entry itself ("B"). A shadow signal has NOT been
   through the gate's hard safety checks: its result is an upper bound for
   what an earlier, safety-checked entry could have earned.
3. Extension rule ("D"): the gate entries bucketed by how far the price had
   already moved since that earliest signal (both prices known at the gate's
   decision), and "skip the entry when the move exceeds X" with X chosen on
   the first 60 % of the period (by time) and tested on the last 40 %.
"C" (a later confirmation) exists only for migrated tokens:
MIGRATED_DELAYED_CONFIRMATION in section 1.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text

from yonixalpha_core import entry_eval
from yonixalpha_core.config import get_settings
from yonixalpha_core.db.base import make_engine, make_session_factory
from yonixalpha_core.db.models import PlatformSetting

# Only the outcome fields the comparison reads (the full outcome JSON of
# 100k+ signals does not fit the server's memory budget).
ROWS_SQL = """
SELECT mint, strategy, lifecycle, decided_at, price_raw, score, ml_probability,
       jsonb_build_object('executable_return_pct', outcome->'executable_return_pct', 'bad_entry', outcome->'bad_entry',
                          'late_entry', outcome->'late_entry', 'entry_position', outcome->'entry_position',
                          'latency_displacement_pct', outcome->'latency_displacement_pct',
                          'rule_exit', outcome->'rule_exit') AS outcome
FROM entry_signals
WHERE outcome_at IS NOT NULL AND decided_at >= :since ORDER BY decided_at LIMIT :limit
"""
MAX_ROWS = 400000
MIN_FROZEN_SPAN = timedelta(days=1)
GATE = entry_eval.CHAMPION
BUCKETS = ((None, 0.0, "moved down"), (0.0, 0.25, "0-25 %"), (0.25, 0.5, "25-50 %"), (0.5, 1.0, "50-100 %"),
           (1.0, 3.0, "100-300 %"), (3.0, None, "> 300 %"))
THRESHOLDS = (0.25, 0.5, 1.0, 2.0, 3.0)
MIN_N = 10


def ret(r: dict) -> float | None:
    v = (r.get("outcome") or {}).get("executable_return_pct")
    return None if v is None else float(v)


def summary(rets: list[float]) -> dict[str, Any]:
    if not rets:
        return {"n": 0}
    wins, losses = [x for x in rets if x > 0], [x for x in rets if x <= 0]
    return {"n": len(rets), "mean": statistics.fmean(rets), "median": statistics.median(rets),
            "win": len(wins) / len(rets), "pf": (sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else None,
            "sum": sum(rets), "sum_wo_best": sum(rets) - max(rets)}


def fmt(s: dict[str, Any]) -> str:
    if not s.get("n"):
        return "n=0"
    pf = f"{s['pf']:.2f}" if s.get("pf") is not None else "-"
    return (f"n={s['n']:<5} mean {s['mean']:+7.2f}% med {s['median']:+7.2f}% win {s['win'] * 100:3.0f}% PF {pf:>5} "
            f"sum {s['sum']:+9.1f} w/o best {s['sum_wo_best']:+9.1f}")


def earliest_refs(rows: list[dict]) -> list[tuple[dict, dict]]:
    """(earliest earlier signal of the same token and lifecycle, gate entry) pairs."""
    first: dict[tuple[str, str], dict] = {}
    pairs = []
    for r in rows:  # chronological
        key = (r["mint"], r["lifecycle"])
        if r["strategy"] == GATE:
            a = first.get(key)
            if a is not None and a["decided_at"] < r["decided_at"]:
                pairs.append((a, r))
        elif r["price_raw"] and key not in first:
            first[key] = r
    return pairs


def move(a: dict, b: dict) -> float | None:
    if not a.get("price_raw") or not b.get("price_raw"):
        return None
    return float(b["price_raw"]) / float(a["price_raw"]) - 1


def bucket(m: float) -> str:
    for lo, hi, name in BUCKETS:
        if (lo is None or m >= lo) and (hi is None or m < hi):
            return name
    return "?"


def extension_rule(pairs: list[tuple[dict, dict]]) -> dict[str, Any]:
    """Choose X on the first 60 % of the gate entries (by time), apply it to
    the last 40 %."""
    rows = [(b["decided_at"], move(a, b), ret(b)) for a, b in pairs]
    rows = [x for x in rows if x[1] is not None and x[2] is not None]
    if len(rows) < 2 * MIN_N:
        return {"status": f"too few gate entries with both prices and an outcome ({len(rows)}; need {2 * MIN_N})"}
    rows.sort(key=lambda x: x[0])
    cut = int(len(rows) * 0.6)
    sel, oos = rows[:cut], rows[cut:]
    sweep = []
    for x in THRESHOLDS:
        kept = [r for _, m, r in sel if m <= x]
        sweep.append((x, summary(kept)))
    ok = [(x, s) for x, s in sweep if s["n"] >= MIN_N]
    chosen = max(ok, key=lambda t: t[1]["mean"])[0] if ok else None
    return {"status": "evaluated", "selection_baseline": summary([r for _, _, r in sel]), "sweep": sweep, "chosen": chosen,
            "oos_baseline": summary([r for _, _, r in oos]),
            "oos_rule": summary([r for _, m, r in oos if chosen is not None and m <= chosen]),
            "oos_start": oos[0][0].isoformat()}


async def report(session, days: int, now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    rows = [dict(r._mapping) for r in (await session.execute(
        text(ROWS_SQL), {"since": now - timedelta(days=days), "limit": MAX_ROWS})).all()]
    frozen_row = await session.get(PlatformSetting, entry_eval.FREEZE_SETTING)
    frozen = frozen_row.value if frozen_row else None
    print(f"ENTRY COMPARISON, last {days} days: {len(rows)} labelled signals"
          + (f" (the first {MAX_ROWS} only: use fewer --days)" if len(rows) >= MAX_ROWS else ""))
    print("unit: executable return % of a reference-size trade after fees, curve impact, measured latency, fixed costs")
    if frozen:
        span = datetime.fromisoformat(frozen["test_end"]) - datetime.fromisoformat(frozen["test_start"])
        print(f"frozen test period (entry_eval): {frozen['test_start'][:16]} .. {frozen['test_end'][:16]}"
              + (f"  ** UNUSABLE: {span.total_seconds() / 60:.0f} minutes long (frozen when the first "
                 f"{entry_eval.MIN_FREEZE_TOTAL} signals existed); readiness verdicts based on it mean nothing **"
                 if span < MIN_FROZEN_SPAN else ""))
    cut = rows[int(len(rows) * 0.6)]["decided_at"] if rows else None
    if cut is not None:
        print(f"this report's own chronological split: hold-out = the last 40 % of the signals, from {cut.isoformat()[:16]}")

    print("\n1. STRATEGIES AND BASELINES (all signals | hold-out: last 40 % by time)")
    comp = entry_eval.comparison(rows, frozen, {})
    by: dict[str, list[dict]] = {}
    for r in rows:
        by.setdefault(r["strategy"], []).append(r)
    for name, c in comp["strategies"].items():
        all_rets = [x for r in by.get(name, []) if (x := ret(r)) is not None]
        hold = [x for r in by.get(name, []) if r["decided_at"] >= cut and (x := ret(r)) is not None] if cut else []
        if not all_rets:
            continue
        m = c["all"]
        bad = m.get("bad_entry_rate")
        late = m.get("late_entry_rate")
        print(f"  {name:<30} all      {fmt(summary(all_rets))}")
        print(f"  {'':<30} hold-out {fmt(summary(hold))}")
        print(f"  {'':<30} bad entries {'-' if bad is None else f'{bad * 100:.0f}%'}, late entries "
              f"{'-' if late is None else f'{late * 100:.0f}%'}")

    pairs = earliest_refs(rows)
    print(f"\n2. EARLIEST SIGNAL (A) vs GATE ENTRY (B), same token: {len(pairs)} pairs")
    a_rets = [ret(a) for a, b in pairs if ret(a) is not None and ret(b) is not None]
    b_rets = [ret(b) for a, b in pairs if ret(a) is not None and ret(b) is not None]
    delay = [(b["decided_at"] - a["decided_at"]).total_seconds() for a, b in pairs]
    moves = [x for a, b in pairs if (x := move(a, b)) is not None]
    if pairs:
        print(f"  B came {statistics.median(delay):.0f} s (median) after the earliest signal; price moved "
              f"{statistics.median(moves) * 100:+.1f} % (median) in between" if moves else "  no prices to compare")
    print(f"  A earliest signal  {fmt(summary(a_rets))}")
    print(f"  B gate entry       {fmt(summary(b_rets))}")
    print("  (A has not passed the gate's hard safety checks: an upper bound, not an executable alternative)")
    strategies_a: dict[str, int] = {}
    for a, _ in pairs:
        strategies_a[a["strategy"]] = strategies_a.get(a["strategy"], 0) + 1
    if strategies_a:
        print("  earliest signal came from: " + ", ".join(f"{k} {v}" for k, v in sorted(strategies_a.items(), key=lambda kv: -kv[1])))

    print("\n3. GATE ENTRIES BY PRICE MOVE SINCE THE EARLIEST SIGNAL, and the skip rule (D)")
    groups: dict[str, list[float]] = {}
    for a, b in pairs:
        m, r = move(a, b), ret(b)
        if m is not None and r is not None:
            groups.setdefault(bucket(m), []).append(r)
    for _, _, name in BUCKETS:
        if name in groups:
            print(f"  moved {name:<11} {fmt(summary(groups[name]))}")
    rule = extension_rule(pairs)
    if rule["status"] != "evaluated":
        print(f"  skip rule: {rule['status']}")
    else:
        print(f"  selection period (first 60 %): baseline {fmt(rule['selection_baseline'])}")
        for x, s in rule["sweep"]:
            print(f"    skip when moved > {x * 100:>4.0f} %: {fmt(s)}")
        if rule["chosen"] is None:
            print(f"  no threshold had n >= {MIN_N} in the selection period: nothing chosen")
        else:
            print(f"  chosen on the selection period: skip when moved > {rule['chosen'] * 100:.0f} %")
            print(f"  out of sample (from {rule['oos_start'][:16]}): baseline {fmt(rule['oos_baseline'])}")
            print(f"  out of sample, with the rule:            {fmt(rule['oos_rule'])}")
    print("\nRead with care: a change is justified only if it helps out of sample, after costs, without depending on the "
          "single best signal. Nothing was written.")


async def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="entry approach comparison, read-only")
    ap.add_argument("--days", type=int, default=14)
    args = ap.parse_args(argv)
    settings = get_settings()
    engine = make_engine(settings, pool_size=1, max_overflow=0)
    sf = make_session_factory(engine)
    try:
        async with sf() as s:
            await report(s, args.days)
    finally:
        await engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
