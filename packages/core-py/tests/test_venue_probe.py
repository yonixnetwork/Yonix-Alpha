"""Activity probe of the observe-only Solana launchpads (master §5-7): log
classification follows the invoke stack (an aggregator's CPI into the venue
counts for the venue, the aggregator's own instructions do not), unknown
instruction names are reported, failed transactions are skipped, and the
probe result drives Launchpad Health (activity from the newest transaction,
never 0 for counts that are not measured) while the venue stays DISABLED
(observe only) in the trading status. Fake RPC: real-chain behaviour is NOT
VERIFIED here."""

import os
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import create_async_engine

from yonixalpha_core.chains import activity, verification
from yonixalpha_core.chains.registry import LAUNCHPADS
from yonixalpha_core.solana import venue_probe as vp

DBC = vp.VENUES["meteora_dbc"]["program"]
JUP = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"
NOW = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)


def logs_for(*ix: tuple[str, str], fail: bool = False) -> list[str]:
    """Logs of one transaction: a Jupiter route calling DBC (CPI) per entry."""
    out = [f"Program {JUP} invoke [1]", "Program log: Instruction: Route"]
    for program, name in ix:
        out += [f"Program {program} invoke [2]", f"Program log: Instruction: {name}",
                f"Program {program} {'failed: custom program error' if fail else 'success'}"]
    out.append(f"Program {JUP} success")
    return out


def test_logs_are_attributed_through_the_invoke_stack():
    kinds = vp.VENUES["meteora_dbc"]["kinds"]
    found, unknown = vp.classify_logs(DBC, logs_for((DBC, "Swap2"), (DBC, "InitializeVirtualPoolWithSplToken"),
                                                    (DBC, "ClaimTradingFee")), kinds)
    assert found == {"trade": 1, "launch": 1} and unknown == {"ClaimTradingFee": 1}
    assert vp.classify_logs(DBC, logs_for(), kinds) == ({}, {})  # Jupiter's own "Route" is not DBC's
    assert kinds["Swap2WithTransferHook"] == "trade" and kinds["MigrationDammV2"] == "migration"
    assert vp.VENUES["raydium_launchlab"]["kinds"]["InitializeWithToken2022"] == "launch"
    assert vp.VENUES["moonshot"]["kinds"]["MigrateFunds"] == "migration"


class FakeRpc:
    def __init__(self, sigs, txs, fail=False):
        self.sigs, self.txs, self.fail, self.calls = sigs, txs, fail, []

    async def call(self, method, params=None, priority="normal"):
        self.calls.append((method, priority))
        if self.fail:
            raise RuntimeError("429 rate limited")
        if method == "getSignaturesForAddress":
            return self.sigs
        return {"meta": {"logMessages": self.txs[params[0]]}}


def sigs_and_txs():
    t = int((NOW - timedelta(minutes=10)).timestamp())
    sigs = [{"signature": f"s{i}", "err": None, "blockTime": t - i * 6} for i in range(10)]
    sigs.insert(1, {"signature": "bad", "err": {"InstructionError": [0, "x"]}, "blockTime": t})
    txs = {f"s{i}": logs_for((DBC, "Swap")) for i in range(10)}
    txs["s3"] = logs_for((DBC, "InitializeVirtualPoolWithToken2022"))
    return sigs, txs


async def test_probe_measures_rate_last_transaction_and_sampled_kinds():
    sigs, txs = sigs_and_txs()
    rpc = FakeRpc(sigs, txs)
    res = await vp.probe(rpc, "meteora_dbc", sample=5)
    assert res.ok and res.signatures == 11 and res.successful == 10  # the failed tx is skipped
    assert res.last_tx_at == NOW - timedelta(minutes=10) and res.span_s == 54.0 and res.rate_per_min == round(10 / 54 * 60, 2)
    assert res.sampled == 5 and res.kinds == {"trade": 4, "launch": 1}
    assert res.last_seen["launch"] == NOW - timedelta(minutes=10, seconds=18)
    assert all(p == "background" for _, p in rpc.calls)
    down = await vp.probe(FakeRpc([], {}, fail=True), "moonshot")
    assert not down.ok and "429" in down.error


async def test_probe_drives_health_but_venue_stays_observe_only():
    engine = create_async_engine(os.environ.get(
        "DATABASE_URL", "postgresql+asyncpg://yonixalpha:yonixalpha_test_pw@localhost:5432/yonixalpha_test"))
    from yonixalpha_core.db import models  # noqa: F401
    from yonixalpha_core.db.base import Base, make_session_factory

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    try:
        sigs, txs = sigs_and_txs()
        res = await vp.probe(FakeRpc(sigs, txs), "meteora_dbc", sample=5)
        async with make_session_factory(engine)() as s:
            old = vp.ProbeResult("meteora_dbc", DBC, ok=True, last_tx_at=NOW - timedelta(days=2))
            old.kinds.update({"trade": 3})
            old.last_seen["migration"] = NOW - timedelta(days=2)
            await vp.record(s, old, NOW - timedelta(days=2))
            await vp.record(s, res, NOW)
            await s.commit()
            spec = LAUNCHPADS["meteora_dbc"]
            a = await activity.launchpad_activity(s, spec, "LIVE", None, now=NOW + timedelta(minutes=1))
            st = await verification.status_for(s, None, spec, "LIVE", now=NOW + timedelta(minutes=1))
            lab = await activity.launchpad_activity(s, LAUNCHPADS["raydium_launchlab"], "LIVE", None, now=NOW)
        assert a["activity_status"] == "ACTIVE" and a["last_transaction"] == NOW - timedelta(minutes=10)
        assert a["last_launch"] == NOW - timedelta(minutes=10, seconds=18)
        assert a["last_migration"] == NOW - timedelta(days=2)  # from an earlier probe's sample
        assert a["launches_7d"] is None and a["trades_7d"] is None and a["volume_7d"] is None  # not measured, not 0
        assert a["probe"]["sample_kinds"] == {"trade": 4, "launch": 1} and a["monitor_at"] == NOW
        assert st["status"] == "DISABLED" and "observe only" in st["why"]
        assert st["checks"]["EVENTS"]["status"] == "PASS" and st["checks"]["BUY"]["status"] == "NOT_RUN"
        assert lab["activity_status"] == "UNVERIFIED" and lab["last_transaction"] is None  # never probed yet
    finally:
        await engine.dispose()
