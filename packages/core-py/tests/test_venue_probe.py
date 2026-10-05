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


LAB = vp.VENUES["raydium_launchlab"]["program"]


def _tx(program, ix_name, accounts, inner=False, extra_keys=()):
    """A getTransaction(json) result with one `program` instruction (outer,
    or as a CPI from Jupiter) whose account list is `accounts`."""
    keys = [JUP, program, *accounts, *extra_keys]
    ix = {"programIdIndex": 1, "accounts": list(range(2, 2 + len(accounts))),
          "data": vp.b58encode(vp.disc(ix_name) + b"\x01" * 16)}
    outer = [{"programIdIndex": 0, "accounts": [], "data": vp.b58encode(b"route")}]
    msg = {"accountKeys": keys, "instructions": outer if inner else [ix]}
    meta = {"logMessages": [], "innerInstructions": [{"index": 0, "instructions": [ix]}] if inner else []}
    return {"transaction": {"message": msg}, "meta": meta}


def test_site_accounts_follow_the_idl_positions_outer_and_cpi():
    """LaunchLab: platform_config is account 3 of buys / sells / initialize;
    DBC: config is account 1 of swaps and 0 of initialize_*. Discriminators
    are Anchor's sha256("global:<name>")[:8] and match the IDLs."""
    assert list(vp.disc("buy_exact_in")) == [250, 234, 13, 123, 213, 156, 19, 236]  # raydium_launchpad IDL
    assert list(vp.disc("swap2")) == [65, 75, 63, 76, 235, 91, 91, 136]  # DBC IDL 0.2.1
    plat = "PLATbonk1111111111111111111111111111111111"
    lab = vp.VENUES["raydium_launchlab"]["site"]["index"]
    acc = ["payer", "auth", "global", plat, "pool"]
    assert vp.site_accounts(LAB, _tx(LAB, "buy_exact_in", acc), lab) == {plat: 1}
    assert vp.site_accounts(LAB, _tx(LAB, "sell_exact_out", acc, inner=True), lab) == {plat: 1}  # via Jupiter CPI
    assert vp.site_accounts(LAB, _tx(LAB, "claim_platform_fee", acc), lab) == {}  # not a trade / launch
    assert vp.site_accounts(LAB, _tx(LAB, "buy_exact_in", acc[:2]), lab) == {}  # too few accounts: skipped
    dbc = vp.VENUES["meteora_dbc"]["site"]["index"]
    assert vp.site_accounts(DBC, _tx(DBC, "swap2", ["auth", "cfgA", "pool"]), dbc) == {"cfgA": 1}
    assert vp.site_accounts(DBC, _tx(DBC, "initialize_virtual_pool_with_token2022", ["cfgB", "x"]), dbc) == {"cfgB": 1}


def test_platform_config_name_and_dbc_quote_mint_are_decoded():
    data = bytearray(vp.PLATFORM_CONFIG_DISC + b"\x00" * 600)
    data[112:112 + 7] = b"StonkFn"
    data[176:176 + 15] = b"https://x.test/"
    assert vp.decode_site("raydium_launchlab", bytes(data)) == {"name": "StonkFn", "web": "https://x.test/"}
    assert "error" in vp.decode_site("raydium_launchlab", b"\x00" * 500)  # wrong account type: not labelled
    mint = bytes(range(32))
    assert vp.decode_site("meteora_dbc", b"\x00" * 8 + mint + b"\x00" * 100) == {"quote_mint": vp.b58encode(mint)}


async def test_probe_reports_sites_with_on_chain_names():
    import base64

    vp._SITE_INFO.clear()
    t = int((NOW - timedelta(minutes=5)).timestamp())
    p1, p2 = "PLAT1111111111111111111111111111111111111", "PLAT2222222222222222222222222222222222222"
    sigs = [{"signature": f"s{i}", "err": None, "blockTime": t - i} for i in range(4)]
    acc = lambda p: ["payer", "auth", "global", p, "pool"]  # noqa: E731
    txs = {"s0": _tx(LAB, "buy_exact_in", acc(p1)), "s1": _tx(LAB, "sell_exact_in", acc(p1), inner=True),
           "s2": _tx(LAB, "buy_exact_in", acc(p2)), "s3": _tx(LAB, "initialize_v2", acc(p1))}
    name = bytearray(vp.PLATFORM_CONFIG_DISC + b"\x00" * 600)
    name[112:120] = b"LetsBONK"

    class Rpc:
        calls = []

        async def call(self, method, params=None, priority="normal"):
            self.calls.append(method)
            if method == "getSignaturesForAddress":
                return sigs
            if method == "getMultipleAccounts":
                return {"context": {}, "value": [{"data": [base64.b64encode(bytes(name)).decode(), "base64"]}
                                                 if a == p1 else None for a in params[0]]}
            return txs[params[0]]

    rpc = Rpc()
    res = await vp.probe(rpc, "raydium_launchlab", sample=4)
    ev = res.evidence()
    assert ev["sites"] == [{"address": p1, "instructions": 3, "name": "LetsBONK", "web": None},
                           {"address": p2, "instructions": 1, "error": "account not found"}]
    assert ev["sites_total"] == 2
    await vp.probe(rpc, "raydium_launchlab", sample=4)
    assert rpc.calls.count("getMultipleAccounts") == 1  # names are cached
    assert "sites" not in (await vp.probe(FakeRpc(*sigs_and_txs()), "moonshot", sample=2)).evidence()


def test_site_accounts_read_version_1_transactions_through_jsonparsed():
    """Production 2026-10-05: LaunchLab / DBC transactions are version 1; the
    probe asks for jsonParsed, where the node resolves the accounts itself."""
    lab = vp.VENUES["raydium_launchlab"]["site"]["index"]
    data = vp.b58encode(vp.disc("buy_exact_in") + b"\x00" * 8)
    tx = {"version": 1, "transaction": {"message": {"accountKeys": [{"pubkey": "P"}], "instructions": [
        {"programId": LAB, "accounts": ["payer", "auth", "cfg", "platformX", "pool"], "data": data, "stackHeight": 1}]}},
        "meta": {"innerInstructions": []}}
    assert vp.site_accounts(LAB, tx, lab) == {"platformX": 1}
