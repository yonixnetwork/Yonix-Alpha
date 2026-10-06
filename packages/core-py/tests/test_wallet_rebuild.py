"""EVM wallet profile rebuild: bounded (most active wallets, loaded in
batches), minimum trade count respected, and the FIFO P/L profile stored."""
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://yonixalpha:yonixalpha_test_pw@localhost:5432/yonixalpha_test")

import pytest_asyncio  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from yonixalpha_core import wallet_profiles  # noqa: E402
from yonixalpha_core.db import models  # noqa: F401,E402
from yonixalpha_core.db.base import Base, make_session_factory  # noqa: E402
from yonixalpha_core.db.models import EvmTrade, WalletProfile  # noqa: E402

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
E18 = 10 ** 18


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(os.environ["DATABASE_URL"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with make_session_factory(engine)() as session:
        yield session
    await engine.dispose()


def trade(n, wallet, token, is_buy, quote, minutes):
    return EvmTrade(event_id=f"e{n}", chain="bsc", launchpad="flap", token=token, trader=wallet, is_buy=is_buy,
                    token_amount=Decimal(1000), quote_amount=Decimal(int(quote * E18)), at=NOW - timedelta(minutes=minutes))


async def test_rebuild_profiles_the_most_active_wallets_with_their_pnl(db):
    busy, calm, tiny = "0x" + "b" * 40, "0x" + "c" * 40, "0x" + "d" * 40
    rows, n = [], 0
    for i in range(6):  # busy: 6 round trips, 4 winners
        rows += [trade(n, busy, f"0xt{i}", True, 1, 100 - i), trade(n + 1, busy, f"0xt{i}", False, 2 if i < 4 else 0.5, 90 - i)]
        n += 2
    rows += [trade(n, calm, "0xq", True, 1, 50), trade(n + 1, calm, "0xq", False, 1.5, 40), trade(n + 2, calm, "0xr", True, 1, 30)]
    rows += [trade(n + 3, tiny, "0xz", True, 1, 20)]  # below min_trades
    db.add_all(rows)
    await db.commit()

    assert await wallet_profiles.rebuild_evm(db, "bsc", NOW, max_wallets=1) == 1
    await db.commit()
    assert [p.wallet for p in (await db.execute(select(WalletProfile))).scalars()] == [busy]

    assert await wallet_profiles.rebuild_evm(db, "bsc", NOW) == 2
    await db.commit()
    got = {p.wallet: p for p in (await db.execute(select(WalletProfile))).scalars()}
    assert set(got) == {busy, calm}
    pnl = got[busy].metrics["pnl"]
    assert pnl["all"]["status"] == "OK" and pnl["all"]["winning_trades"] == 4 and pnl["all"]["losing_trades"] == 2
    assert pnl["all"]["usually_earns"]["median"] == "1.000000000" and pnl["all"]["usually_loses"]["avg"] == "-0.500000000"
    assert pnl["windows"]["30D"]["status"] == "INSUFFICIENT_DATA"
    calm_pnl = got[calm].metrics["pnl"]
    assert calm_pnl["all"]["status"] == "INSUFFICIENT_DATA" and calm_pnl["open_tokens"] == 1


async def test_validated_wallet_is_paper_followed_and_never_copied(db):
    """§25-26: a wallet passing the (operator-lowered) checks is VALIDATED and
    its recent buys are replayed on paper from the other trades of the same
    tokens; it is never added as a copy target. The regime test runs over
    the hours the rebuild summarised."""
    from yonixalpha_core import wallet_validation
    from yonixalpha_core.db.models import CopyTarget, MarketRegimeHour, PlatformSetting

    pro, other = "0x" + "e" * 40, "0x" + "f" * 40
    db.add(PlatformSetting(key=wallet_validation.SETTINGS_KEY, value={
        "min_trades": 4, "min_closed": 3, "min_active_days": 2, "min_active_weeks": 1, "min_unique_tokens": 3,
        "min_history_days": 1, "min_profitable_periods": 1, "min_profit_factor": 1.0}))
    rows, n = [], 0
    for i, day_min in enumerate((3 * 1440, 2 * 1440, 1440, 300)):  # 4 tokens over 4 days, all profitable
        tok = f"0xv{i}"
        rows += [trade(n, pro, tok, True, 1, day_min), trade(n + 1, pro, tok, False, 1.3, day_min - 20)]
        rows += [trade(n + 2, other, tok, True, 1.1, day_min - 1), trade(n + 3, other, tok, True, 1.2, day_min - 10)]
        n += 4
    db.add_all(rows)
    await db.commit()
    assert await wallet_profiles.rebuild_evm(db, "bsc", NOW) == 2
    await db.commit()
    p = await db.get(WalletProfile, ("bsc", pro))
    v = p.metrics["validation"]
    assert v["status"] == "VALIDATED", v["reason"]
    pf = p.metrics["paper_follow"]
    assert pf["evaluated"] == 4 and pf["won"] == 4 and pf["buys_replayed"] == 4
    assert p.metrics["discovery"]["stage"] == "PAPER_FOLLOWED"
    assert p.metrics["regimes"]["status"] in ("CONSISTENT", "INSUFFICIENT_DATA", "REGIME_DEPENDENT")
    assert (await db.execute(select(MarketRegimeHour))).scalars().first() is not None
    assert (await db.execute(select(CopyTarget))).scalars().first() is None  # never copied automatically


async def test_trades_quoted_in_other_tokens_are_left_out_and_unrefreshed_profiles_are_stale(db):
    """Four.meme curves quoted in tokenized stocks (BNCB ~$6) report `cost` in
    the stock's units: counted as BNB they made a wallet look like it traded
    hundreds of BNB. They are excluded by the token's recorded quote or the
    trade's native_quote flag; WBNB counts as native. A profile no rebuild
    refreshes is marked stale, and a rebuild clears the mark."""
    from yonixalpha_core.chains.registry import BSC_WBNB
    from yonixalpha_core.db.models import EvmToken

    bncb = "0x4902c5ebc598265ed2212b559b042de8a5eeec3f"
    w = "0x" + "e" * 40
    db.add_all([EvmToken(chain="bsc", token="0xstock", launchpad="fourmeme", created_at=NOW, quote_token=bncb),
                EvmToken(chain="bsc", token="0xwbnb", launchpad="fourmeme", created_at=NOW, quote_token=BSC_WBNB)])
    rows = []
    for i in range(3):  # real BNB round trips: +1 each
        rows += [trade(10 * i, w, f"0xn{i}", True, 1, 100 - i), trade(10 * i + 1, w, f"0xn{i}", False, 2, 90 - i)]
    rows += [trade(50, w, "0xstock", True, 500, 80), trade(51, w, "0xstock", False, 900, 70)]  # BNCB units
    flagged = [trade(60, w, "0xflag", True, 300, 60), trade(61, w, "0xflag", False, 10, 50)]
    for t in flagged:
        t.extra = {"native_quote": False}
    rows += flagged + [trade(70, w, "0xwbnb", True, 1, 40), trade(71, w, "0xwbnb", False, 1.5, 30)]
    db.add_all(rows)
    await db.commit()

    assert await wallet_profiles.rebuild_evm(db, "bsc", NOW) == 1
    await db.commit()
    p = (await db.execute(select(WalletProfile))).scalar_one()
    assert p.trades == 8 and p.tokens == 4  # 3 BNB tokens + the WBNB-quoted one; stock / flagged left out
    assert Decimal(p.metrics["pnl"]["all"]["realized_pnl"]) == Decimal("3.5")
    assert "stale" not in p.metrics

    later = NOW + timedelta(days=20)  # nothing in the window any more: not rebuilt, marked stale
    assert await wallet_profiles.rebuild_evm(db, "bsc", later) == 0
    await db.commit()
    p = (await db.execute(select(WalletProfile))).scalar_one()
    assert p.metrics["stale"]["since"] == later.isoformat() and "tokenized stocks" in p.metrics["stale"]["reason"]
    assert Decimal(p.metrics["pnl"]["all"]["realized_pnl"]) == Decimal("3.5")  # the old figures are kept, labelled

    assert await wallet_profiles.rebuild_evm(db, "bsc", NOW) == 1  # rebuilt again: the mark is gone
    await db.commit()
    db.expunge_all()
    p = (await db.execute(select(WalletProfile))).scalar_one()
    assert "stale" not in p.metrics


def test_row_batches_stay_within_the_budget_and_never_split_a_wallet():
    counts = [("a", 3), ("b", 2), ("c", 9), ("d", 1), ("e", 1)]
    assert wallet_profiles._row_batches(counts, 5) == [["a", "b"], ["c"], ["d", "e"]]
    assert wallet_profiles._row_batches([], 5) == []


async def test_solana_rebuild_in_row_batches_matches_the_whole_table_at_once(db):
    """Server 2026-10-06: the 30-day launch_buyers table (1,066,313 rows) was
    loaded at once and copy-engine was killed for memory every few minutes.
    Loaded in row batches, the profiles are the same."""
    from yonixalpha_core.db.models import LaunchBuyer

    def buyer(wallet, n, days_ago=0.0, led=None):
        at = NOW - timedelta(days=days_ago, minutes=n)
        return LaunchBuyer(mint=f"M{wallet}{n}", wallet=wallet, rank=1, launch_created_at=at - timedelta(seconds=2),
                           first_buy_at=at, sol_in=Decimal("0.5"), tokens_in=Decimal(1000), recorded_at=at,
                           outcome="WIN" if n % 2 else "LOSS", early_window_closed=True, ledger=led)

    sold = {"covered": True, "sol_out": "0.8", "tokens_out": "1000", "last_sell_at": (NOW - timedelta(minutes=1)).isoformat()}
    rows = [buyer("busy", i, led=sold if i < 3 else None) for i in range(5)]
    rows += [buyer("calm", i) for i in range(3)] + [buyer("calm", 9, days_ago=40)]  # one older than the window
    rows += [buyer("tiny", i) for i in range(2)]  # below min_rows
    db.add_all(rows)
    await db.commit()

    want = {w: wallet_profiles.solana_metrics([r for r in rows if r.wallet == w and r.first_buy_at >= NOW - timedelta(days=30)],
                                              wallet_profiles.ScoreConfig(), NOW, 30) for w in ("busy", "calm")}
    assert await wallet_profiles.rebuild_solana(db, NOW, row_batch=4) == 2  # "busy" alone is over the budget
    await db.commit()
    got = {p.wallet: p for p in (await db.execute(select(WalletProfile).where(WalletProfile.chain == "solana"))).scalars()}
    assert set(got) == {"busy", "calm"}
    for w, m in want.items():
        assert got[w].trades == m["trades"] and got[w].metrics["ledger_coverage"] == m["ledger_coverage"]
        assert got[w].metrics["pnl"]["all"]["status"] == m["pnl"]["all"]["status"]
    assert got["busy"].metrics["ledger_coverage"] == {"launches": 5, "with_ledger": 3, "without_ledger": 2}
    assert got["calm"].trades == 3  # the 40-day-old buy is outside the window


async def test_a_bot_wallet_is_profiled_on_its_most_recent_buys_and_says_so(db):
    """Server 2026-10-06: batched by whole wallets, one bot wallet buying every
    launch still took copy-engine out of memory every ~25 minutes."""
    from yonixalpha_core.db.models import LaunchBuyer

    rows = [LaunchBuyer(mint=f"B{i}", wallet="bot", rank=1, launch_created_at=NOW - timedelta(minutes=i, seconds=2),
                        first_buy_at=NOW - timedelta(minutes=i), sol_in=Decimal("0.1"), tokens_in=Decimal(10),
                        recorded_at=NOW, early_window_closed=True) for i in range(7)]
    rows += [LaunchBuyer(mint=f"H{i}", wallet="human", rank=2, first_buy_at=NOW - timedelta(hours=i), sol_in=Decimal(1),
                         tokens_in=Decimal(10), recorded_at=NOW, early_window_closed=True) for i in range(3)]
    db.add_all(rows)
    await db.commit()
    assert await wallet_profiles.rebuild_solana(db, NOW, max_rows_per_wallet=4) == 2
    await db.commit()
    got = {p.wallet: p for p in (await db.execute(select(WalletProfile).where(WalletProfile.chain == "solana"))).scalars()}
    bot = got["bot"]
    assert bot.trades == 4 and bot.metrics["sample"]["launches_in_window"] == 7
    assert bot.metrics["sample"]["launches_used"] == 4
    assert bot.first_seen == NOW - timedelta(minutes=3)  # the 4 most recent buys, not the oldest
    assert any("most recent 4 of 7" in x for x in bot.metrics["pnl"]["notes"])
    assert "sample" not in got["human"].metrics and got["human"].trades == 3


async def test_an_evm_bot_wallet_is_profiled_on_its_most_recent_trades_and_says_so(db):
    """Server 2026-10-06: the 100 most active BSC wallets' 14 days of trades,
    loaded at once as ORM objects, took copy-engine to 1.6 GB (1,051 MB
    measured on 500,000 bot trades; 109 MB in row batches with the cap)."""
    bot, calm = "0x" + "e" * 40, "0x" + "c" * 40
    rows, n = [], 0
    for i in range(4):  # bot: 4 round trips, 8 trades, the oldest first
        rows += [trade(n, bot, f"0xb{i}", True, 1, 100 - 10 * i), trade(n + 1, bot, f"0xb{i}", False, 2, 95 - 10 * i)]
        n += 2
    rows += [trade(n, calm, "0xq", True, 1, 50), trade(n + 1, calm, "0xq", False, 1.5, 40), trade(n + 2, calm, "0xr", True, 1, 30)]
    db.add_all(rows)
    await db.commit()
    assert await wallet_profiles.rebuild_evm(db, "bsc", NOW, max_trades_per_wallet=4, row_batch=3) == 2
    await db.commit()
    got = {p.wallet: p for p in (await db.execute(select(WalletProfile))).scalars()}
    b = got[bot]
    assert b.metrics["sample"] == {"trades_in_window": 8, "trades_used": 4,
                                   "basis": "the most recent 4 trades (bounded per wallet)"}
    assert b.trades == 4 and b.first_seen == NOW - timedelta(minutes=80)  # the 4 most recent trades
    assert b.metrics["pnl"]["all"]["winning_trades"] == 2  # the two most recent round trips
    assert any("most recent 4 of 8 trades" in x for x in b.metrics["pnl"]["notes"])
    assert "sample" not in got[calm].metrics and got[calm].trades == 3
