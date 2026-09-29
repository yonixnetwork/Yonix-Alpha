from datetime import datetime, timedelta, timezone
from decimal import Decimal


from yonixalpha_core import paper_engine
from yonixalpha_core.db.models import PaperPosition
from yonixalpha_core.exit_intel import solana_exit_decision
from yonixalpha_core.safety import store
from yonixalpha_core.safety.gate import assess
from yonixalpha_core.safety.liquidity import book_from_levels
from yonixalpha_core.safety.models import (
    AccountState,
    AssessmentInput,
    MarketInfo,
    Observation,
    StrategyLevels,
    StrategySignal,
)
from yonixalpha_core.safety.settings import default_settings_for
from yonixalpha_core.solana.flow import Trade

from app.gate_manage import manage_gate_positions

NOW = datetime.now(timezone.utc).replace(microsecond=0)


def book(mid, depth=Decimal(50)):
    bids = [(mid - Decimal("0.05") - Decimal("0.1") * i, depth) for i in range(100)]
    asks = [(mid + Decimal("0.05") + Decimal("0.1") * i, depth) for i in range(100)]
    return book_from_levels(bids, asks, Decimal(5))


async def open_futures_short(session_factory):
    settings = default_settings_for("binance_futures")
    m = book(Decimal(3000))
    inp = AssessmentInput(
        engine="binance_futures", strategy_name="meta_muse", asset_id="ETHUSDT", symbol="ETHUSDT", now=NOW,
        market=MarketInfo(Observation("t", NOW), m.mid, Decimal("0.003"), m.liquidity_quote, None), token=None, holders=None,
        flow=None, account=AccountState(Decimal(1000), Decimal(1000), 0, Decimal(0), Decimal(0), None, Decimal(0), False),
        liquidity_model=m, signal=StrategySignal("meta_muse", "1", True, 1.0), side="SHORT",
        strategy_levels=StrategyLevels(stop_loss=Decimal(3030), take_profits=[Decimal(2940)], source="meta_muse"),
    )
    a = assess(inp, settings)
    assert a.executable, a.reasons
    async with session_factory() as s:
        acct = await store.get_paper_account(s, "binance_futures")
        row, _ = await store.persist_assessment(s, a, None, "fut-1")
        pos = await paper_engine.open_position(s, acct, a, row.id, None, m, None, None, NOW,
                                               venue={"kind": "futures", "venue": "binance", "symbol": "ETHUSDT"})
        await s.commit()
        return pos.id


async def test_leftover_legacy_futures_position_is_not_managed(session_factory, redis_client):
    """Futures were removed: a position that engine left open stays as
    history - it is neither priced nor closed by the Solana manager."""
    pid = await open_futures_short(session_factory)
    counts = await manage_gate_positions(session_factory, redis_client, None, NOW + timedelta(minutes=5), None, None)
    assert counts == {"managed": 0, "closed": 0, "unpriced": 0}
    async with session_factory() as s:
        assert (await s.get(PaperPosition, pid)).status == "open"


def test_exit_intelligence_needs_two_pieces_of_evidence():
    def tr(sec, who, buy, sol):
        return Trade(NOW - timedelta(seconds=sec), who, buy, sol, 1, 1, 1)

    selling = [tr(60, f"s{i}", False, 10**9) for i in range(6)] + [tr(50, "b0", True, 10**8)]
    assert solana_exit_decision(selling, NOW, None, None, None).action == "REDUCE"
    # Heavy selling by one wallet only: sell pressure without seller dominance -> hold.
    one = [tr(60, "whale", False, 5 * 10**9), tr(50, "b0", True, 10**8), tr(40, "b1", True, 10**8)]
    assert solana_exit_decision(one, NOW, None, None, None).action == "HOLD"
    assert solana_exit_decision(one, NOW, None, Decimal(30), Decimal(15)).action == "EXIT"
    assert solana_exit_decision(one, NOW, "whale", None, None).action == "EXIT"
    quiet = [tr(60, "b0", True, 10**8)]
    # A 50% liquidity drop without sell pressure is one signal: hold.
    assert solana_exit_decision(quiet, NOW, None, Decimal(30), Decimal(15)).action == "HOLD"
    # A collapse past exit_emergency_liquidity_drop (60%) is an emergency on its own.
    assert solana_exit_decision(quiet, NOW, None, Decimal(30), Decimal(10)).action == "EXIT_NOW"
