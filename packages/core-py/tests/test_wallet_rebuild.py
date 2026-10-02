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
