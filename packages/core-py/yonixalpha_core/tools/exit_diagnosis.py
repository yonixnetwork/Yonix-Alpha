"""Automatic vs manual exits: what actually happened to every LIVE sell
(master upgrade §46-47). Read-only; changes nothing.

Automatic and manual exits share one path: the dashboard (or CLOSE
POSITIONS, or the copy engine) sets `exit_requested`, and the Solana
position loop turns it into a SELL order exactly like a stop loss or a
take-profit (`live_trading.manage_live_position` → `request_live_exit` →
the order worker). So a difference between them has to show up in the
recorded orders — when the exit was triggered, how long it waited for the
position loop, how long signing / landing took, which stage failed, which
slippage and minimum output it carried, and whether a second sell was
needed. This report lays those side by side per origin:

  MANUAL     operator_exit on the position timeline (dashboard sell,
             paper/positions exit, CLOSE POSITIONS / EMERGENCY EXIT)
  COPY       copy_exit_requested (a copy target sold)
  AUTOMATIC  stop loss, take-profits, trailing stop, exit intelligence

It also counts positions whose tokens left the wallet outside YonixAlpha
(reconciliation "position_tokens_missing"): a sell made in another wallet
app never produces an order here, so it cannot be compared stage by stage.

On the server, in /opt/yonixalpha:

    docker compose --env-file .env -f infra/docker/docker-compose.yml \\
        -f infra/docker/docker-compose.prod.yml run --rm paper-trading \\
        python -m yonixalpha_core.tools.exit_diagnosis [--days 30] [--json]
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

MANUAL_EVENTS = {"operator_exit": "MANUAL", "copy_exit_requested": "COPY"}
FAILED = ("FAILED", "EXPIRED")


@dataclass
class ExitRow:
    order_id: str
    position_id: str | None
    origin: str  # MANUAL | COPY | AUTOMATIC
    reason: str
    status: str
    trigger_at: datetime
    created_at: datetime
    submitted_at: datetime | None
    confirmed_at: datetime | None
    finished_at: datetime | None
    stage: str | None
    error: str | None
    attempts: int
    slippage_pct: float | None
    min_out_set: bool
    route: str | None

    def ms(self, a: datetime | None, b: datetime | None) -> float | None:
        return None if a is None or b is None else round((b - a).total_seconds() * 1000, 1)

    @property
    def trigger_to_order_ms(self):
        return self.ms(self.trigger_at, self.created_at)

    @property
    def order_to_signed_ms(self):
        return self.ms(self.created_at, self.submitted_at)

    @property
    def signed_to_confirmed_ms(self):
        return self.ms(self.submitted_at, self.confirmed_at)

    @property
    def total_ms(self):
        return self.ms(self.trigger_at, self.confirmed_at or self.finished_at)


def classify(order: Any, timeline: list[tuple[str, datetime]]) -> tuple[str, datetime]:
    """(origin, trigger time) of one SELL order, from its reason and the
    position timeline events before it."""
    decided = ((order.diagnostics or {}).get("decision") or {}).get("decision_at")
    trigger = datetime.fromisoformat(decided) if decided else order.created_at
    if order.reason == "manual_exit":
        before = [(t, at) for t, at in timeline if t in MANUAL_EVENTS and at <= order.created_at]
        if before:
            kind, at = max(before, key=lambda x: x[1])
            return MANUAL_EVENTS[kind], at
        return "MANUAL", trigger
    return "AUTOMATIC", trigger


def _pct(values: list[float], q: float) -> float | None:
    v = sorted(x for x in values if x is not None)
    if not v:
        return None
    return v[min(len(v) - 1, int(round(q * (len(v) - 1))))]


def summarize(rows: list[ExitRow], external_sells: int = 0) -> dict[str, Any]:
    groups: dict[str, list[ExitRow]] = defaultdict(list)
    for r in rows:
        groups[r.origin].append(r)
    out: dict[str, Any] = {"origins": {}, "external_sells": external_sells, "orders": len(rows)}
    for origin, rs in sorted(groups.items()):
        by_pos: dict[str, list[ExitRow]] = defaultdict(list)
        for r in rs:
            by_pos[r.position_id or r.order_id].append(r)
        retried = 0
        for seq in by_pos.values():
            seq.sort(key=lambda r: r.created_at)
            first_ok = next((i for i, r in enumerate(seq) if r.status == "CONFIRMED"), None)
            if first_ok is not None and any(r.status in FAILED for r in seq[:first_ok]):
                retried += 1
        n = len(rs)
        status = Counter(r.status for r in rs)
        stat = {}
        for name in ("trigger_to_order_ms", "order_to_signed_ms", "signed_to_confirmed_ms", "total_ms"):
            vals = [getattr(r, name) for r in rs if r.status == "CONFIRMED" or name == "trigger_to_order_ms"]
            stat[name] = {"median": _pct(vals, 0.5), "p90": _pct(vals, 0.9), "max": _pct(vals, 1.0)}
        failed = [r for r in rs if r.status in FAILED]
        out["origins"][origin] = {
            "orders": n, "status": dict(status),
            "confirmed_pct": round(100 * status.get("CONFIRMED", 0) / n, 1) if n else None,
            "latency_ms": stat,
            "positions": len(by_pos), "positions_needing_a_second_sell": retried,
            "failure_stages": Counter(r.stage or "unknown" for r in failed).most_common(6),
            "failure_errors": Counter((r.error or "")[:90] for r in failed).most_common(6),
            "reasons": Counter(r.reason for r in rs).most_common(8),
            "slippage_pct": {"median": _pct([r.slippage_pct for r in rs], 0.5), "max": _pct([r.slippage_pct for r in rs], 1.0)},
            "min_output_set_pct": round(100 * sum(r.min_out_set for r in rs) / n, 1) if n else None,
            "routes": Counter(r.route or "?" for r in rs).most_common(4),
            "avg_attempts": round(sum(r.attempts for r in rs) / n, 2) if n else None,
        }
    auto, man = out["origins"].get("AUTOMATIC"), out["origins"].get("MANUAL")
    out["comparison"] = _compare(auto, man)
    return out


def _compare(auto: dict | None, man: dict | None) -> list[str]:
    """Plain findings, each backed by the numbers above; never a guess."""
    if not auto or not man:
        return ["not enough data: needs both automatic and manual LIVE sells in the window"]
    notes = []
    a, m = auto["confirmed_pct"], man["confirmed_pct"]
    if a is not None and m is not None and a + 10 < m:
        notes.append(f"automatic sells confirm less often ({a}% vs {m}% manual): see failure_stages / failure_errors")
    for k, label in (("trigger_to_order_ms", "waiting for the position loop"), ("order_to_signed_ms", "build + sign + submit"),
                     ("signed_to_confirmed_ms", "landing / confirmation")):
        am, mm = auto["latency_ms"][k]["median"], man["latency_ms"][k]["median"]
        if am is not None and mm is not None and am > 2 * mm + 500:
            notes.append(f"automatic sells are slower at {label}: median {am:.0f} ms vs {mm:.0f} ms manual")
    if auto["positions_needing_a_second_sell"] > man["positions_needing_a_second_sell"]:
        notes.append(f"{auto['positions_needing_a_second_sell']} automatic exits needed a second sell "
                     f"({man['positions_needing_a_second_sell']} manual)")
    sa, sm = auto["slippage_pct"]["median"], man["slippage_pct"]["median"]
    if sa is not None and sm is not None and sa < sm:
        notes.append(f"automatic sells carried tighter slippage (median {sa}% vs {sm}%)")
    return notes or ["no significant difference between automatic and manual sells in this window"]


async def load(session, since: datetime) -> tuple[list[ExitRow], int]:
    from sqlalchemy import func, select

    from yonixalpha_core.db.models import ExecutionOrder, ReconciliationEvent, TradeTimelineEvent

    orders = (await session.execute(select(ExecutionOrder).where(
        ExecutionOrder.mode == "LIVE", ExecutionOrder.side == "SELL", ExecutionOrder.created_at >= since)
        .order_by(ExecutionOrder.created_at))).scalars().all()
    pids = {o.position_id for o in orders if o.position_id}
    tl: dict[Any, list[tuple[str, datetime]]] = defaultdict(list)
    if pids:
        for pid, kind, at in (await session.execute(select(
                TradeTimelineEvent.position_id, TradeTimelineEvent.event_type, TradeTimelineEvent.occurred_at).where(
                TradeTimelineEvent.position_id.in_(pids), TradeTimelineEvent.event_type.in_(tuple(MANUAL_EVENTS))))).all():
            tl[pid].append((kind, at))
    rows = []
    for o in orders:
        origin, trigger = classify(o, tl.get(o.position_id, []))
        rows.append(ExitRow(
            order_id=str(o.id), position_id=str(o.position_id) if o.position_id else None, origin=origin,
            reason=o.reason, status=o.status, trigger_at=trigger, created_at=o.created_at,
            submitted_at=o.submitted_at, confirmed_at=o.confirmed_at,
            finished_at=o.updated_at if o.status in FAILED else None, stage=(o.result or {}).get("stage"),
            error=o.error, attempts=o.attempts or 0,
            slippage_pct=float(o.slippage_pct) if o.slippage_pct is not None else None,
            min_out_set=bool((o.limits or {}).get("min_sol_out_lamports")), route=o.route))
    external = (await session.execute(select(func.count()).select_from(ReconciliationEvent).where(
        ReconciliationEvent.kind == "position_tokens_missing", ReconciliationEvent.created_at >= since))).scalar_one()
    return rows, int(external)


def render(report: dict[str, Any], days: int) -> str:
    lines = [f"EXIT DIAGNOSIS: LIVE sells in the last {days} days ({report['orders']} orders)", ""]
    for origin, g in report["origins"].items():
        lat = g["latency_ms"]
        lines += [f"== {origin}: {g['orders']} orders on {g['positions']} positions, confirmed {g['confirmed_pct']}%, "
                  f"status {g['status']}",
                  "   latency ms (median / p90 / max):"]
        for k in ("trigger_to_order_ms", "order_to_signed_ms", "signed_to_confirmed_ms", "total_ms"):
            v = lat[k]
            lines.append(f"     {k:24} {v['median']} / {v['p90']} / {v['max']}")
        lines += [f"   positions needing a second sell: {g['positions_needing_a_second_sell']}",
                  f"   slippage % median/max: {g['slippage_pct']['median']} / {g['slippage_pct']['max']}; "
                  f"min output set on {g['min_output_set_pct']}% of orders; avg attempts {g['avg_attempts']}",
                  f"   routes: {g['routes']}", f"   reasons: {g['reasons']}",
                  f"   failure stages: {g['failure_stages']}", f"   failure errors: {g['failure_errors']}", ""]
    lines.append(f"Positions whose tokens left the wallet outside YonixAlpha (sold elsewhere): {report['external_sells']}")
    lines += ["", "FINDINGS:"] + [f" - {n}" for n in report["comparison"]]
    return "\n".join(lines)


async def main() -> int:
    from yonixalpha_core.config import get_settings
    from yonixalpha_core.db.base import make_engine, make_session_factory

    ap = argparse.ArgumentParser(description="automatic vs manual LIVE sells (read-only)")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    engine = make_engine(get_settings())
    try:
        async with make_session_factory(engine)() as session:
            rows, external = await load(session, datetime.now(timezone.utc) - timedelta(days=args.days))
        report = summarize(rows, external)
        if args.json:
            report["rows"] = [{**asdict(r), "total_ms": r.total_ms} for r in rows]
            print(json.dumps(report, default=str, indent=2))
        else:
            print(render(report, args.days))
    finally:
        await engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
