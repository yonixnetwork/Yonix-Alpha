"""Automatic vs manual exit diagnosis (master upgrade §46-47): origin from
the position timeline, stage latencies, retried exits, and findings that
only state what the numbers show."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from yonixalpha_core.tools.exit_diagnosis import ExitRow, classify, render, summarize

T = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
S = timedelta(seconds=1)


def order(reason, created, decided=None):
    return SimpleNamespace(reason=reason, created_at=created,
                           diagnostics={"decision": {"decision_at": (decided or created).isoformat()}})


def test_origin_comes_from_the_timeline_not_from_the_reason_alone():
    tl = [("copy_exit_requested", T - 5 * S), ("operator_exit", T - 3 * S)]
    assert classify(order("manual_exit", T), tl) == ("MANUAL", T - 3 * S)
    assert classify(order("manual_exit", T), [("copy_exit_requested", T - 2 * S)]) == ("COPY", T - 2 * S)
    # a timeline event after the order does not explain it
    assert classify(order("manual_exit", T), [("operator_exit", T + S)])[0] == "MANUAL"
    assert classify(order("stop_loss", T, T - S), tl) == ("AUTOMATIC", T - S)


def row(origin, status, pos, created, signed=None, confirmed=None, stage=None, error=None, slip=15.0, reason="stop_loss"):
    return ExitRow(order_id=f"{pos}-{created.timestamp()}", position_id=pos, origin=origin, reason=reason, status=status,
                   trigger_at=created - 2 * S if origin == "MANUAL" else created, created_at=created,
                   submitted_at=signed, confirmed_at=confirmed, finished_at=created + 30 * S if status == "FAILED" else None,
                   stage=stage, error=error, attempts=1, slippage_pct=slip, min_out_set=True, route="pump")


def test_summary_shows_where_automatic_sells_lose_time_and_retries():
    rows = [
        # automatic: first sell fails on slippage, a second one confirms slowly
        row("AUTOMATIC", "FAILED", "p1", T, T + S, None, "TRANSACTION_FAILED", "slippage: too little SOL received", slip=5),
        row("AUTOMATIC", "CONFIRMED", "p1", T + 40 * S, T + 45 * S, T + 60 * S, slip=10),
        row("AUTOMATIC", "CONFIRMED", "p2", T, T + 4 * S, T + 20 * S, slip=5),
        # manual: fast, one sell each
        row("MANUAL", "CONFIRMED", "p3", T, T + S, T + 3 * S, reason="manual_exit", slip=15),
        row("MANUAL", "CONFIRMED", "p4", T, T + S, T + 3 * S, reason="manual_exit", slip=15),
    ]
    rep = summarize(rows, external_sells=2)
    a, m = rep["origins"]["AUTOMATIC"], rep["origins"]["MANUAL"]
    assert a["orders"] == 3 and a["positions_needing_a_second_sell"] == 1 and m["positions_needing_a_second_sell"] == 0
    assert a["failure_stages"] == [("TRANSACTION_FAILED", 1)] and a["confirmed_pct"] == 66.7
    assert m["latency_ms"]["signed_to_confirmed_ms"]["median"] == 2000.0
    text = " | ".join(rep["comparison"])
    assert "confirm less often" in text and "landing / confirmation" in text and "second sell" in text
    assert "tighter slippage" in text
    out = render(rep, 30)
    assert "sold elsewhere): 2" in out and "== AUTOMATIC" in out and "== MANUAL" in out


def test_no_comparison_is_invented_without_both_kinds_of_sell():
    rep = summarize([row("AUTOMATIC", "CONFIRMED", "p1", T, T + S, T + 2 * S)])
    assert rep["comparison"] == ["not enough data: needs both automatic and manual LIVE sells in the window"]


async def test_loader_reads_live_sells_and_external_sells_from_the_real_schema():
    import os
    from decimal import Decimal

    from sqlalchemy.ext.asyncio import create_async_engine

    from yonixalpha_core.db import models  # noqa: F401
    from yonixalpha_core.db.base import Base, make_session_factory
    from yonixalpha_core.db.models import ExecutionOrder, ReconciliationEvent
    from yonixalpha_core.tools.exit_diagnosis import load

    engine = create_async_engine(os.environ.get(
        "DATABASE_URL", "postgresql+asyncpg://yonixalpha:yonixalpha_test_pw@localhost:5432/yonixalpha_test"))
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    try:
        async with make_session_factory(engine)() as s:
            base = dict(mode="LIVE", mint="Mint1", provider="pumpportal", route="pump", amount="1", amount_kind="tokens",
                        slippage_pct=Decimal("10"), priority_fee_sol=Decimal("0.0001"))
            s.add(ExecutionOrder(side="SELL", reason="stop_loss", status="CONFIRMED", idempotency_key="a",
                                 created_at=T, submitted_at=T + S, confirmed_at=T + 3 * S,
                                 limits={"min_sol_out_lamports": 5}, result={"stage": "CONFIRMED"}, **base))
            s.add(ExecutionOrder(side="SELL", reason="manual_exit", status="FAILED", idempotency_key="b", created_at=T,
                                 error="blockhash expired", result={"stage": "EXPIRED"}, **base))
            s.add(ExecutionOrder(side="BUY", reason="entry", status="CONFIRMED", idempotency_key="c", created_at=T, **base))
            s.add(ExecutionOrder(side="SELL", reason="stop_loss", status="CONFIRMED", idempotency_key="d",
                                 created_at=T - timedelta(days=60), **{**base, "mode": "PAPER"}))
            s.add(ReconciliationEvent(kind="position_tokens_missing", severity="critical", created_at=T))
            await s.commit()
            rows, external = await load(s, T - timedelta(days=1))
        assert sorted((r.origin, r.status) for r in rows) == [("AUTOMATIC", "CONFIRMED"), ("MANUAL", "FAILED")]
        auto = next(r for r in rows if r.origin == "AUTOMATIC")
        assert auto.min_out_set and auto.total_ms == 3000.0 and external == 1
    finally:
        await engine.dispose()
