"""data-evm end to end against a fake BSC node + real Postgres / Redis:
discovery (idempotent across a re-scan), stats and category, safety,
the launchpad-evidence gate on entries, a paper entry through plan_trade,
no duplicate entry, a stop-loss exit at the executable sell quote, the kill
switch, and evidence recording. Proves the pipeline logic, not real-chain
behaviour (NOT VERIFIED on chain)."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from yonixalpha_core import kill_switch
from yonixalpha_core.chains import verification
from yonixalpha_core.chains.base import PAPER_REQUIRED
from yonixalpha_core.chains.evm import settings as evm_settings
from yonixalpha_core.chains.evm import paper, store
from yonixalpha_core.chains.evm.abi import ZERO_ADDRESS
from yonixalpha_core.chains.evm.fourmeme import EVENTS, FourMeme
from yonixalpha_core.db.models import (EvmExitSample, EvmObservation, EvmScanGap, EvmToken, EvmTrade, LaunchpadCheck,
                                       PaperAccount, PaperPosition)
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
    node.on(lp.spec.contracts["manager_v2"], "buyTokenAMAP(address,uint256,uint256)", "0x")  # not X Mode
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
    # master §68: what the log scan stored is counted for the stream cross-check, once despite the re-scan
    from yonixalpha_core.chains.evm import crosscheck
    rep = await crosscheck.report(redis_client, "bsc", datetime.now(timezone.utc))
    assert rep["launches_seen_first_by_stream"]["logged"] == 1 and rep["launches_seen_first_by_stream"]["rate"] == 0.0
    assert 1 <= rep["trades_seen_first_by_stream"]["logged"] <= 10
    assert (await w.crosscheck_pass(now))["checked"] == 0  # no stream ran: nothing to check

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
        rec = row.extra["entry_decision"]  # master §77 record, decided by the §76 hierarchy
        assert (rec["decision"], rec["layer"]) == ("NO_TRADE", "DATA_SAFETY")
        assert rec["safety_evidence"]["verdict"] == "PASS" and rec["ml_evidence"]["contribution_pct"] == 0
        assert rec["provider_status"]["rpc"]["available"] is True and rec["features"]["category"] == row.category

    await record_paper_evidence(session_factory, now)
    counts = await w.entry_pass(s, now)
    assert counts["opened"] == 1, (await _decision(session_factory))
    rec = await _decision(session_factory)
    assert rec["decision"] in ("EXECUTE", "REDUCE_SIZE") and rec["blockers"] == [] and rec["risk"]["plan"]["size"]
    assert (await w.entry_pass(s, now))["opened"] == 0  # no second entry on the same token
    from yonixalpha_core.db.models import EvmObservation
    async with session_factory() as session:  # master §14: observed from the launch, entered through observation
        o = (await session.execute(select(EvmObservation))).scalar_one()
        assert o.category == "FRESH" and o.state == "ENTERED" and o.observation_reason == "launch observed"
        assert [h["state"] for h in o.history][:2] == ["DISCOVERED", "OBSERVING"]
    assert (await w.observation_pass(now))["snapshots"] == 2  # T0 and T+5 (the launch was 310 s ago)
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
        # master §41 exits: a checkpoint per tick here (HOLD, the mirrored copy sell, the stop)
        xs = (await session.execute(select(EvmExitSample).order_by(EvmExitSample.id))).scalars().all()
        assert [(x.verdicts["deterministic"], x.verdicts["risk"], x.verdicts["final"]) for x in xs] == [
            ("HOLD", "HOLD", "HOLD"), ("SELL", "HOLD", "SELL"), ("HOLD", "SELL", "SELL")]
        assert xs[2].exit_reasons == ["stop_loss"] and xs[0].features["held_s"] == 0.0 and xs[0].launchpad == "fourmeme"

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
        gap = (await session.execute(select(EvmScanGap))).scalar_one()  # recorded with the cursor move
        assert (gap.from_block, gap.to_block, gap.status) == (11, 880, "PENDING")
        n_obs = (await session.execute(select(func.count()).select_from(EvmObservation))).scalar_one()

    # master §68-70: the skipped range is backfilled, only while the live scan is caught up
    assert await w.gap_pass(s, now, {"fourmeme": {"lag": 10 ** 6}}) == {}
    back = (await w.gap_pass(s, now, {"fourmeme": {"lag": 0}}))["fourmeme"]
    assert back["status"] == "DONE" and back["trades"] == 7 and back["launches"] == 1  # blocks 690 .. 880
    async with session_factory() as session:
        gap = (await session.execute(select(EvmScanGap))).scalar_one()
        assert (gap.status, gap.trades, gap.launches, gap.next_block) == ("DONE", 7, 1, 881)
        assert await store.get_cursor(session, "bsc", "fourmeme") == node.head  # history only: the cursor stays
        assert (await session.execute(select(func.count()).select_from(EvmTrade))).scalar_one() == 10
        row = await session.get(EvmToken, ("bsc", TOKEN))
        assert row.extra["backfilled"] is True and row.created_block == 690
        # a backfilled launch is past its window: no observation is opened, so it is never entered
        assert (await session.execute(select(func.count()).select_from(EvmObservation))).scalar_one() == n_obs
    assert await w.gap_pass(s, now, {"fourmeme": {"lag": 0}}) == {}

    async with session_factory() as session:  # a gap older than the trade retention is expired, not scanned
        session.add(EvmScanGap(chain="bsc", launchpad="fourmeme", from_block=1, to_block=5, next_block=1,
                               status="PENDING", detected_at=now - timedelta(days=15), reason={}, launches=0, trades=0,
                               attempts=0))
        await session.commit()
    assert await w.gap_pass(s, now, {"fourmeme": {"lag": 0}}) == {}
    async with session_factory() as session:
        assert (await session.execute(select(EvmScanGap.status).order_by(EvmScanGap.id))).scalars().all() == \
            ["DONE", "EXPIRED"]

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


async def test_streams_watch_copy_targets_and_launchpad_contracts(session_factory, redis_client):
    """Master §9 / §13: data-evm builds the Robinhood sequencer feed and the
    BSC pending stream; they watch enabled copy targets and the launchpads'
    own contracts, and BSC without a WSS endpoint has no stream URL."""
    from yonixalpha_core.chains.evm import streams
    from yonixalpha_core.db.models import CopyTarget, PlatformSetting

    from app.main import _streams

    now = datetime.now(timezone.utc)
    node, lp = fourmeme_node(now)
    worker = ChainWorker("bsc", lp.rpc, [lp], session_factory, redis_client)
    async with session_factory() as s:
        s.add_all([CopyTarget(chain="bsc", wallet="0x" + "ab" * 20, mode="NOTIFY", enabled=True, settings={}),
                   CopyTarget(chain="bsc", wallet="0x" + "cd" * 20, mode="NOTIFY", enabled=False, settings={}),
                   PlatformSetting(key=streams.SETTINGS_KEY, value={"bsc_pending_enabled": False})])
        await s.commit()
    feed, pending = _streams(None, session_factory, redis_client, {"bsc": worker})  # app settings: only for WSS decryption
    assert isinstance(feed, streams.SequencerFeed) and feed.chain == "robinhood"
    wallets, contracts = await pending.watch()
    assert wallets == {"0x" + "ab" * 20}  # the disabled target is not watched
    assert lp.spec.contracts["manager_v2"].lower() in contracts
    rh_wallets, rh_contracts = await feed.watch()
    assert not rh_wallets and streams.EXTRA_CONTRACTS["robinhood"] <= rh_contracts
    assert await pending.urls() == [] and await pending.enabled() is False
    assert (await feed.reload()).robinhood_feed_enabled


async def test_quote_pass_records_the_quote_of_tokens_stored_without_one(session_factory, redis_client):
    """Four.meme tokens first seen before their quote was read get it from
    getTokenInfo (so stock-quoted curves leave native-unit sums); a token whose
    quote cannot be read is marked and not asked again."""
    now = datetime.now(timezone.utc).replace(microsecond=0)
    node, lp = fourmeme_node(now)
    stock, bncb = "0x" + "77" * 20, "0x4902c5EBc598265ed2212B559B042de8A5eeEc3f"
    dead = "0x" + "88" * 20
    mgr = lp.spec.contracts["manager_v2"]
    by_token = {TOKEN.lower(): ZERO_ADDRESS, stock.lower(): bncb}

    def info(p):
        tok = "0x" + p[0]["data"][-40:]
        if tok not in by_token:
            raise Exception("execution reverted")
        return enc(["uint256", "address", "address"] + ["uint256"] * 8 + ["bool"],
                   [2, mgr, by_token[tok], 10 ** 12, 100, 0, 1, 1, 1, 2 * 10 ** 18, 24 * 10 ** 18, False])

    node.on(lp.helper, "getTokenInfo(address)", info)
    async with session_factory() as session:
        for tok in (TOKEN, stock, dead):
            session.add(EvmToken(chain="bsc", token=tok, launchpad="fourmeme", created_at=now, last_trade_at=now,
                                 extra={}))
        await session.commit()
    w = ChainWorker("bsc", lp.rpc, [lp], session_factory, redis_client)
    assert await w.quote_pass(now) == 3
    async with session_factory() as session:
        got = {t: await session.get(EvmToken, ("bsc", t)) for t in (TOKEN, stock, dead)}
        assert got[TOKEN].quote_token == ZERO_ADDRESS and got[stock].quote_token.lower() == bncb.lower()
        assert got[dead].quote_token is None and got[dead].extra["quote_lookup"] == "unreadable"
    assert await w.quote_pass(now) == 0  # nothing left to ask


async def _ready_for_entry(session_factory, redis_client, now):
    node, lp = fourmeme_node(now)
    w = ChainWorker("bsc", lp.rpc, [lp], session_factory, redis_client)
    s = settings()
    await w.discovery_pass(s, now)
    assert await w.safety_pass(s, now) == 1
    await record_paper_evidence(session_factory, now)
    return node, w, s


async def test_an_entry_needs_gas_for_both_swaps_and_the_gas_reserve(session_factory, redis_client):
    """Master §57: gas is verified before the entry. The paper book must pay
    the buy's and the sell's gas at the current gas price plus the chain's gas
    reserve, or the entry is NO_TRADE: INSUFFICIENT GAS. An unreadable gas
    price is never assumed affordable."""
    now = datetime.now(timezone.utc).replace(microsecond=0)
    node, w, s = await _ready_for_entry(session_factory, redis_client, now)
    async with session_factory() as session:
        acct = await paper.ensure_account(session, "bsc")
        acct.cash_balance = Decimal("0.0015")  # less than 0.0006 gas (2 x 300k x 1 gwei) + 0.002 reserve
        await session.commit()
    assert (await w.entry_pass(s, now))["opened"] == 0
    d = await _decision(session_factory)
    blockers = {b["code"]: b["message"] for b in d["blockers"]}
    assert set(blockers) == {"INSUFFICIENT_GAS"} and blockers["INSUFFICIENT_GAS"].startswith("INSUFFICIENT GAS: 0.0015 BNB")
    assert d["detail"]["gas"]["round_trip_gas"] == "0.0006" and d["detail"]["gas"]["gas_price_gwei"] == "1"

    node.gas_price = None  # the node does not answer eth_gasPrice
    async with session_factory() as session:
        (await paper.ensure_account(session, "bsc")).cash_balance = Decimal("1")
        await session.commit()
    assert (await w.entry_pass(s, now))["opened"] == 0
    assert {b["code"] for b in (await _decision(session_factory))["blockers"]} == {"GAS_PRICE_UNAVAILABLE"}

    node.gas_price = 10 ** 9
    assert (await w.entry_pass(s, now))["opened"] == 1
    async with session_factory() as session:
        p = (await session.execute(select(PaperPosition))).scalar_one()
        assert (await session.get(PaperAccount, p.account_id)).cash_balance >= Decimal("0.0026")  # gas money kept


async def test_wallet_pass_syncs_the_evm_balance_and_native_usd_rates(session_factory, redis_client):
    """Master §56: the EVM account's balance is read every minute; on BSC the
    BNB / ETH USD rates come from PancakeSwap V2 quotes (executable, not an
    index); an implausible quote is not stored."""
    import json

    from yonixalpha_core.chains.evm import native_price
    from yonixalpha_core.chains.registry import BSC_PANCAKE_V2

    now = datetime.now(timezone.utc).replace(microsecond=0)
    node, lp = fourmeme_node(now)
    addr = "0x" + "ab" * 20

    async def bal(*_a):
        return 2 * 10 ** 18

    lp.rpc.get_balance = bal
    prices = {native_price.PAIRS["BNB"].lower(): 600, native_price.PAIRS["ETH"].lower(): 5}  # ETH 5 USD: not sane

    def amounts(p):
        data = p[0]["data"]
        token_in = "0x" + data[2 + 8 + 64 * 3 + 24: 2 + 8 + 64 * 4]  # amountIn, offset, length, path[0]
        return enc(["uint256[]"], [[10 ** 18, prices[token_in] * 10 ** 18]])

    node.on(BSC_PANCAKE_V2["router"], "getAmountsOut(uint256,address[])", amounts)
    w = ChainWorker("bsc", lp.rpc, [lp], session_factory, redis_client, wallet_address=addr)
    out = await w.wallet_pass(now)
    assert out["balance"] == "2" and out["native_usd"]["BNB"] == {"price": "600"} and "error" in out["native_usd"]["ETH"]
    assert json.loads(await redis_client.get("yx:evm:wallet:bsc"))["address"] == addr
    assert (await native_price.usd_rate(redis_client, "bsc", now))["price"] == "600"
    assert (await native_price.usd_rate(redis_client, "robinhood", now))["price"] is None  # ETH quote rejected


async def test_manual_buy_runs_every_entry_check_but_replaces_the_signal(session_factory, redis_client):
    """Master §45: a manual BUY on BSC is queued, re-checked (safety re-run
    when stale) and executed by the same entry code as automatic trading. The
    operator's decision replaces only the trade signal: a token with too few
    buys can be bought by hand, a launchpad without verified evidence still
    cannot. EVM is paper only."""
    from yonixalpha_core.chains.evm import manual

    now = datetime.now(timezone.utc).replace(microsecond=0)
    node, lp = fourmeme_node(now)
    w = ChainWorker("bsc", lp.rpc, [lp], session_factory, redis_client)
    s, _ = evm_settings.parse({"bsc": {"confirmations": 0}, "fresh_min_buys": 50})  # the strategy would never buy
    await w.discovery_pass(s, now)

    async with session_factory() as session:
        with pytest.raises(manual.ManualTradeError):
            await manual.create_request(session, redis_client, "bsc", "0x" + "12" * 20, "op", now)  # not discovered
        req = await manual.create_request(session, redis_client, "bsc", TOKEN.lower(), "op", now)
    assert req["status"] == "QUEUED" and req["mode"] == "PAPER" and req["token"] == TOKEN

    out = await w.manual_pass(s, now)  # safety never ran: it runs now; the launchpad has no evidence yet
    got = await manual.get(redis_client, req["id"])
    assert out == {"processed": 1, "opened": 0} and got["status"] == "BLOCKED"
    assert {b["code"] for b in got["blockers"]} == {"LAUNCHPAD_NOT_VERIFIED"}  # not TOO_FEW_BUYS: signal replaced
    async with session_factory() as session:
        row = await session.get(EvmToken, ("bsc", TOKEN))
        assert row.safety_verdict == "PASS" and row.safety_at == now  # re-run for the request
        assert row.extra["manual_decision"]["source"] == "manual"

    await record_paper_evidence(session_factory, now)
    async with session_factory() as session:
        req2 = await manual.create_request(session, redis_client, "bsc", TOKEN, "op", now)
    assert (await w.manual_pass(s, now))["opened"] == 1
    got = await manual.get(redis_client, req2["id"])
    assert got["status"] == "PAPER_POSITION_OPEN" and got["detail"]["signal"].startswith("replaced by the operator")
    async with session_factory() as session:
        p = (await session.execute(select(PaperPosition))).scalar_one()
        assert str(p.id) == got["position_id"] and p.plan["entry_source"] == "manual" and p.engine == "evm_bsc"

    async with session_factory() as session:  # a second manual buy: the re-entry cooldown still applies
        req3 = await manual.create_request(session, redis_client, "bsc", TOKEN, "op", now)
    await w.manual_pass(s, now)
    assert "ALREADY_TRADED" in {b["code"] for b in (await manual.get(redis_client, req3["id"]))["blockers"]}

    async with session_factory() as session:  # not taken within 10 minutes: never executed late
        req4 = await manual.create_request(session, redis_client, "bsc", TOKEN, "op", now - timedelta(minutes=11))
    await w.manual_pass(s, now)
    assert (await manual.get(redis_client, req4["id"]))["status"] == "EXPIRED"


async def test_manual_buy_is_refused_on_an_observe_only_venue(session_factory, redis_client):
    from yonixalpha_core.chains.evm import manual

    now = datetime.now(timezone.utc)
    async with session_factory() as session:
        session.add(EvmToken(chain="bsc", token="0x" + "aa" * 20, launchpad="genius_fun", created_at=now))
        await session.commit()
        with pytest.raises(manual.ManualTradeError, match="observe only"):
            await manual.create_request(session, redis_client, "bsc", "0x" + "aa" * 20, "op", now)


CURVE = "0x3333333333333333333333333333333333333333"


def pons_node(now: datetime, quote_reserve: int = 2 * 10 ** 18):
    """A Pons V2 launch on a fake Robinhood node: the factory announces the
    curve, ten CurveBuy / CurveSell over the last five minutes, the curve's
    state for quotes (buy simulated through eth_call, sell from the source's
    formula on the live reserves)."""
    from yonixalpha_core.chains.evm.pons import LAUNCHED_TOKEN_TYPES, V2_EVENTS, PonsV2

    node = Node(4663)
    node.genesis_ts = int(now.timestamp()) - node.head
    lp = PonsV2(rpc_for(node, "robinhood"))
    ev = V2_EVENTS.by_name
    node.logs = [log_of(ev["TokenLaunched"], {"token": TOKEN, "curve": CURVE, "deployer": CREATOR, "pairToken": ZERO_ADDRESS,
                                              "launchConfigId": 1, "graduationThreshold": 4 * 10 ** 18}, lp.factory, 690, 0)]
    for i, px in enumerate(PRICES):
        buy, who = i not in (4, 7), f"0x{(i % 8) + 1:040x}"
        quote, tokens = px * 10 ** 13, 10 ** 22
        node.logs.append(log_of(ev["CurveBuy" if buy else "CurveSell"], (
            {"buyer": who, "recipient": who, "quoteIn": quote, "tokensOut": tokens, "fee": quote // 100, "tax": quote // 100}
            if buy else
            {"seller": who, "recipient": who, "tokensIn": tokens, "quoteOut": quote, "fee": quote // 100, "tax": quote // 100}),
            CURVE, 700 + i * 30, i + 1))
    node.on(lp.factory, "getLaunchedToken(address)", enc(
        [LAUNCHED_TOKEN_TYPES], [(TOKEN, CURVE, CREATOR, CREATOR, ZERO_ADDRESS, 4 * 10 ** 18, 10000, 200, 100, False, 0, 0, 0, 0,
                                  True)]))
    set_pons_reserves(node, quote_reserve)
    node.on(CURVE, "feeBps()", enc(["uint256"], [100]))
    node.on(CURVE, "creatorTaxBps()", enc(["uint256"], [100]))
    node.on(CURVE, "sellableTokens()", enc(["uint256"], [8 * 10 ** 26]))
    node.on(CURVE, "graduated()", enc(["bool"], [False]))
    node.on(CURVE, "readyToGraduate()", enc(["bool"], [False]))
    node.on(CURVE, "realQuoteReserve()", enc(["uint256"], [10 ** 18]))
    node.on(TOKEN, "totalSupply()", enc(["uint256"], [10 ** 27]))

    def buy_sim(p):  # the curve's own price, fee and tax taken
        value = int(p[0]["value"], 16)
        return enc(["uint256"], [value * 98 // 100 * 10 ** 27 // (2 * 10 ** 18)])

    node.on(CURVE, "buy(uint256,uint256,address)", buy_sim)
    return node, lp


def set_pons_reserves(node: Node, quote_reserve: int) -> None:
    node.on(CURVE, "getReserves()", enc(["uint256", "uint256"], [quote_reserve, 10 ** 27]))


async def test_robinhood_pons_pipeline_discovery_safety_entry_exit(session_factory, redis_client):
    """Master §79 Robinhood buy / sell: the same pipeline as BSC on a Pons V2
    curve: discovery, safety, the evidence gate, a paper entry, management,
    and a stop-loss exit at the executable sell quote, with PnL in ETH."""
    import httpx

    now = datetime.now(timezone.utc).replace(microsecond=0)
    node, lp = pons_node(now)
    explorer = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(503)))  # never the network
    w = ChainWorker("robinhood", lp.rpc, [lp], session_factory, redis_client, http=explorer)
    s, errors = evm_settings.parse({"robinhood": {"confirmations": 0}, "fresh_min_buys": 5, "fresh_min_unique_buyers": 4})
    assert not errors

    out = await w.discovery_pass(s, now)
    assert out["pons_v2"]["launches"] == 1 and out["pons_v2"]["trades"] == 10
    async with session_factory() as session:
        row = await session.get(EvmToken, ("robinhood", TOKEN))
        assert row.category == "FRESH" and row.stats["buys"] == 8 and row.launchpad == "pons_v2"

    assert await w.safety_pass(s, now) == 1
    async with session_factory() as session:
        row = await session.get(EvmToken, ("robinhood", TOKEN))
        assert row.safety_verdict == "PASS", row.safety

    assert (await w.entry_pass(s, now))["opened"] == 0  # never traded before real-chain evidence
    async with session_factory() as session:
        for c in PAPER_REQUIRED:
            await verification.record(session, "pons_v2", c, True, {"test": True}, "test", now)
        await session.commit()
    # master §11 / §70: the launch-window coordination check cannot read the
    # launch transaction yet, so the entry is NO_TRADE, never assumed safe
    assert (await w.entry_pass(s, now))["opened"] == 0
    async with session_factory() as session:
        rec = (await session.get(EvmToken, ("robinhood", TOKEN))).extra.get("entry_decision")
    assert rec["decision"] == "NO_TRADE" and [b["code"] for b in rec["blockers"]] == ["LAUNCH_COORDINATION"]
    assert "COORDINATION_DATA_UNAVAILABLE" in rec["blockers"][0]["message"]
    # the chain answers: the launch transaction (no snipe-tax exemptions in its
    # receipt), the curve's token balance, every buyer pays the snipe tax and
    # is an old wallet, the explorer has no funding data
    from yonixalpha_core.testing.evm_node import TX
    node.txs[TX] = {"from": CREATOR, "to": lp.factory, "input": "0x"}
    node.receipts[TX] = {"logs": []}
    node.on(TOKEN, "balanceOf(address)", enc(["uint256"], [10 ** 27 - 6 * 10 ** 22]))  # supply less net curve buys
    node.on(CURVE, "currentSnipeTaxBps(address)", enc(["uint256"], [9000]))
    for i in range(1, 9):
        node.nonces[f"0x{i:040x}"] = 40
    from app.worker import SAFETY_RECHECK
    async with session_factory() as session:  # the next safety re-check is due (it refreshes coordination)
        row = await session.get(EvmToken, ("robinhood", TOKEN))
        row.safety_at = now - SAFETY_RECHECK - timedelta(seconds=1)
        await session.commit()
    assert await w.safety_pass(s, now) == 1
    counts = await w.entry_pass(s, now)
    async with session_factory() as session:
        rec = (await session.get(EvmToken, ("robinhood", TOKEN))).extra.get("entry_decision")
    assert counts["opened"] == 1, rec
    assert rec["decision"] in ("EXECUTE", "REDUCE_SIZE") and rec["blockers"] == []
    assert (await w.entry_pass(s, now))["opened"] == 0  # no second entry on the same token
    async with session_factory() as session:
        p = (await session.execute(select(PaperPosition))).scalar_one()
        assert p.engine == "evm_robinhood" and p.status == "open"
        acct = await session.get(PaperAccount, p.account_id)
        assert acct.quote_currency == "ETH" and acct.cash_balance == acct.starting_balance - p.entry_cost_quote
        entry_cost = p.entry_cost_quote

    r = await w.manage_pass(now)
    assert r["managed"] == 1 and r["closed"] == 0
    set_pons_reserves(node, 2 * 10 ** 17)  # the curve's quote side collapses: the sell quote falls below the stop
    r = await w.manage_pass(now)
    assert r["closed"] == 1
    async with session_factory() as session:
        p = (await session.execute(select(PaperPosition))).scalar_one()
        assert p.status == "closed" and p.exit_reason == "stop_loss"
        assert p.realized_pnl == p.proceeds_quote - entry_cost and p.realized_pnl < 0
