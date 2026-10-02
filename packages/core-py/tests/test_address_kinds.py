"""M10b: address kinds. eth_getCode tells a wallet (no code), an EIP-7702
delegated wallet (0xef0100 || delegate, still a signing EOA) and a contract
apart; results are stored, contracts are never re-checked, wallets are after
a week, lookups are bounded and a failed lookup leaves the address unknown
(never assumed to be a wallet). Wallet profiles of contracts carry the
CONTRACT label, no score and the REJECTED discovery stage."""

import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from yonixalpha_core import wallet_profiles
from yonixalpha_core.chains.evm import address_kinds as ak

NOW = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
WALLET, SMART, ROUTER = "0x" + "a1" * 20, "0x" + "a2" * 20, "0x" + "b1" * 20
DELEGATE = "0x63c0c19a282a1b52b07dd5a65b58948a07dae32b"


def test_classify_code():
    assert ak.classify_code("0x") == (ak.WALLET, None, 0) and ak.classify_code(None) == (ak.WALLET, None, 0)
    assert ak.classify_code("0xef0100" + DELEGATE[2:]) == (ak.DELEGATED, DELEGATE, 23)
    assert ak.classify_code("0x6080604052")[0] == ak.CONTRACT
    assert ak.classify_code("0xef0100" + "00" * 30)[0] == ak.CONTRACT  # not a 23-byte designator


class Node:
    def __init__(self, fail=()):
        self.calls, self.fail = [], set(fail)

    async def get_code(self, a):
        self.calls.append(a)
        if a in self.fail:
            raise TimeoutError("node")
        return {ROUTER: "0x6080", SMART: "0xef0100" + DELEGATE[2:]}.get(a, "0x")


async def _db():
    engine = create_async_engine(os.environ.get(
        "DATABASE_URL", "postgresql+asyncpg://yonixalpha:yonixalpha_test_pw@localhost:5432/yonixalpha_test"))
    from yonixalpha_core.db import models  # noqa: F401
    from yonixalpha_core.db.base import Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    return engine


async def test_resolve_stores_rechecks_and_bounds_lookups():
    from yonixalpha_core.db.base import make_session_factory
    from yonixalpha_core.db.models import EvmAddressKind

    engine = await _db()
    try:
        async with make_session_factory(engine)() as s:
            node = Node(fail={WALLET})
            got = await ak.resolve(s, node, "bsc", [ROUTER.upper().replace("0X", "0x"), SMART, WALLET], NOW)
            assert got == {ROUTER: ak.CONTRACT, SMART: ak.DELEGATED}  # the failed lookup stays unknown
            await s.commit()
            assert (await s.get(EvmAddressKind, ("bsc", SMART))).delegate == DELEGATE
            node = Node()
            got = await ak.resolve(s, node, "bsc", [ROUTER, SMART, WALLET], NOW + timedelta(days=1))
            assert node.calls == [WALLET] and got[WALLET] == ak.WALLET  # known ones are not looked up again
            node = Node()
            await ak.resolve(s, node, "bsc", [ROUTER, SMART, WALLET], NOW + timedelta(days=9))
            assert sorted(node.calls) == sorted([SMART, WALLET])  # wallets re-checked after a week, contracts never
            node = Node()
            many = [f"0x{i:040x}" for i in range(10)]
            got = await ak.resolve(s, node, "robinhood", many, NOW, max_lookups=3)
            assert len(node.calls) == 3 and len(got) == 3
            assert len((await s.execute(select(EvmAddressKind).where(EvmAddressKind.chain == "robinhood"))).scalars().all()) == 3
    finally:
        await engine.dispose()


async def test_contract_profiles_are_labelled_and_never_candidates():
    from yonixalpha_core.db.base import make_session_factory
    from yonixalpha_core.db.models import EvmTrade, WalletProfile

    engine = await _db()
    try:
        async with make_session_factory(engine)() as s:
            for i, trader in enumerate([ROUTER] * 6 + [SMART] * 4):
                s.add(EvmTrade(event_id=f"e{i}", chain="bsc", launchpad="flap", token=f"0x{i % 3 + 1:040x}",
                               trader=trader, is_buy=i % 2 == 0, token_amount=Decimal(10 ** 21),
                               quote_amount=Decimal(10 ** 16), at=NOW - timedelta(hours=1, minutes=i)))
            await s.commit()
            n = await wallet_profiles.rebuild_evm(s, "bsc", NOW, rpc=Node())
            await s.commit()
            assert n == 2
            router = await s.get(WalletProfile, ("bsc", ROUTER))
            smart = await s.get(WalletProfile, ("bsc", SMART))
        assert router.labels[0] == "CONTRACT" and router.score is None and "router or bot" in router.score_detail["reason"]
        assert router.metrics["discovery"]["stage"] == "REJECTED" and router.metrics["account"]["kind"] == "CONTRACT"
        assert smart.metrics["account"]["kind"] == "DELEGATED_WALLET" and "DELEGATED_WALLET" in smart.labels
        assert smart.metrics["discovery"]["stage"] != "REJECTED" or smart.metrics["validation"]["status"] == "NOT_VALIDATED"
    finally:
        await engine.dispose()
