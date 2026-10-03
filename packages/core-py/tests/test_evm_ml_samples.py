"""EVM opportunity samples (M12a) and wallet behaviour labels (M12b): no
look-ahead, labels from the hour after, verdicts, builders."""

import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://yonixalpha:yonixalpha_test_pw@localhost:5432/yonixalpha_test")

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from yonixalpha_core.db import models  # noqa: F401,E402
from yonixalpha_core.db.base import Base, make_session_factory  # noqa: E402
from yonixalpha_core.db.models import (EvmMlSample, EvmObservation, EvmToken, EvmTrade, PaperPosition,  # noqa: E402
                                       WalletProfile, WalletTradeLabel)
from yonixalpha_core.ml import evm_samples as es  # noqa: E402
from yonixalpha_core.ml import wallet_labels as wl  # noqa: E402

pytestmark = pytest.mark.asyncio
T = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
TOK = "0x" + "1" * 40
E18 = 10 ** 18


def snap(minutes, **kw):
    at = T + timedelta(minutes=minutes)
    base = {"minutes": minutes, "at": at.isoformat(), "taken_at": (at + timedelta(seconds=20)).isoformat(),
            "state_read_at": (at + timedelta(seconds=20)).isoformat(), "price": "0.000001", "trades": 10, "buyers": 6,
            "sellers": 2, "buy_volume": "1.5", "sell_volume": "0.5", "liquidity": "3", "curve_progress": 12}
    base.update(kw)
    return base


async def test_features_use_only_snapshots_at_or_before_the_decision_and_drop_late_live_state():
    s5 = snap(5, price="0.000002")
    s, t0 = es.decision_snapshot({"T0": snap(0), "T+5": s5, "T+10": snap(10, trades=999)})
    assert s is s5 and t0["minutes"] == 0
    x = es.features(s, t0, "bsc", "FRESH", "fourmeme")
    assert x["trades"] == 10.0 and x["price_change_t0"] == pytest.approx(1.0) and x["buy_sell_volume_ratio"] == 3.0
    assert x["liquidity"] == 3.0 and x["chain_bsc"] == 1.0 and x["cat_FRESH"] == 1.0 and x["lp_fourmeme"] == 1.0
    assert x["effective_buyers"] is None and x["effective_buyers__missing"] == 1.0  # unknown is not 0
    late = snap(5, state_read_at=(T + timedelta(minutes=9)).isoformat())  # live state read 4 min late: could carry the future
    x = es.features(late, None, "robinhood", "MIGRATED", "pons_v2")
    assert x["liquidity"] is None and x["liquidity__missing"] == 1.0 and x["curve_progress"] is None
    assert x["price_change_t0"] is None and x["chain_robinhood"] == 1.0 and set(x) == set(es.FEATURE_NAMES)


async def test_labels_come_only_from_the_hour_after_the_decision():
    d = T + timedelta(minutes=5)
    path = [(d - timedelta(minutes=1), 9.0),  # before the decision: ignored
            (d + timedelta(minutes=3), 0.4),  # -60 % within 10 min: fast dump
            (d + timedelta(minutes=30), 2.5), (d + timedelta(minutes=59), 1.2),
            (d + timedelta(minutes=70), 50.0)]  # after the hour: ignored
    lab = es.labels(1.0, path, d, d + timedelta(minutes=40))
    assert lab["fast_dump"] and lab["upside_50"] and lab["upside_100"] and lab["migrate_60m"]
    assert lab["return_60m_pct"] == pytest.approx(20.0) and lab["max_return_pct"] == pytest.approx(150.0)
    assert lab["max_drawdown_pct"] == pytest.approx(-60.0) and lab["trades_after"] == 3
    assert es.labels(None, path, d, None) == {"unknown": "no trade price at the decision point"}
    quiet = es.labels(1.0, [], d, None)
    assert quiet["return_60m_pct"] == 0.0 and not quiet["upside_50"] and "no trade" in quiet["note"]


async def test_verdicts_and_ml_verdict():
    h = [{"state": "OBSERVING"}, {"state": "QUALIFIED"}, {"state": "WAITING_FOR_ENTRY"}]
    assert es.verdicts("EXPIRED", h, None) == {"deterministic": "BUY", "risk": "ALLOW", "final": "WAIT", "ml": "NOT_AVAILABLE"}
    assert es.verdicts("ENTERED", [{"state": "ENTERED"}], "BUY")["final"] == "BUY"
    v = es.verdicts("REJECTED", [{"state": "SAFETY_FAILURE"}], None)
    assert (v["deterministic"], v["risk"], v["final"]) == ("WAIT", "REJECT", "REJECT")
    assert es.ml_verdict({}) is None
    assert es.ml_verdict({"P_UPSIDE_50": {"value": 0.7}, "P_FAST_DUMP": {"value": 0.2}}) == "BUY"
    assert es.ml_verdict({"P_UPSIDE_50": {"value": 0.7}, "P_FAST_DUMP": {"value": 0.6}}) == "REJECT"
    assert es.ml_verdict({"P_UPSIDE_50": {"value": 0.3}}) == "WAIT"


