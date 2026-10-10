"""Early-entry intelligence storage (2026-10-10) against Postgres and Redis:
one record per (mint, strategy) even across restarts, labels from the
held stream (and UNKNOWN when it expired, never guessed), smart-wallet
quality only from launches resolved BEFORE the decision, migrated pool
sampling with one batched RPC call, the latency timeline, late entries and
paper vs LIVE parity per signal. No network, no transaction is sent."""

import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://yonixalpha:yonixalpha_test_pw@localhost:5432/yonixalpha_test")

import pytest_asyncio  # noqa: E402
from redis.asyncio import from_url  # noqa: E402
from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from yonixalpha_core import entry_intel as ei  # noqa: E402
from yonixalpha_core import entry_parity, entry_store, entry_timing  # noqa: E402
from yonixalpha_core.db.base import Base, make_session_factory  # noqa: E402
from yonixalpha_core.db.models import (  # noqa: E402
    EntrySignal,
    ExecutionOrder,
    LaunchBuyer,
    PaperAccount,
    PaperPosition,
    PlatformSetting,
    RiskAssessment,
    Token,
    TradingCandidate,
)
from yonixalpha_core.solana import pump_stream, pumpswap  # noqa: E402
from yonixalpha_core.testing.curve_sim import T0, Curve  # noqa: E402

MINT = "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"
MINT2 = "9n4nbM75f5Ui33ZbPYXn59EwSgE8CGsHtAeTH5YFeJ9E"


@pytest_asyncio.fixture
async def redis():
    r = from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/15"), decode_responses=True)
    await r.flushdb()
    yield r
    await r.flushdb()
    await r.aclose()


