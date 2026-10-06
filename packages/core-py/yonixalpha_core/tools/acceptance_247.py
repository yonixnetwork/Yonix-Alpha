"""24/7 acceptance report (master §62, §81): did the server keep working while
nobody was watching? Read-only: database and Redis reads, nothing changed.

The §81 procedure is in docs/ACCEPTANCE_24x7.md: note the UTC time, start
AUTO, close the browser, turn phone data off, wait, then run

    $C run --rm decision-engine python -m yonixalpha_core.tools.acceptance_247 --since 2026-10-05T01:00:00Z

and again after a server restart (step 6 of the procedure).

For the window it reports:
  1. Services: each one's heartbeat now, and its restarts inside the window
     (service_started events); more than MAX_RESTARTS is a crash loop.
  2. Each §81 duty, from what the workers wrote: events in the window, the
     newest one, and the longest silence between two of them (window edges
     included), against a limit per duty.
  3. Restart and reconciliation: LIVE wallet reconciliation time, its
     findings, and EVM scan ranges skipped and backfilled.
  4. History: rows older than the window are still there, so the dashboard
     shows them when it is reopened.

Verdict per duty:
  ACTIVE    events in the window, no silence longer than the limit;
  GAP       events, but a silence longer than the limit (a stop, or a quiet market);
  FAIL      no event although the source never stops (Solana, BSC and
            Robinhood discovery and trades; both EVM chains always run in
            data-evm, so this is FAIL unless every launchpad of that chain
            was switched off on the Launchpads page);
  NO EVENT  no event where one depends on the market (copying needs a
            target to trade, exits need an open position): not a failure,
            and not a proof either.
The result is PASS only with no FAIL, no stale heartbeat and no service
restarting repeatedly. It proves
continuity, never profit.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text

from yonixalpha_core.config import get_settings
from yonixalpha_core.db.base import make_engine, make_session_factory

SERVICES = ["data-solana", "engine-solana-discovery", "decision-engine", "ml", "paper-trading", "data-evm", "copy-engine"]
HEARTBEAT_STALE_SECONDS = 90  # the health page's limit (apps/api health_state)
# More starts than this inside one window is a crash loop, not an operator
# restart (a deliberate restart can add a second start when a service comes
# up before the database): FAIL, even if the heartbeat is fresh right now.
# Server 2026-10-06: copy-engine started 318 times in 25.7 h and the report
# still said PASS.
MAX_RESTARTS = 3


@dataclass(frozen=True)
class Duty:
    key: str
    label: str  # the §81 duty it proves
    table: str
    column: str
    where: str = ""
    continuous: bool = True  # the source never stops while the system runs
    max_gap_minutes: int = 15
    # rows are updated in place (a daily rollup): only the newest update is
    # evidence, so the silence measured is the one since it, never between rows
    newest_only: bool = False


DUTIES: tuple[Duty, ...] = (
    Duty("solana_discovery", "discovering: Solana launches observed", "token_observations", "launched_at", max_gap_minutes=10),
    Duty("solana_decisions", "monitoring: Solana observations decided", "token_observations", "decided_at", max_gap_minutes=10),
    Duty("bsc_discovery", "discovering: BSC launches", "evm_tokens", "created_at", "chain = 'bsc'", max_gap_minutes=10),
    Duty("bsc_trades", "monitoring: BSC launchpad trades", "evm_trades", "at", "chain = 'bsc'", max_gap_minutes=5),
    Duty("robinhood_discovery", "discovering: Robinhood launches", "evm_tokens", "created_at", "chain = 'robinhood'",
         max_gap_minutes=20),
    Duty("robinhood_trades", "monitoring: Robinhood launchpad trades", "evm_trades", "at", "chain = 'robinhood'",
         max_gap_minutes=10),
    Duty("evm_observations", "monitoring: BSC / Robinhood observations moving", "evm_observations", "state_at",
         max_gap_minutes=10),
    # launchpad_activity is one row per launchpad and day, overwritten on every
    # write: the gaps between rows are not silences (server 2026-10-06: "13.0 h"
    # between two launchpads' last writes while the feeds were ACTIVE)
    Duty("launchpad_probe", "monitoring: launchpad activity rollup (newest write)", "launchpad_activity", "updated_at",
         max_gap_minutes=20, newest_only=True),
    Duty("copy_events", "copying: target wallet trades seen and decided", "copy_events", "detected_at",
         continuous=False, max_gap_minutes=240),
    Duty("evm_position_ticks", "managing positions: BSC / Robinhood exit checks", "evm_exit_samples", "at",
         continuous=False, max_gap_minutes=10),
    Duty("timeline", "updating exits: position timeline events", "trade_timeline_events", "occurred_at",
         continuous=False, max_gap_minutes=240),
    Duty("closed_positions", "recording PnL: positions closed", "paper_positions", "exit_at", "realized_pnl IS NOT NULL",
         continuous=False, max_gap_minutes=24 * 60),
)


def verdict(duty: Duty, count: int, longest_gap_s: float | None) -> str:
    if count == 0:
        return "FAIL" if duty.continuous else "NO EVENT"
    if longest_gap_s is not None and longest_gap_s > duty.max_gap_minutes * 60:
        return "GAP"
    return "ACTIVE"


def heartbeat_state(hb: dict | None, now: datetime) -> tuple[str, float | None]:
    if not hb or not hb.get("at"):
        return "NONE", None
    age = (now - datetime.fromisoformat(hb["at"])).total_seconds()
    if hb.get("status") == "disabled":
        return "DISABLED", age
    return ("OK" if age <= HEARTBEAT_STALE_SECONDS else "STALE"), age


def _fmt_s(s: float | None) -> str:
    if s is None:
        return "-"
    return f"{s / 3600:.1f} h" if s >= 3600 else f"{s / 60:.1f} min" if s >= 60 else f"{s:.0f} s"


async def duty_stats(session, duty: Duty, since: datetime, until: datetime) -> dict[str, Any]:
    """Count, newest, and the longest silence inside [since, until], window
    edges included, computed in SQL (a busy BSC day is about a million trades)."""
    cond = f"{duty.column} >= :since AND {duty.column} <= :until" + (f" AND {duty.where}" if duty.where else "")
    q = text(f"""
        SELECT count(*) AS n, max(ts) AS newest, max(gap) AS longest
        FROM (SELECT {duty.column} AS ts,
                     EXTRACT(EPOCH FROM {duty.column} - lag({duty.column}) OVER (ORDER BY {duty.column})) AS gap
              FROM {duty.table} WHERE {cond}) s""")
    row = (await session.execute(q, {"since": since, "until": until})).one()
    n, newest, inner = int(row.n), row.newest, row.longest
    first = None
    if n:
        first = (await session.execute(text(f"SELECT min({duty.column}) FROM {duty.table} WHERE {cond}"),
                                       {"since": since, "until": until})).scalar_one()
    longest = None
    if n and duty.newest_only:
        longest = (until - newest).total_seconds()
    elif n:  # the silence before the first event and after the newest one count too
        longest = max(float(inner or 0), (first - since).total_seconds(), (until - newest).total_seconds())
    older = (await session.execute(text(f"SELECT count(*) FROM {duty.table} WHERE {duty.column} < :since"
                                        + (f" AND {duty.where}" if duty.where else "")), {"since": since})).scalar_one()
    return {"count": n, "newest": newest, "longest_gap_s": longest, "older": int(older)}


async def collect(session, redis, since: datetime, until: datetime, now: datetime) -> dict[str, Any]:
    from yonixalpha_core.events import read_heartbeats

    out: dict[str, Any] = {"since": since, "until": until, "services": {}, "duties": [], "restart": {}}
    hbs = await read_heartbeats(redis, SERVICES) if redis is not None else {}
    starts = dict((await session.execute(text(
        "SELECT service, count(*) FROM system_events WHERE event_type = 'service_started' "
        "AND created_at >= :since AND created_at <= :until GROUP BY service"), {"since": since, "until": until})).all())
    for svc in SERVICES:
        state, age = heartbeat_state(hbs.get(svc), now)
        out["services"][svc] = {"heartbeat": state, "age_s": age, "restarts": int(starts.get(svc, 0))}
    for duty in DUTIES:
        st = await duty_stats(session, duty, since, until)
        st.update(key=duty.key, label=duty.label, limit_s=duty.max_gap_minutes * 60, continuous=duty.continuous,
                  verdict=verdict(duty, st["count"], st["longest_gap_s"]))
        out["duties"].append(st)
    out["open_positions"] = (await session.execute(text(
        "SELECT count(*), min(last_marked_at) FROM paper_positions WHERE status = 'open'"))).one()._asdict()
    out["restart"]["reconciliation_events"] = dict((await session.execute(text(
        "SELECT kind, count(*) FROM reconciliation_events WHERE created_at >= :since AND created_at <= :until GROUP BY kind"),
        {"since": since, "until": until})).all())
    out["restart"]["scan_gaps"] = [r._asdict() for r in (await session.execute(text(
        "SELECT chain, launchpad, count(*) AS ranges, count(*) FILTER (WHERE status = 'DONE') AS backfilled, "
        "count(*) FILTER (WHERE status = 'PENDING') AS pending, count(*) FILTER (WHERE status IN ('FAILED', 'EXPIRED')) AS lost "
        "FROM evm_scan_gaps WHERE detected_at >= :since AND detected_at <= :until GROUP BY chain, launchpad"),
        {"since": since, "until": until})).all()]
    wallet = await redis.get("yx:live:wallet") if redis is not None else None
    out["restart"]["live_reconciled_at"] = json.loads(wallet).get("at") if wallet else None
    pm = await redis.get("yx:pm:last_pass") if redis is not None else None
    out["position_loop"] = json.loads(pm) if pm else None
    return out


def render(r: dict[str, Any]) -> tuple[str, bool]:
    lines = [f"24/7 acceptance window {r['since'].isoformat()} .. {r['until'].isoformat()} "
             f"({_fmt_s((r['until'] - r['since']).total_seconds())})", "", "SERVICES (heartbeat now, restarts in the window)"]
    bad = False
    for svc, s in r["services"].items():
        flag = s["heartbeat"] in ("NONE", "STALE")
        looping = s["restarts"] > MAX_RESTARTS
        bad |= flag or looping
        lines.append(f"  {svc:26s} {s['heartbeat']:9s} age {_fmt_s(s['age_s']):>9s}   restarts {s['restarts']}"
                     + ("   <- not running" if flag else "")
                     + (f"   <- restarting repeatedly (more than {MAX_RESTARTS})" if looping else ""))
    lines += ["", "DUTIES (events in the window, newest, longest silence vs limit)"]
    for d in r["duties"]:
        bad |= d["verdict"] == "FAIL"
        newest = d["newest"].isoformat(timespec="seconds") if d["newest"] else "-"
        lines.append(f"  {d['verdict']:8s} {d['label']}: {d['count']} events, newest {newest}, longest silence "
                     f"{_fmt_s(d['longest_gap_s'])} (limit {_fmt_s(d['limit_s'])}); history before the window {d['older']}")
    op = r["open_positions"]
    lines += ["", f"OPEN POSITIONS now: {op['count']}" + (f", oldest mark {op['min'].isoformat(timespec='seconds')}"
                                                         if op.get("min") else "")]
    pl = r.get("position_loop")
    lines.append(f"  Solana position loop last pass: {pl.get('at') if pl else 'not reported'}")
    rs = r["restart"]
    lines += ["", "RESTART / RECONCILIATION",
              f"  LIVE wallet reconciled at: {rs['live_reconciled_at'] or 'not reported (no LIVE wallet configured, or no run in the last hour)'}",
              f"  reconciliation findings in the window: {rs['reconciliation_events'] or 'none'}"]
    for g in rs["scan_gaps"]:
        lines.append(f"  EVM scan ranges skipped: {g['chain']} {g['launchpad']} {g['ranges']}: backfilled {g['backfilled']}, "
                     f"pending {g['pending']}, not recovered {g['lost']}")
    lines += ["", "RESULT: " + ("FAIL (see the lines marked FAIL / not running / restarting repeatedly)" if bad else
                                "PASS: every service alive and every continuous source kept producing; "
                                "NO EVENT lines are market-dependent, GAP lines need a look")]
    return "\n".join(lines), not bad


def _parse_time(s: str) -> datetime:
    t = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


async def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--since", required=True, help="UTC start of the unattended window, e.g. 2026-10-05T01:00:00Z")
    ap.add_argument("--until", help="end of the window (default: now)")
    a = ap.parse_args(argv)
    now = datetime.now(timezone.utc)
    since, until = _parse_time(a.since), _parse_time(a.until) if a.until else now
    if until <= since:
        print("--until must be after --since")
        return 2
    settings = get_settings()
    from yonixalpha_core.db.redis import make_redis

    engine = make_engine(settings)
    redis = make_redis(settings)
    try:
        async with make_session_factory(engine)() as session:
            report = await collect(session, redis, since, until, now)
    finally:
        await engine.dispose()
        await redis.aclose()
    text_out, ok = render(report)
    print(text_out)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
