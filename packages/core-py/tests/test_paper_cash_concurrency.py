"""Paper cash under concurrent writers (2026-10-24, server evidence: the
`solana` book differed from its own closed trades by -0.0636 SOL). The
decision engine (entries) and the position manager (exits) write the same
account row from different processes; a read-modify-write of the balance
loses one of two overlapping updates. adjust_cash applies a delta in the
database, so both survive."""

import os
from decimal import Decimal

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://yonixalpha:yonixalpha_test_pw@localhost:5432/yonixalpha_test")
os.environ.setdefault("JWT_SECRET", "test-secret-test-secret-test-secret-32")
os.environ.setdefault("ADMIN_PASSWORD_HASH", "x")

import pytest  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from yonixalpha_core.db.base import Base, make_session_factory  # noqa: E402
from yonixalpha_core.db.models import PaperAccount  # noqa: E402
from yonixalpha_core.safety import store  # noqa: E402


@pytest.fixture
async def sf():
    engine = create_async_engine(os.environ["DATABASE_URL"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield make_session_factory(engine)
    await engine.dispose()


@pytest.mark.asyncio
async def test_overlapping_entry_and_exit_both_change_the_cash(sf):
    async with sf() as s:
        acct = await store.get_paper_account(s, "solana")
        await s.commit()
        aid = acct.id
    entry, exit_ = sf(), sf()
    try:
        a1 = await entry.get(PaperAccount, aid)   # the decision engine reads the book ...
        a2 = await exit_.get(PaperAccount, aid)   # ... and so does the position manager
        await store.adjust_cash(exit_, a2, Decimal("0.3"))   # an exit credits 0.3
        await exit_.commit()
        await store.adjust_cash(entry, a1, Decimal("-0.1"))  # an entry, still in its own transaction, debits 0.1
        await entry.commit()
    finally:
        await entry.close()
        await exit_.close()
    async with sf() as s:
        assert (await s.get(PaperAccount, aid)).cash_balance == Decimal("10.2")  # 10 + 0.3 - 0.1: nothing lost


@pytest.mark.asyncio
async def test_the_object_carries_the_new_balance_and_a_pending_change_is_kept(sf):
    async with sf() as s:
        acct = await store.get_paper_account(s, "solana")
        acct.cash_balance = Decimal(5)  # an unflushed change in the same session is flushed first
        await store.adjust_cash(s, acct, Decimal("-1.5"))
        assert acct.cash_balance == Decimal("3.5")
        await s.commit()
    async with sf() as s:
        assert (await store.get_paper_account(s, "solana")).cash_balance == Decimal("3.5")


@pytest.mark.asyncio
async def test_the_old_read_modify_write_loses_an_update(sf):
    """The defect itself: both writers set an absolute balance computed from
    what they read; the later commit erases the earlier change."""
    async with sf() as s:
        aid = (await store.get_paper_account(s, "solana")).id
        await s.commit()
    entry, exit_ = sf(), sf()
    try:
        a1, a2 = await entry.get(PaperAccount, aid), await exit_.get(PaperAccount, aid)
        a2.cash_balance += Decimal("0.3")
        await exit_.commit()
        a1.cash_balance -= Decimal("0.1")
        await entry.commit()
    finally:
        await entry.close()
        await exit_.close()
    async with sf() as s:
        assert (await s.get(PaperAccount, aid)).cash_balance == Decimal("9.9")  # the exit's +0.3 is gone