@pytest_asyncio.fixture
async def sf():
    engine = create_async_engine(os.environ["DATABASE_URL"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield make_session_factory(engine)
    await engine.dispose()


async def put_stream(redis, mint: str, c: Curve, fee_bps: int = 125, created=T0) -> None:
    for t in c.trades:
        await redis.rpush(pump_stream.trades_key(mint), json.dumps([int(t.at.timestamp()), t.trader, 1 if t.is_buy else 0,
                                                                    t.sol_lamports, t.token_raw, t.virtual_sol, t.virtual_token]))
    last = c.trades[-1]
    await redis.hset(pump_stream.curve_key(mint), mapping={"vsol": last.virtual_sol, "vtok": last.virtual_token,
                                                           "updated_at": int(last.at.timestamp()), "fee_bps": fee_bps})
    await redis.hset(pump_stream.meta_key(mint), mapping={"created_at": int(created.timestamp()), "symbol": "TST",
                                                          "creator": "creator", "received_at": f"{created.timestamp() + 1.2:.3f}"})


def launch() -> Curve:
    c = Curve()
    for i in range(12):
        c.buy(1 + i, f"w{i}", 0.3)
    for i in range(10):
        c.buy(20 + i, f"x{i}", 0.6)
    return c


# --- recording ---------------------------------------------------------------------------

async def test_one_record_per_mint_and_strategy_even_after_a_restart(redis, sf):
    async with sf() as s:
        for _ in range(2):
            if await entry_store.claim(redis, ei.EARLY_ACCELERATION, MINT):
                await entry_store.record_signal(s, mint=MINT, strategy=ei.EARLY_ACCELERATION, lifecycle="FRESH",
                                                decision=ei.CANDIDATE, decided_at=T0, score=0.6, features={"a": 1})
        # a restarted worker lost its claims (Redis flushed): the unique constraint still holds
        await redis.flushdb()
        assert await entry_store.claim(redis, ei.EARLY_ACCELERATION, MINT)
        again = await entry_store.record_signal(s, mint=MINT, strategy=ei.EARLY_ACCELERATION, lifecycle="FRESH",
                                                decision=ei.CANDIDATE, decided_at=T0 + timedelta(seconds=5))
        other = await entry_store.record_signal(s, mint=MINT, strategy=ei.MOMENTUM_CONTINUATION, lifecycle="FRESH",
                                                decision=ei.CANDIDATE, decided_at=T0)
        await s.commit()
        assert again is False and other is True
        assert (await s.execute(select(func.count()).select_from(EntrySignal))).scalar_one() == 2
        assert await entry_store.record_baseline(s, redis, mint=MINT, strategy=ei.CURRENT_PROMOTE, decided_at=T0, price_raw=0.03)
        assert not await entry_store.record_baseline(s, redis, mint=MINT, strategy=ei.CURRENT_PROMOTE, decided_at=T0, price_raw=0.03)


async def test_settings_modes_and_validation(sf):
    assert entry_store.validate_update({"modes": {ei.SMART_WALLET_CONFIRMATION: "PAPER"}})
    assert entry_store.validate_update({"modes": {ei.MIGRATED_PULLBACK: "PAPER"}})
    assert entry_store.validate_update({"modes": {"NOPE": "SHADOW"}})
    assert not entry_store.validate_update({"modes": {ei.EARLY_ACCELERATION: "PAPER"}, "config": {"ea_min_trades": 8}})
    async with sf() as s:
        s.add(PlatformSetting(key=entry_store.SETTINGS_KEY, value={"modes": {ei.EARLY_ACCELERATION: "PAPER", "X": "LIVE"},
                                                                   "config": {"ea_min_trades": 9}}))
        await s.commit()
        st = await entry_store.load_settings(s)
    assert st["modes"][ei.EARLY_ACCELERATION] == "PAPER" and st["modes"][ei.MOMENTUM_CONTINUATION] == "SHADOW"
    assert st["config"].ea_min_trades == 9 and "X" not in st["modes"]


# --- labelling ---------------------------------------------------------------------------------

async def test_label_due_from_the_held_stream_and_unknown_when_expired(redis, sf):
    c = launch()
    await put_stream(redis, MINT, c)
    now = T0 + timedelta(seconds=2000)
    async with sf() as s:
        await entry_store.record_signal(s, mint=MINT, strategy=ei.EARLY_ACCELERATION, lifecycle="FRESH", decision=ei.CANDIDATE,
                                        decided_at=T0 + timedelta(seconds=15), phase=ei.EARLY_ACCEL_PHASE)
        await entry_store.record_signal(s, mint=MINT2, strategy=ei.EARLY_ACCELERATION, lifecycle="FRESH", decision=ei.CANDIDATE,
                                        decided_at=T0 + timedelta(seconds=15))
        await entry_store.record_signal(s, mint=MINT, strategy=ei.NAIVE_SAMPLE, lifecycle="FRESH", decision="BASELINE",
                                        decided_at=now - timedelta(seconds=60))  # horizon not reached: not labelled yet
        await s.commit()
        res = await entry_store.label_due(s, redis, now)
        assert res == {"labelled": 2, "unknown": 1}
        rows = {r.mint + r.strategy: r for r in (await s.execute(select(EntrySignal))).scalars()}
    ok = rows[MINT + ei.EARLY_ACCELERATION].outcome
    assert ok["latency_source"].startswith("DEFAULT") and ok["executable_return_pct"] is not None
    assert "expired" in rows[MINT2 + ei.EARLY_ACCELERATION].outcome["unknown"]
    assert rows[MINT + ei.NAIVE_SAMPLE].outcome is None


async def test_measured_latency_uses_confirmed_live_buys(redis, sf):
    async with sf() as s:
        for i in range(6):
            s.add(ExecutionOrder(mode="LIVE", side="BUY", reason="entry", mint=MINT, provider="native", route="pump",
                                 amount="0.01", amount_kind="sol", slippage_pct=Decimal(10), priority_fee_sol=Decimal("0.0001"),
                                 status="CONFIRMED", idempotency_key=f"k{i}",
                                 diagnostics={"timing": {"decision_to_confirm_ms": 2000 + 200 * i}}))
        await s.commit()
        lat, src = await entry_store.measured_latency(s, redis)
    assert lat == 2.5 and src.startswith("MEASURED")


# --- smart wallets: causal history only ----------------------------------------------------------

async def test_wallet_quality_uses_only_launches_resolved_before_the_decision(redis, sf):
    c = launch()
    t = T0 + timedelta(seconds=40)
    async with sf() as s:
        for i in range(6):  # w0: six closed, profitable launch trades resolved BEFORE t
            s.add(LaunchBuyer(mint=f"m{i}", wallet="w0", rank=1, launch_created_at=T0 - timedelta(days=1, seconds=60),
                              first_buy_at=T0 - timedelta(days=1), sol_in=Decimal("0.3"), tokens_in=Decimal(1000),
                              recorded_at=T0 - timedelta(days=1), outcome="WIN",
                              outcome_resolved_at=T0 - timedelta(hours=20 - i),
                              ledger={"covered": True, "sol_out": "0.5", "tokens_out": "1000"}))
        for i in range(6):  # w1: the same record, but resolved AFTER t: must not be visible
            s.add(LaunchBuyer(mint=f"n{i}", wallet="w1", rank=1, launch_created_at=T0, first_buy_at=T0,
                              sol_in=Decimal("0.3"), tokens_in=Decimal(1000), recorded_at=T0, outcome="WIN",
                              outcome_resolved_at=t + timedelta(minutes=5 + i),
                              ledger={"covered": True, "sol_out": "0.5", "tokens_out": "1000"}))
        for i in range(2):  # w2: too few closed trades to be proven
            s.add(LaunchBuyer(mint=f"o{i}", wallet="w2", rank=1, launch_created_at=T0, first_buy_at=T0 - timedelta(days=1),
                              sol_in=Decimal("0.3"), tokens_in=Decimal(1000), recorded_at=T0, outcome="WIN",
                              outcome_resolved_at=T0 - timedelta(hours=3),
                              ledger={"covered": True, "sol_out": "0.6", "tokens_out": "1000"}))
        await s.commit()
        ev = await entry_store.wallet_evidence(s, redis, c.trades, t)
    assert ev["status"] == "MEASURED"
    proven = [e["wallet"] for e in ev["proven_entries"]]
    assert proven == ["w0"]
    st = ev["proven_entries"][0]["stats"]
    assert st["closed_trades"] == 6 and st["median_return_pct"] > 0 and st["windows"]["24h"]["n"] == 6
    assert ev["proven_exits"] == [] and ev["coordinated"] is False


async def test_missing_wallet_history_is_unknown(redis, sf):
    async with sf() as s:
        ev = await entry_store.wallet_evidence(s, redis, launch().trades, T0 + timedelta(seconds=40))
    assert ev["status"] == "UNKNOWN" and "history" in ev["reason"]


# --- migrated pools -----------------------------------------------------------------------------

class FakeRpc:
    def __init__(self, pool_data: str, base: int, quote: int):
        self.calls: list[str] = []
        self.pool_data, self.base, self.quote = pool_data, base, quote

    async def call(self, method, params=None, priority="normal"):
        self.calls.append(method)
        assert priority == "background"
        if method == "getAccountInfo":
            return {"value": {"owner": pumpswap.PUMP_AMM_PROGRAM, "data": [self.pool_data, "base64"]}}
        vals = []
        for i in range(0, len(params[0]), 2):
            vals += [{"data": {"parsed": {"info": {"tokenAmount": {"amount": str(self.base)}}}}},
                     {"data": {"parsed": {"info": {"tokenAmount": {"amount": str(self.quote)}}}}}]
        return {"value": vals}


def _pool_b64(mint: str) -> str:
    import base64

    from solders.pubkey import Pubkey

    wsol = pumpswap.WSOL_MINT
    body = (bytes([1]) + (0).to_bytes(2, "little") + bytes(Pubkey.from_string(MINT2)) + bytes(Pubkey.from_string(mint))
            + bytes(Pubkey.from_string(wsol)) + bytes(Pubkey.from_string(MINT2)) + bytes(Pubkey.from_string(MINT2))
            + bytes(Pubkey.from_string(MINT2)) + (1).to_bytes(8, "little"))
    return base64.b64encode(pumpswap.POOL_DISC + body).decode()


async def test_migrated_sampling_batches_vaults_and_records_variants(redis, sf):
    now = datetime.now(timezone.utc)
    await redis.zadd(pump_stream.MIGRATED, {MINT: int(now.timestamp()) - 300, MINT2: int(now.timestamp()) - 200})
    rpc = FakeRpc(_pool_b64(MINT), 1_000_000, 85_000_000_000)
    res = await entry_store.sample_migrated(redis, rpc, now)
    assert res["sampled"] == 2 and rpc.calls.count("getMultipleAccounts") == 1
    res2 = await entry_store.sample_migrated(redis, rpc, now + timedelta(seconds=30))
    assert res2["sampled"] == 2 and rpc.calls.count("getAccountInfo") == 2  # vaults cached after the first pass
    async with sf() as s:
        n = await entry_store.record_migrated_variants(s, redis, now + timedelta(seconds=31), {})
        rows = (await s.execute(select(EntrySignal.strategy).where(EntrySignal.mint == MINT))).scalars().all()
    assert n >= 4 and ei.MIGRATED_IMMEDIATE in rows and ei.MIGRATED_NO_TRADE in rows


# --- timeline, late entries, parity ----------------------------------------------------------------

async def _entered(s, mode: str, mint: str = MINT):
    tok = Token(mint_address=mint, first_seen_source="pump_stream")
    s.add(tok)
    acct = PaperAccount(name=f"a{mode}", quote_currency="SOL", starting_balance=10, cash_balance=10, reset_at=T0)
    s.add(acct)
    await s.flush()
    cand = TradingCandidate(token_id=tok.id, engine="discovery", state="entered", state_history=[],
                            detail={"source": "pump_stream", "mint": mint})
    s.add(cand)
    await s.flush()
    cand.created_at = T0 + timedelta(seconds=12)
    waits = [(T0 + timedelta(seconds=14), False, [{"code": "VOLATILITY_UNAVAILABLE", "action": "NO_TRADE"}], "NO_TRADE"),
             (T0 + timedelta(seconds=44), False, [{"code": "SIGNAL_NOT_QUALIFIED", "action": "WAIT"}], "WAIT"),
             (T0 + timedelta(seconds=74), True, [], "EXECUTE")]
    a_ids = []
    for i, (at, ex, findings, dec) in enumerate(waits):
        a = RiskAssessment(idempotency_key=f"{mode}{mint}{i}", candidate_id=cand.id, engine="solana_fresh", strategy="s",
                           asset_id=mint, decision=dec, status_label=dec, executable=ex, execution_target=mode,
                           overall_risk="LOW", risk_engine_version="1", evaluated_at=at,
                           assessment={"decision": dec, "executable": ex, "findings": findings,
                                       "plan": {"entry_price": "0.0000002", "position_size": {"value": "0.05"}},
                                       "inputs_snapshot": {"entry_quality": {"indicators": ["buyer_stall"], "evidence": ["x"]}}})
        s.add(a)
        await s.flush()
        a_ids.append(a.id)
    pos = PaperPosition(symbol="TST", provider="paper", side="LONG", entry_price=Decimal("0.00000021"), quantity=Decimal(1),
                        stop_loss=Decimal("0.0000001"), take_profit=[], status="closed", entry_at=T0 + timedelta(seconds=75),
                        exit_at=T0 + timedelta(seconds=300), account_id=acct.id, engine="solana_fresh", asset_id=mint,
                        candidate_id=cand.id, assessment_id=a_ids[-1], execution_mode=mode, entry_cost_quote=Decimal("0.05"),
                        fees_paid_quote=Decimal("0.0005"), realized_pnl=Decimal("-0.005"), realized_pnl_pct=Decimal("-0.1"),
                        exit_reason="stop_loss", plan={"venue": {"decimals": 6}})
    s.add(pos)
    await s.flush()
    if mode == "LIVE":
        s.add(ExecutionOrder(position_id=pos.id, assessment_id=a_ids[-1], mode="LIVE", side="BUY", reason="entry", mint=mint,
                             provider="native", route="pump", amount="0.05", amount_kind="sol", slippage_pct=Decimal(10),
                             priority_fee_sol=Decimal("0.0001"), status="CONFIRMED", idempotency_key=f"o{mint}",
                             created_at=T0 + timedelta(seconds=74.5),
                             diagnostics={"timing": {"decision_to_confirm_ms": 3100, "timestamps": {
                                 "worker_pickup_at": (T0 + timedelta(seconds=74.7)).isoformat(),
                                 "quote_at": (T0 + timedelta(seconds=74.9)).isoformat(),
                                 "signed_at": (T0 + timedelta(seconds=75.2)).isoformat(),
                                 "submitted_at": (T0 + timedelta(seconds=75.5)).isoformat(),
                                 "first_seen_at": (T0 + timedelta(seconds=76.6)).isoformat(),
                                 "confirmed_at": (T0 + timedelta(seconds=77.4)).isoformat()}},
                                 "price": {"classification": "MARKET_MOVEMENT"}}))
    await s.commit()
    return cand, pos


async def test_candidate_timeline_latency_and_waiting_split(redis, sf):
    await entry_timing.mark(redis, MINT, "launch_observed_at", T0.timestamp())
    await entry_timing.mark(redis, MINT, "event_received_at", T0.timestamp() + 1.5)
    await entry_timing.mark(redis, MINT, "event_received_at", T0.timestamp() + 99)  # first write wins
    async with sf() as s:
        cand, _ = await _entered(s, "LIVE")
        tl = await entry_timing.candidate_timeline(s, redis, cand)
        summary = await entry_timing.latency_summary(s, redis, T0 - timedelta(hours=1))
    lat = tl["latency"]
    assert lat["detection_latency_s"] == 1.5 and lat["promotion_latency_s"] == 10.5
    assert lat["gate_wait_s"] == 60.0 and lat["confirmation_latency_s"] == 1.9
    assert tl["waiting"]["seconds"]["data"] == 30.0 and tl["waiting"]["seconds"]["strategy"] == 30.0
    assert summary["entered"] == 1 and summary["latency_seconds"]["gate_wait_s"]["median"] == 60.0


async def test_late_entries_from_the_stream_and_from_the_database(redis, sf):
    c = Curve()
    for i in range(40):
        c.buy(1 + i * 2, f"p{i}", 0.5)
    await put_stream(redis, MINT, c)
    await entry_timing.mark(redis, MINT, "event_received_at", T0.timestamp() + 1)
    async with sf() as s:
        await _entered(s, "PAPER")
        await _entered(s, "LIVE", MINT2)  # no stream for MINT2: database fallback
        rep = await entry_timing.late_entries(s, redis, T0 - timedelta(hours=1))
    by = {r["mint"]: r for r in rep["rows"]}
    assert by[MINT]["prices_from"] == "stream" and by[MINT]["price_change_detection_to_entry_pct"] > 50
    assert by[MINT]["decelerating_at_entry"] is True and rep["late_by_50pct_or_more"] >= 1
    assert by[MINT2]["prices_from"] is None or "database" in by[MINT2]["prices_from"]


async def test_parity_per_signal_names_the_differences(redis, sf):
    c = Curve()
    for i in range(30):
        c.buy(70 + i * 0.3, f"q{i}", 0.4)  # the price runs right after the paper entry at T0+75
    await put_stream(redis, MINT, c)
    async with sf() as s:
        await _entered(s, "PAPER")
        await _entered(s, "LIVE", MINT2)
        rep = await entry_parity.per_signal(s, redis, T0 - timedelta(hours=1), 3.0, "test")
    by = {r["mode"]: r for r in rep["rows"]}
    assert by["PAPER"]["est_live_displacement_pct"] is not None and by["PAPER"]["est_live_displacement_pct"] > 2
    assert any("LIVE order would have landed" in x for x in by["PAPER"]["difference_reasons"])
    assert by["LIVE"]["live_price_classification"] == "MARKET_MOVEMENT"
    assert by["LIVE"]["entry_displacement_pct"] == 5.0
    assert rep["paper"]["entries"] == 1 and rep["live"]["entries"] == 1


async def test_paper_results_per_strategy(sf):
    async with sf() as s:
        cand, pos = await _entered(s, "PAPER")
        cand.detail = {**cand.detail, "entry_strategy": ei.EARLY_ACCELERATION}
        await s.commit()
        res = await entry_store.paper_results(s)
    assert res[ei.EARLY_ACCELERATION]["closed"] == 1 and res[ei.EARLY_ACCELERATION]["median_pnl_pct"] == -10.0
