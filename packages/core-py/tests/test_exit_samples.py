"""Master §41, exits: SELL / HOLD checkpoints of open EVM paper positions,
their verdicts, forward labels and the comparison."""

import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://yonixalpha:yonixalpha_test_pw@localhost:5432/yonixalpha_test")

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from yonixalpha_core.db import models  # noqa: F401,E402
from yonixalpha_core.db.base import Base, make_session_factory  # noqa: E402
from yonixalpha_core.db.models import EvmExitSample, EvmToken, EvmTrade, PaperPosition  # noqa: E402
from yonixalpha_core.ml import exit_samples as xs  # noqa: E402

pytestmark = pytest.mark.asyncio
T = datetime(2026, 10, 4, 12, tzinfo=timezone.utc)
TOK = "0x" + "5" * 40
E18 = 10 ** 18


@pytest_asyncio.fixture
async def sf():
    engine = create_async_engine(os.environ["DATABASE_URL"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield make_session_factory(engine)
    await engine.dispose()


def position(**kw):
    base = dict(id=uuid.uuid4(), engine="evm_bsc", symbol="X", asset_id=TOK, provider="paper", side="LONG",
                entry_price=Decimal("1.0"), quantity=Decimal(100), initial_quantity=Decimal(100),
                remaining_quantity=Decimal(50), stop_loss=Decimal("0.8"), take_profit=[], status="open",
                entry_at=T, highest_price=Decimal("1.5"), tp_hits=["tp1"], plan={"venue": {"launchpad": "fourmeme"}})
    base.update(kw)
    return PaperPosition(**base)


async def test_verdicts_follow_the_exit_reasons():
    assert xs.verdicts([]) == {"deterministic": "HOLD", "risk": "HOLD", "final": "HOLD", "ml": "NOT_AVAILABLE"}
    assert xs.verdicts(["take_profit_1"])["deterministic"] == "SELL" and xs.verdicts(["take_profit_1"])["risk"] == "HOLD"
    assert xs.verdicts(["stop_loss"]) == {"deterministic": "HOLD", "risk": "SELL", "final": "SELL", "ml": "NOT_AVAILABLE"}
    assert xs.verdicts(["trailing_stop"])["deterministic"] == "SELL" and xs.verdicts(["copy_sell"])["final"] == "SELL"
    assert xs.verdicts(["manual_exit"]) == {"deterministic": "HOLD", "risk": "HOLD", "final": "SELL", "ml": "NOT_AVAILABLE"}


async def test_features_are_what_was_known_at_the_checkpoint():
    p = position()
    x = xs.features(p, Decimal("1.2"), {"buys": 9, "net_buy_ratio": "0.7"}, {"liquidity_quote": "3"}, "bsc", "fourmeme",
                    T + timedelta(minutes=10))
    assert x["held_s"] == 600 and x["unrealized_pct"] == pytest.approx(20.0) and x["peak_pct"] == pytest.approx(50.0)
    assert x["drawdown_from_peak_pct"] == pytest.approx(-20.0) and x["stop_distance_pct"] == pytest.approx(50.0)
    assert x["remaining_fraction"] == 0.5 and x["tp_hits"] == 1.0 and x["liquidity"] == 3.0
    assert x["volatility"] is None and x["volatility__missing"] == 1.0 and x["chain_bsc"] == 1.0
    assert set(xs.FEATURE_NAMES) <= set(x)


async def test_labels_come_from_the_next_15_minutes_only():
    path = [(T + timedelta(minutes=m), px) for m, px in ((1, 1.0), (5, 0.85), (14, 0.95), (20, 0.1))]
    lab = xs.labels(1.0, path, T)
    assert lab == {"forward_return_pct": pytest.approx(-5.0), "fell_10": True, "rose_10": False, "trades_after": 3}
    assert xs.labels(None, path, T) == {"unknown": "no trade price at the checkpoint"}
    assert xs.labels(1.0, [], T)["forward_return_pct"] == 0.0


async def test_record_every_five_minutes_and_on_every_exit_then_label(sf):
    p = position()
    async with sf() as s:
        s.add(p)
        row = EvmToken(chain="bsc", token=TOK, launchpad="fourmeme", created_at=T, category="FRESH", stage="CURVE",
                       venue={}, stats={"buys": 4}, state={"liquidity_quote": "2"})
        s.add(row)
        q = int(Decimal("0.1") * E18)
        for i, (m, px) in enumerate(((0, 1.0), (3, 1.0), (8, 0.8), (12, 1.3))):
            s.add(EvmTrade(event_id=f"bsc:0x{i:064x}:0", chain="bsc", launchpad="fourmeme", token=TOK, trader="0x" + "a" * 40,
                           is_buy=True, token_amount=Decimal(int(q / px)), quote_amount=Decimal(q), at=T + timedelta(minutes=m)))
        await s.commit()
        assert await xs.record(s, p, Decimal("1.0"), [], row, "bsc", T + timedelta(minutes=1)) is True
        await s.commit()
        assert await xs.record(s, p, Decimal("1.0"), [], row, "bsc", T + timedelta(minutes=3)) is False  # < 5 min
        assert await xs.record(s, p, Decimal("1.0"), ["take_profit_1"], row, "bsc", T + timedelta(minutes=4)) is True
        await s.commit()
        assert await xs.label_pending(s, T + timedelta(minutes=10)) == 0  # forward window not over yet
        assert await xs.label_pending(s, T + timedelta(minutes=30)) == 2
        await s.commit()
        rows = (await s.execute(select(EvmExitSample).order_by(EvmExitSample.at))).scalars().all()
    assert [r.verdicts["final"] for r in rows] == ["HOLD", "SELL"] and rows[1].exit_reasons == ["take_profit_1"]
    assert rows[0].labels["fell_10"] is True and rows[0].labels["rose_10"] is True  # 0.8 at 8 min, 1.3 at 12 min
    assert rows[0].features["buys"] == 4.0 and rows[0].launchpad == "fourmeme"
    c = xs.compare([{"verdicts": r.verdicts, "labels": r.labels} for r in rows])
    assert c["final"]["HOLD"]["labelled"] == 1 and c["final"]["SELL"]["mean_forward_return_pct"] == pytest.approx(30.0)
    assert c["ml"] == {"NOT_AVAILABLE": {**c["ml"]["NOT_AVAILABLE"], "n": 2}}
