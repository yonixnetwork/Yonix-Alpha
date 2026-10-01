"""data-evm end to end against a fake BSC node + real Postgres / Redis:
discovery (idempotent across a re-scan), stats and category, safety,
the launchpad-evidence gate on entries, a paper entry through plan_trade,
no duplicate entry, a stop-loss exit at the executable sell quote, the kill
switch, and evidence recording. Proves the pipeline logic, not real-chain
behaviour (NOT VERIFIED on chain)."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select

from yonixalpha_core import kill_switch
from yonixalpha_core.chains import verification
from yonixalpha_core.chains.base import PAPER_REQUIRED
from yonixalpha_core.chains.evm import settings as evm_settings
from yonixalpha_core.chains.evm import store
from yonixalpha_core.chains.evm.abi import ZERO_ADDRESS
from yonixalpha_core.chains.evm.fourmeme import EVENTS, FourMeme
from yonixalpha_core.db.models import EvmToken, EvmTrade, LaunchpadCheck, PaperAccount, PaperPosition
from yonixalpha_core.testing.evm_node import Node, enc, log_of, rpc_for

from app.worker import ChainWorker

TOKEN = "0x1111111111111111111111111111111111111111"
CREATOR = "0x9999999999999999999999999999999999999999"
PRICES = [95, 100, 104, 108, 97, 101, 105, 109, 98, 102]  # per-minute closes swing ~7%: a stop wider than costs


def fourmeme_node(now: datetime) -> tuple[Node, FourMeme]:
    node = Node(56)
    node.genesis_ts = int(now.timestamp()) - node.head  # block N is N seconds before `now`... head == now
    lp = FourMeme(rpc_for(node))
    mgr = lp.spec.contracts["manager_v2"]
    ev = EVENTS.by_name
    node.logs = [log_of(ev["TokenCreate"], {"creator": CREATOR, "token": TOKEN, "requestId": 1, "name": "Moon",
                                            "symbol": "MOON", "totalSupply": 10 ** 27, "launchTime": 1, "launchFee": 0},
                        mgr, 690, 0)]
    for i, px in enumerate(PRICES):  # one trade every 30 s over the last 5 minutes
        buy = i not in (4, 7)
        node.logs.append(log_of(ev["TokenPurchase" if buy else "TokenSale"], {
            "token": TOKEN, "account": f"0x{(i % 8) + 1:040x}", "price": px, "amount": 10 ** 21,
            "cost": px * 10 ** 15 // 100, "fee": 10 ** 13, "offers": 1, "funds": 1}, mgr, 700 + i * 30, i + 1))
    node.on(lp.helper, "getTokenInfo(address)", enc(
        ["uint256", "address", "address"] + ["uint256"] * 8 + ["bool"],
        [2, mgr, ZERO_ADDRESS, 10 ** 12, 100, 0, 1, 1, 1, 2 * 10 ** 18, 24 * 10 ** 18, False]))
    node.on(TOKEN, "totalSupply()", enc(["uint256"], [10 ** 27]))  # read by the launch-coordination check
    set_quotes(node, lp, sell_back=Decimal("0.98"))
    return node, lp


def set_quotes(node: Node, lp: FourMeme, sell_back: Decimal) -> None:
    """Buy: 1 token-wei per 1e-? ... fixed rate 1e6 tokens per quote unit; sell returns `sell_back` of cost."""
    def try_buy(p):
        funds = int(p[0]["data"][-64:], 16)
        return enc(["address", "address"] + ["uint256"] * 6, [TOKEN, ZERO_ADDRESS, funds * 10 ** 6, funds, 0, 0, 0, 0])

    def try_sell(p):
        amount = int(p[0]["data"][-64:], 16)
        return enc(["address", "address", "uint256", "uint256"],
                   [TOKEN, ZERO_ADDRESS, int(Decimal(amount) / 10 ** 6 * sell_back), 0])

    node.on(lp.helper, "tryBuy(address,uint256,uint256)", try_buy)
    node.on(lp.helper, "trySell(address,uint256)", try_sell)


def settings():
    s, errors = evm_settings.parse({"bsc": {"confirmations": 0}, "fresh_min_buys": 5, "fresh_min_unique_buyers": 4})
    assert not errors
    return s


async def record_paper_evidence(session_factory, now):
    async with session_factory() as session:
        for c in PAPER_REQUIRED:
            await verification.record(session, "fourmeme", c, True, {"test": True}, "test", now)
        await session.commit()


async def test_pipeline_discovery_safety_entry_exit(session_factory, redis_client):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    node, lp = fourmeme_node(now)
    w = ChainWorker("bsc", lp.rpc, [lp], session_factory, redis_client)
    s = settings()

    out = await w.discovery_pass(s, now)
    assert out["fourmeme"]["launches"] == 1 and out["fourmeme"]["trades"] == 10
    async with session_factory() as session:
        row = await session.get(EvmToken, ("bsc", TOKEN))
        assert row.category == "FRESH" and row.stats["buys"] == 8 and row.stats["volatility"] is not None
        await store.set_cursor(session, "bsc", "fourmeme", 600, now)  # a restart that re-scans the same blocks
        await session.commit()
    await w.discovery_pass(s, now)
    async with session_factory() as session:
        assert (await session.execute(select(func.count()).select_from(EvmTrade))).scalar_one() == 10
        assert await store.get_cursor(session, "bsc", "fourmeme") == node.head

    assert await w.safety_pass(s, now) == 1
    async with session_factory() as session:
        row = await session.get(EvmToken, ("bsc", TOKEN))
        assert row.safety_verdict == "PASS", row.safety

    counts = await w.entry_pass(s, now)
    assert counts == {"evaluated": 1, "opened": 0}
    async with session_factory() as session:
        row = await session.get(EvmToken, ("bsc", TOKEN))
        codes = {b["code"] for b in row.extra["entry_decision"]["blockers"]}
        assert codes == {"LAUNCHPAD_NOT_VERIFIED"}  # never traded before real-chain evidence

    await record_paper_evidence(session_factory, now)
    counts = await w.entry_pass(s, now)
    assert counts["opened"] == 1, (await _decision(session_factory))
    assert (await w.entry_pass(s, now))["opened"] == 0  # no second entry on the same token
    async with session_factory() as session:
        p = (await session.execute(select(PaperPosition))).scalar_one()
        assert p.engine == "evm_bsc" and p.execution_mode in (None, "PAPER") and p.status == "open"
        acct = await session.get(PaperAccount, p.account_id)
        assert acct.quote_currency == "BNB" and acct.cash_balance == Decimal("1") - p.entry_cost_quote
        entry_cost = p.entry_cost_quote

    r = await w.manage_pass(now)
    assert r["managed"] == 1 and r["closed"] == 0

    # a SELL_ONLY copy target sold half of its holding: the copy engine queued
    # half of this position; data-evm (which owns it) fills it on its next pass
    from yonixalpha_core import copy_trading as ct
    async with session_factory() as session:
        p = (await session.execute(select(PaperPosition))).scalar_one()
        before = p.remaining_quantity
        p.plan = ct.queue_partial_exit(p.plan, Decimal("0.5"), now)
        await session.commit()
    r = await w.manage_pass(now)
    async with session_factory() as session:
        p = (await session.execute(select(PaperPosition))).scalar_one()
        assert p.status == "open" and p.remaining_quantity == before / 2 and ct.pending_partial_exit(p.plan) is None

    set_quotes(node, lp, sell_back=Decimal("0.4"))  # the exit quote collapses below the stop
    r = await w.manage_pass(now)
    assert r["closed"] == 1
    async with session_factory() as session:
        p = (await session.execute(select(PaperPosition))).scalar_one()
        assert p.status == "closed" and p.exit_reason == "stop_loss"
        assert p.realized_pnl == p.proceeds_quote - entry_cost and p.realized_pnl < 0

    n = await w.evidence_pass(now, force=True)
    async with session_factory() as session:
        checks = set((await session.execute(select(LaunchpadCheck.check).where(
            LaunchpadCheck.source == "data-evm"))).scalars())
    assert n >= 5 and {"ACTIVE", "DISCOVERY", "EVENTS", "QUOTE", "SAFETY", "LIQUIDITY"} <= checks


async def test_a_bundled_launch_is_not_entered_unless_the_operator_says_otherwise(session_factory, redis_client):
    """Three wallets bought in the launch transaction's own block (master §11):
    LAUNCH_BLOCK_BUNDLE, NO_TRADE by default. The operator can set that
    detection to NONE (report only); the finding is still shown."""
    from yonixalpha_core import launch_coordination as lc
    from yonixalpha_core.db.models import PlatformSetting

    now = datetime.now(timezone.utc).replace(microsecond=0)
    node, lp = fourmeme_node(now)
    ev = EVENTS.by_name
    for i in range(3):
        node.logs.append(log_of(ev["TokenPurchase"], {
            "token": TOKEN, "account": f"0x{0xb0 + i:040x}", "price": 95, "amount": 10 ** 21, "cost": 95 * 10 ** 13,
            "fee": 10 ** 13, "offers": 1, "funds": 1}, lp.spec.contracts["manager_v2"], 690, 50 + i))  # unique event ids
    w = ChainWorker("bsc", lp.rpc, [lp], session_factory, redis_client)
    s = settings()
    await w.discovery_pass(s, now)
    await w.safety_pass(s, now)
    await record_paper_evidence(session_factory, now)
    assert (await w.entry_pass(s, now))["opened"] == 0
    async with session_factory() as session:
        row = await session.get(EvmToken, ("bsc", TOKEN))
        assert row.coordination["action"] == "NO_TRADE" and row.coordination_at == now
        assert {f["code"] for f in row.coordination["findings"]} == {"LAUNCH_BLOCK_BUNDLE"}
        assert row.coordination["window"]["buyers"] == 5
        blockers = {b["code"] for b in row.extra["entry_decision"]["blockers"]}
        assert blockers == {"LAUNCH_COORDINATION"}
        session.add(PlatformSetting(key=lc.SETTINGS_KEY, value={"actions": {"LAUNCH_BLOCK_BUNDLE": "NONE"}}))
        await session.commit()
    later = now + timedelta(minutes=3)
    await w.safety_pass(s, later)
    assert (await w.entry_pass(s, later))["opened"] == 1
    async with session_factory() as session:
        row = await session.get(EvmToken, ("bsc", TOKEN))
        assert row.coordination["status"] == "COORDINATION_DETECTED" and row.coordination["action"] == "NONE"


async def test_kill_switch_and_controls_block_entries(session_factory, redis_client):
    from yonixalpha_core.chains import controls

    now = datetime.now(timezone.utc).replace(microsecond=0)
    node, lp = fourmeme_node(now)
    w = ChainWorker("bsc", lp.rpc, [lp], session_factory, redis_client)
    s = settings()
    await w.discovery_pass(s, now)
    await w.safety_pass(s, now)
    await record_paper_evidence(session_factory, now)
    await kill_switch.engage(redis_client, "test")
    async with session_factory() as session:
        await controls.set_control(session, "chain:bsc", enabled=False, mode=None, note=None, user="test")
        await session.commit()
    assert (await w.entry_pass(s, now))["opened"] == 0
    codes = {b["code"] for b in (await _decision(session_factory))["blockers"]}
    assert {"KILL_SWITCH", "TRADING_CONTROL_OFF"} <= codes


async def test_restart_restores_announced_contracts(session_factory, redis_client):
    """A Pons V2 curve announced before a restart is accepted after it."""
    from yonixalpha_core.chains.evm.pons import V2_EVENTS, PonsV2

    now = datetime.now(timezone.utc).replace(microsecond=0)
    node = Node(4663)
    node.genesis_ts = int(now.timestamp()) - node.head
    lp = PonsV2(rpc_for(node, "robinhood"))
    curve = "0x3333333333333333333333333333333333333333"
    node.logs = [log_of(V2_EVENTS.by_name["TokenLaunched"], {
        "token": TOKEN, "curve": curve, "deployer": CREATOR, "pairToken": ZERO_ADDRESS, "launchConfigId": 1,
        "graduationThreshold": 10 ** 18}, lp.factory, 900, 0)]
    s = settings()
    await ChainWorker("robinhood", lp.rpc, [lp], session_factory, redis_client).discovery_pass(s, now)
    fresh = PonsV2(rpc_for(node, "robinhood"))  # a new process
    w2 = ChainWorker("robinhood", fresh.rpc, [fresh], session_factory, redis_client)
    await w2.restore()
    assert fresh.curves == {curve.lower(): TOKEN}


async def _decision(session_factory):
    async with session_factory() as session:
        return (await session.get(EvmToken, ("bsc", TOKEN))).extra.get("entry_decision")


async def test_discovery_far_behind_jumps_to_recent_blocks_and_says_so(session_factory, redis_client):
    """Robinhood discovery fell ~10 hours behind after hours of HTTP 429 (seen
    on the server): replaying that is useless for real-time entries. Past
    max_lag_minutes the cursor jumps to the last backfill_minutes, and the
    skipped range is reported, never silent. 0 disables the jump."""
    now = datetime.now(timezone.utc).replace(microsecond=0)
    node, lp = fourmeme_node(now)  # 1 s per block, head 1000; trades at blocks 700..970
    s, errors = evm_settings.parse({"bsc": {"confirmations": 0, "max_lag_minutes": 5, "backfill_minutes": 2}})
    assert not errors
    async with session_factory() as session:
        await store.set_cursor(session, "bsc", "fourmeme", 10, now)
        await session.commit()
    w = ChainWorker("bsc", lp.rpc, [lp], session_factory, redis_client)
    out = (await w.discovery_pass(s, now))["fourmeme"]
    assert out["skipped"] == {"from": 11, "to": 880, "blocks": 870, "lag_minutes": 16.5}
    assert out["from"] == 881 and out["trades"] == 3  # blocks 910, 940, 970
    async with session_factory() as session:
        assert await store.get_cursor(session, "bsc", "fourmeme") == node.head

    s0, _ = evm_settings.parse({"bsc": {"confirmations": 0, "max_lag_minutes": 0}})
    async with session_factory() as session:
        await store.set_cursor(session, "bsc", "fourmeme", 10, now)
        await session.commit()
    out = (await w.discovery_pass(s0, now))["fourmeme"]
    assert "skipped" not in out and out["from"] == 11


async def test_a_short_rpc_cooldown_is_not_alerted_but_a_persisting_one_is(session_factory, redis_client, monkeypatch):
    """Seen on the server: Robinhood's only public RPC answers 429 and discovery
    fails for a few seconds ("all cooling down for 4s more"). Nothing is lost
    (the cursor resumes), so Telegram hears about it only when it lasts
    RPC_OUTAGE_ALERT_SECONDS; any other discovery error is alerted at once."""
    import app.worker as worker_mod
    from yonixalpha_core.chains.evm.rpc import EvmRpcUnavailableError

    now = datetime.now(timezone.utc)
    node, lp = fourmeme_node(now)
    sent = []

    async def fake_alert(service, event, detail=None, *a, **k):
        sent.append((event, detail))
        return True

    async def cooling(*a, **k):
        raise EvmRpcUnavailableError("bsc RPC unavailable: all cooling down for 4s more")

    monkeypatch.setattr(worker_mod, "alert_error", fake_alert)
    monkeypatch.setattr(lp, "scan", cooling)
    s, _ = evm_settings.parse({"bsc": {"confirmations": 0}})
    w = ChainWorker("bsc", lp.rpc, [lp], session_factory, redis_client)
    out = await w.discovery_pass(s, now)
    assert "cooling down" in out["fourmeme"]["error"] and sent == []
    w.failing_since["fourmeme"] -= worker_mod.RPC_OUTAGE_ALERT_SECONDS  # it has now lasted that long
    await w.discovery_pass(s, now)
    assert sent and sent[-1][0] == "bsc.fourmeme.discovery_failed" and sent[-1][1]["failing_for_s"] >= 120

    async def broken(*a, **k):
        raise ValueError("bad log")

    sent.clear()
    w.failing_since.clear()
    monkeypatch.setattr(lp, "scan", broken)
    await w.discovery_pass(s, now)
    assert [e for e, _ in sent] == ["bsc.fourmeme.discovery_failed"]  # not an RPC outage: at once