async def test_compare_counts_missed_winners_and_bad_entries():
    win = {"upside_50": True, "upside_100": True, "fast_dump": False, "return_60m_pct": 80.0}
    dump = {"upside_50": False, "upside_100": False, "fast_dump": True, "return_60m_pct": -60.0}
    rows = [{"verdicts": {"deterministic": "BUY", "risk": "ALLOW", "final": "BUY", "ml": "REJECT"}, "labels": dump,
             "executable_return_pct": -55.0},
            {"verdicts": {"deterministic": "WAIT", "risk": "ALLOW", "final": "WAIT", "ml": "BUY"}, "labels": win},
            {"verdicts": {"deterministic": "WAIT", "risk": "REJECT", "final": "REJECT", "ml": "NOT_AVAILABLE"},
             "labels": {"unknown": "x"}}]
    c = es.compare(rows)
    assert c["final_missed_winners"] == 1 and c["final_bad_entries"] == 1
    assert c["final"]["BUY"]["fast_dump_rate"] == 1.0 and c["final"]["BUY"]["executable"] == {"n": 1, "mean_pct": -55.0}
    assert c["ml"]["BUY"]["upside_100_rate"] == 1.0 and c["final_vs_ml"] == {"final BUY / ml REJECT": 1, "final WAIT / ml BUY": 1}
    assert c["final"]["REJECT"] == {"n": 1, "labelled": 0}


async def test_wallet_episode_labels():
    def tr(m, p, buy=True, h="w"):
        return wl.Trade(T + timedelta(minutes=m), h, buy, p, 0.1)
    entry = tr(10, 3.0)
    token = [tr(0, 1.0, h="x"), entry, tr(20, 7.0, h="y"), tr(40, 2.0, h="y"), tr(50, 1.5, buy=False), tr(80, 6.0, h="z")]
    res = wl.episode_labels(entry, [entry, tr(50, 1.5, buy=False)], token, 1.0, T + timedelta(hours=3))
    assert wl.SUCCESSFUL in res["labels"] and wl.LATE_ENTRY in res["labels"]  # bought at 3x the first price
    assert wl.LATE_EXIT in res["labels"]  # up 2.3x, sold at 0.21 of the peak
    assert wl.PREMATURE in res["labels"]  # 6.0 >= 2 x 1.5 within the hour after the sell
    e2 = tr(10, 1.0)
    res = wl.episode_labels(e2, [e2], [e2, tr(12, 0.4, h="y"), tr(30, 1.6, h="y")], None, T + timedelta(hours=3))
    assert res["labels"] == [wl.FAILED] and res["successful_entry"] is False  # -50 % came first
    assert wl.missed_winners("w", {"A"}, [(T, "fourmeme")], [("A", "fourmeme", T), ("B", "fourmeme", T + timedelta(minutes=30)),
                                                             ("C", "flap", T)]) == [("B", "fourmeme", T + timedelta(minutes=30))]


async def test_wallet_features_use_only_earlier_known_outcomes():
    entry = wl.Trade(T + timedelta(minutes=10), "w", True, 2.0, 0.5)
    token = [wl.Trade(T, "a", True, 1.0, 1.0), wl.Trade(T + timedelta(minutes=5), "b", False, 1.5, 0.2), entry]
    prior = [{"available_at": (T + timedelta(minutes=5)).isoformat(), "successful_entry": True},
             {"available_at": (T + timedelta(minutes=30)).isoformat(), "successful_entry": False}]  # not known yet
    x = wl.entry_features(entry, token, T, 1.0, prior, "bsc", "flap")
    assert x["prior_episodes"] == 1.0 and x["prior_success_rate"] == 1.0 and x["entry_multiple"] == 2.0
    assert x["buyers_before"] == 1.0 and x["sell_volume_before"] == 0.2 and x["token_age_s"] == 600
    assert set(x) == set(wl.FEATURE_NAMES) and x["lp_flap"] == 1.0


