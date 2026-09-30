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
