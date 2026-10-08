"""Solana performance report, PAPER vs LIVE (yonixalpha_core.solana_performance):
stage, hold-time, entry-quality and exit-reason splits, profit factor, max
drawdown, MFE / MAE, missed winners, false positives and LIVE execution
telemetry, all aggregated from seeded rows with hand-computed answers."""

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from yonixalpha_core.db.models import ExecutionOrder, OpportunityOutcome, PaperPosition

NOW = datetime.now(timezone.utc).replace(microsecond=0)


def pos(mode, engine, pnl, pct, hold_s, exit_reason, *, high="1.5", low="0.8", plan=None, minutes_ago=60):
    exit_at = NOW - timedelta(minutes=minutes_ago)
    return PaperPosition(id=uuid.uuid4(), symbol="T", provider="paper", side="LONG", engine=engine,
                         entry_price=Decimal(1), quantity=Decimal(1), take_profit=[], status="closed",
                         execution_mode=mode, realized_pnl=Decimal(pnl), realized_pnl_pct=Decimal(pct),
                         fees_paid_quote=Decimal("0.001"), highest_price=Decimal(high), lowest_price=Decimal(low),
                         entry_at=exit_at - timedelta(seconds=hold_s), exit_at=exit_at, exit_reason=exit_reason,
                         plan=plan or {"lifecycle": "FRESH"})


def outcome(p, snapshot, **kw):
    return OpportunityOutcome(key=f"gate:{p.id}", mint=f"M{p.id.hex[:20]}", engine=p.engine, stage="GATE",
                              decision="EXECUTE", traded=True, position_id=p.id, execution_mode=p.execution_mode,
                              decided_at=p.entry_at, snapshot=snapshot, **kw)


async def test_solana_performance_splits_paper_and_live_with_measured_numbers(app, client, auth_headers):
    fresh = pos("PAPER", "solana_fresh", "0.02", "0.20", 5, "take_profit_1", minutes_ago=50)
    near = pos("PAPER", "solana_fresh", "-0.01", "-0.10", 20, "stop_loss", minutes_ago=40)
    migrated = pos("PAPER", "solana_fresh", "0.01", "0.10", 120, "trailing_stop", plan={"lifecycle": "MIGRATED"},
                   minutes_ago=30)
    momentum = pos("PAPER", "solana_momentum", "-0.03", "-0.30", 4000, "stop_loss", minutes_ago=20)
    live = pos("LIVE", "solana_fresh", "0.005", "0.05", 45, "take_profit_1", high="1.2", low="0.9")
    old = pos("PAPER", "solana_fresh", "9", "9", 5, "take_profit_1", minutes_ago=60 * 24 * 10)  # outside the window
    evm = pos("PAPER", "evm_bsc", "9", "9", 5, "take_profit_1")  # not Solana
    async with app.state.db_session_factory() as s:
        s.add_all([fresh, near, migrated, momentum, live, old, evm])
        await s.flush()
        s.add_all([
            outcome(fresh, {"curve_progress": "0.31", "deterioration_indicators": []}),
            outcome(near, {"curve_progress": "0.82", "deterioration_indicators": ["sell_pressure"]},
                    trade_result={"pnl_sol": "-0.01"}, loss_analysis={"classification": "BAD_ENTRY"}),
            outcome(momentum, {"curve_progress": "0.95"}, trade_result={"pnl_sol": "-0.03"},
                    post_exit={"classification": "POSSIBLY_EARLY"}),
            OpportunityOutcome(key="gate:missed", mint="Mmissed", engine="solana_fresh", stage="GATE", decision="REJECT",
                               traded=False, reasons=["LOW_LIQUIDITY: 3 SOL"], decided_at=NOW - timedelta(hours=2),
                               snapshot={}, peak_pct=Decimal(140),
                               analysis={"counterfactual": {"classification": "MISSED_WIN"}}),
            ExecutionOrder(mode="LIVE", side="BUY", reason="entry", mint="Mlive", provider="pumpportal_local", route="pump",
                           amount="0.05", amount_kind="sol", slippage_pct=Decimal(15), priority_fee_sol=Decimal("0.0001"),
                           idempotency_key="k1", status="CONFIRMED",
                           diagnostics={"timing": {"decision_to_submit_ms": 420, "decision_to_confirm_ms": 1900},
                                        "price": {"components_pct": {"vs_expected_pct": "1.5",
                                                                     "total_vs_decision_pct": "4.2"}}}),
        ])
        await s.commit()

    r = await client.get("/api/analytics/solana-performance?days=7", headers=auth_headers)
    assert r.status_code == 200, r.text
    d = r.json()
    paper, lv = d["trades_by_mode"]["PAPER"], d["trades_by_mode"]["LIVE"]
    o = paper["overall"]
    assert (o["trades"], o["wins"], o["losses"], o["win_rate_pct"]) == (4, 2, 2, 50.0)
    assert o["net_pnl_sol"] == -0.01 and o["fees_sol"] == 0.004
    assert o["profit_factor"] == 0.75  # 0.03 won / 0.04 lost
    assert (o["avg_win_pct"], o["avg_loss_pct"]) == (15.0, -20.0)
    assert o["avg_mfe_pct"] == 50.0 and o["avg_mae_pct"] == -20.0
    # cumulative +0.02, +0.01, +0.02, -0.01: peak 0.02, deepest -0.01
    assert o["max_drawdown_sol"] == 0.03
    assert list(paper["by_stage"]) == ["FRESH", "NEAR_MIGRATION", "MIGRATED", "MOMENTUM"]
    assert all(v["trades"] == 1 for v in paper["by_stage"].values())
    assert paper["by_hold"] == {k: paper["by_hold"][k] for k in ("<10s", "10-30s", "1-5m", ">60m")}
    assert paper["by_entry_quality"]["CLEAN"]["trades"] == 1 and paper["by_entry_quality"]["DETERIORATING"]["trades"] == 1
    assert paper["by_entry_quality"]["UNKNOWN"]["trades"] == 2
    assert paper["by_exit_reason"]["stop_loss"]["trades"] == 2 and o["anecdotal"] is True
    assert lv["overall"]["trades"] == 1 and lv["by_hold"] == {"30s-1m": lv["by_hold"]["30s-1m"]}
    assert lv["overall"]["profit_factor"] == "no losses"
    assert d["entries_by_mode"]["PAPER"]["entries"] == 4 and d["entries_by_mode"]["LIVE"]["exited"] == 1
    assert d["missed_winners_by_rule"] == [{"rule": "LOW_LIQUIDITY", "count": 1, "median_peak_pct": 140.0}]
    assert d["false_positives_by_mode"] == {"PAPER": {"BAD_ENTRY": 1, "UNCLASSIFIED": 1}}
    assert d["exit_timing_by_mode"] == {"PAPER": {"POSSIBLY_EARLY": 1}}
    ex = d["execution_live"][0]
    assert (ex["side"], ex["route"], ex["orders"], ex["median_decision_to_confirm_ms"],
            ex["median_slippage_vs_expected_pct"]) == ("BUY", "pump", 1, 1900, 1.5)
    assert d["decisions_by_target"] == {}  # no gate decisions seeded: an empty section, not invented numbers
    assert "no per-trade slippage record" in d["definitions"]["slippage"]

    bare = (await client.get("/api/analytics/solana-performance?days=1&outcomes=false", headers=auth_headers)).json()
    assert "missed_winners_by_rule" not in bare and bare["days"] == 1
    assert (await client.get("/api/analytics/solana-performance?days=0", headers=auth_headers)).status_code == 422