@pytest_asyncio.fixture
async def sf():
    engine = create_async_engine(os.environ["DATABASE_URL"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield make_session_factory(engine)
    await engine.dispose()


def trade(i, at, trader, buy, price, quote=Decimal("0.1")):
    q = int(quote * E18)
    return EvmTrade(event_id=f"bsc:0x{i:064x}:0", chain="bsc", launchpad="fourmeme", token=TOK, trader=trader,
                    is_buy=buy, token_amount=Decimal(int(q / price)), quote_amount=Decimal(q), at=at)


async def test_builders_materialise_samples_and_episodes_once(sf):
    now = T + timedelta(days=2)
    w = "0x" + "a" * 40
    async with sf() as s:
        s.add(EvmToken(chain="bsc", token=TOK, launchpad="fourmeme", created_at=T, category="FRESH", stage="CURVE",
                       venue={}, stats={}))
        s.add(EvmObservation(chain="bsc", token=TOK, category="FRESH", state="ENTERED", state_at=T + timedelta(minutes=6),
                             started_at=T, deadline=T + timedelta(hours=1), observation_reason="launch",
                             snapshots={"T0": snap(0), "T+5": snap(5)},
                             history=[{"state": "QUALIFIED"}, {"state": "ENTERED"}]))
        s.add(PaperPosition(engine="evm_bsc", symbol="X", asset_id=TOK, provider="paper", side="LONG", entry_price=Decimal(1),
                            quantity=Decimal(1), take_profit=[], status="closed", realized_pnl_pct=Decimal("0.25"),
                            entry_at=T + timedelta(minutes=6), plan={}))
        s.add(WalletProfile(chain="bsc", wallet=w, metrics={}, labels=[], source="t", trades=3, tokens=1))
        s.add_all([trade(1, T + timedelta(seconds=30), "0x" + "b" * 40, True, 1e-6),
                   trade(2, T + timedelta(minutes=4), w, True, 2e-6),
                   trade(3, T + timedelta(minutes=20), "0x" + "c" * 40, True, 5e-6),
                   trade(4, T + timedelta(minutes=50), w, False, 4e-6)])
        await s.commit()
    async with sf() as s:
        out = await es.build(s, now)
        await s.commit()
        assert out["built"] == 1 and (await es.build(s, now))["built"] == 0
        row = (await s.execute(select(EvmMlSample))).scalar_one()
        assert row.traded and row.executable_return_pct == pytest.approx(25.0) and row.verdicts["final"] == "BUY"
        assert row.labels["upside_100"] is True and row.labels["max_return_pct"] == pytest.approx(150.0, rel=1e-3)
        assert row.decided_at == T + timedelta(minutes=5) and row.features["trades"] == 10.0
    async with sf() as s:
        out = await wl.build(s, now)
        await s.commit()
        assert out == {"tokens": 1, "episodes": 1}
        ep = (await s.execute(select(WalletTradeLabel).where(WalletTradeLabel.kind == "EPISODE"))).scalar_one()
        assert ep.wallet == w and wl.SUCCESSFUL in ep.labels and ep.outcome["entry_multiple"] == pytest.approx(2.0, rel=1e-3)
        assert ep.features["buyers_before"] == 1.0 and ep.features["prior_episodes"] == 0.0
        assert (await wl.build(s, now)) == {"tokens": 0, "episodes": 0}  # each token once


async def test_missed_winners_for_a_copy_target(sf):
    from yonixalpha_core.db.models import CopyTarget

    w, tok2, tok3 = "0x" + "a" * 40, "0x" + "2" * 40, "0x" + "3" * 40
    now = T + timedelta(days=1)

    def t(i, token, at, trader, price):
        q = int(Decimal("0.1") * E18)
        return EvmTrade(event_id=f"bsc:0x{i:064x}:0", chain="bsc", launchpad="fourmeme", token=token, trader=trader,
                        is_buy=True, token_amount=Decimal(int(q / price)), quote_amount=Decimal(q), at=at)
    async with sf() as s:
        s.add(CopyTarget(chain="bsc", wallet=w, mode="NOTIFY"))
        for tk, created in ((TOK, T), (tok2, T + timedelta(minutes=20)), (tok3, T + timedelta(minutes=25))):
            s.add(EvmToken(chain="bsc", token=tk, launchpad="fourmeme", created_at=created, category="FRESH",
                           stage="CURVE", venue={}, stats={}))
        s.add_all([t(1, TOK, T + timedelta(minutes=2), w, 1e-6),  # the wallet is active on fourmeme
                   t(2, tok2, T + timedelta(minutes=21), "0x" + "b" * 40, 1e-6),
                   t(3, tok2, T + timedelta(minutes=50), "0x" + "c" * 40, 3e-6),  # tok2 tripled: a winner it missed
                   t(4, tok3, T + timedelta(minutes=26), "0x" + "b" * 40, 1e-6),
                   t(5, tok3, T + timedelta(minutes=40), "0x" + "c" * 40, 1.2e-6)])  # tok3 did not double
        await s.commit()
    async with sf() as s:
        assert await wl.build_missed(s, now) == 1
        await s.commit()
        rows = (await s.execute(select(WalletTradeLabel))).scalars().all()
        assert [(r.token, r.kind, r.labels) for r in rows] == [(tok2, "MISSED", [wl.MISSED])]
        assert await wl.build_missed(s, now) == 0  # recorded once
