"""Strategy comparison API (memecoin refocus, 2026-10-20): closed Solana
positions by strategy, token category and both, PAPER and LIVE apart, with
the net without the single best trade."""

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from yonixalpha_core.db.models import PaperPosition

NOW = datetime.now(timezone.utc).replace(microsecond=0)


def pos(mode, engine, pnl, *, symbol="T", category="FRESH", hours_ago=2, cost="0.05"):
    exit_at = NOW - timedelta(hours=hours_ago)
    return PaperPosition(id=uuid.uuid4(), symbol=symbol, provider="paper", side="LONG", engine=engine,
                         entry_price=Decimal(1), quantity=Decimal(1), take_profit=[], status="closed",
                         execution_mode=mode, realized_pnl=Decimal(pnl), realized_pnl_pct=Decimal(pnl) / Decimal(cost),
                         entry_cost_quote=Decimal(cost), fees_paid_quote=Decimal("0.001"),
                         entry_at=exit_at - timedelta(seconds=60), exit_at=exit_at, exit_reason="stop_loss",
                         plan={"token_category": category})


async def test_comparison_groups_by_strategy_and_category_and_shows_the_net_without_the_best(app, client, auth_headers):
    rows = [pos("PAPER", "solana_migration", "0.139", symbol="DOTF", category="MIGRATED"),
            pos("PAPER", "solana_migration", "-0.02", symbol="A", category="MIGRATED"),
            pos("PAPER", "solana_migration", "-0.03", symbol="B", category="MIGRATED"),
            pos("PAPER", "solana_fresh", "-0.0178", symbol="MEIZHU", category="NEAR_MIGRATION"),
            pos("LIVE", "solana_fresh", "-0.001", symbol="C"),
            pos("PAPER", "evm_bsc", "5", symbol="EVM")]  # not Solana
    async with app.state.db_session_factory() as s:
        s.add_all(rows)
        await s.commit()
    r = await client.get("/api/strategies/comparison", headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["trades"] == 5 and body["min_sample"] == 20
    mig = next(g for g in body["strategy"] if g["key"] == "solana_migration" and g["mode"] == "PAPER")
    assert mig["trades"] == 3 and mig["net_sol"] == "0.089000" and mig["net_without_best_sol"] == "-0.050000"
    assert mig["best"]["symbol"] == "DOTF" and mig["worst"]["symbol"] == "B" and mig["sample"] == "too small to judge"
    assert {(g["mode"], g["key"]) for g in body["category"]} == {("PAPER", "MIGRATED"), ("PAPER", "NEAR_MIGRATION"),
                                                                  ("LIVE", "FRESH")}
    assert any(g["key"] == "solana_fresh | NEAR_MIGRATION" for g in body["strategy_x_category"])
    # a time filter keeps only positions closed since then
    since = (NOW - timedelta(hours=1)).isoformat()
    r = await client.get("/api/strategies/comparison", params={"since": since}, headers=auth_headers)
    assert r.json()["trades"] == 0
    assert (await client.get("/api/strategies/comparison")).status_code == 401
    # the catalog route still answers for real strategy names
    assert (await client.get("/api/strategies", headers=auth_headers)).status_code == 200
