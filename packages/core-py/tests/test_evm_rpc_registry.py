"""BSC / Robinhood RPC endpoints from the dashboard: order (dashboard, .env,
public), overrides for .env / public endpoints, a chain never left empty,
and the running EvmRpc picking the list up without a restart."""
import os
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://yonixalpha:yonixalpha_test_pw@localhost:5432/yonixalpha_test")

import pytest_asyncio  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from yonixalpha_core import secretbox  # noqa: E402
from yonixalpha_core.chains.base import Chain  # noqa: E402
from yonixalpha_core.chains.evm import rpc_registry as reg  # noqa: E402
from yonixalpha_core.chains.evm.rpc import make_rpc  # noqa: E402
from yonixalpha_core.chains.registry import CHAINS  # noqa: E402
from yonixalpha_core.db import models  # noqa: F401,E402
from yonixalpha_core.db.base import Base, make_session_factory  # noqa: E402
from yonixalpha_core.db.models import PlatformSetting, RpcProvider  # noqa: E402

SETTINGS = SimpleNamespace(JWT_SECRET="x" * 40, CONFIG_ENCRYPTION_KEY=None, BSC_RPC_URLS="https://env-bsc.example/K1",
                           ROBINHOOD_RPC_URLS=None)


@pytest_asyncio.fixture
async def sf():
    engine = create_async_engine(os.environ["DATABASE_URL"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield make_session_factory(engine)
    await engine.dispose()


async def test_dashboard_then_env_then_public_and_overrides(sf):
    async with sf() as s:
        s.add(RpcProvider(name="alchemy bsc", chain="bsc", provider_type="alchemy", priority=150,
                          rpc_url_enc=secretbox.encrypt(SETTINGS, "https://bnb.alchemy.example/v2/K2"), rpc_display="x"))
        s.add(RpcProvider(name="solana one", chain="solana", provider_type="helius", priority=10,
                          rpc_url_enc=secretbox.encrypt(SETTINGS, "https://sol.example/K3"), rpc_display="x"))
        await s.commit()
        rows = await reg.endpoints(s, SETTINGS, "bsc")
        assert [r["source"] for r in rows][:2] == ["dashboard", "env"] and rows[-1]["source"] == "public"
        assert "https://sol.example/K3" not in [r["url"] for r in rows]  # Solana rows stay Solana
        urls = await reg.effective_urls(s, SETTINGS, "bsc")
        assert urls[:2] == ["https://bnb.alchemy.example/v2/K2", "https://env-bsc.example/K1"]
        # disable every .env / public BSC endpoint and the dashboard one: public fallback, never empty
        ov = {r["label"]: {"enabled": False} for r in rows if r["source"] != "dashboard"}
        s.add(PlatformSetting(key=reg.OVERRIDES_KEY, value=ov))
        p = (await s.execute(RpcProvider.__table__.select().where(RpcProvider.chain == "bsc"))).first()
        await s.execute(RpcProvider.__table__.update().where(RpcProvider.id == p.id).values(enabled=False))
        await s.commit()
        assert await reg.effective_urls(s, SETTINGS, "bsc") == list(CHAINS[Chain.BSC].public_rpc)
        assert await reg.effective_urls(s, SETTINGS, "robinhood") == list(CHAINS[Chain.ROBINHOOD].public_rpc)


async def test_reloader_swaps_the_running_list_without_a_restart(sf):
    rpc = make_rpc("robinhood", SETTINGS)
    assert [e.url for e in rpc.endpoints] == list(CHAINS[Chain.ROBINHOOD].public_rpc)
    async with sf() as s:
        s.add(RpcProvider(name="qn robinhood", chain="robinhood", provider_type="quicknode", priority=150,
                          rpc_url_enc=secretbox.encrypt(SETTINGS, "https://rh.quicknode.example/K4"), rpc_display="x"))
        await s.commit()
    out = await reg.make_reloader({"robinhood": rpc}, SETTINGS, sf)()
    assert [e.url for e in rpc.endpoints][0] == "https://rh.quicknode.example/K4"
    assert out["evm_endpoints"]["robinhood"][0].startswith("https://rh.quicknode.example") and "K4" not in str(out)
    await rpc.aclose()
