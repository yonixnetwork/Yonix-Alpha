"""End-to-end scenarios for the fresh-token observation lifecycle, run
through the real code paths on byte-exact synthetic Pump.fun / PumpSwap data
(no network): stream ingest -> discovery funnel (observation window) ->
safety gate -> paper entry -> exit intelligence / stops -> recorded result.

The discovery funnel and decision-engine gate_eval are loaded by path: all
services name their package `app`, and neither module imports anything
service-local.
"""

import importlib.util
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import select

from yonixalpha_core.db.models import PaperPosition, TokenObservation, TradeTimelineEvent, TradingCandidate
from yonixalpha_core.safety.settings import default_settings_for
from yonixalpha_core.solana import pump_stream, pumpswap
from yonixalpha_core.solana.assembler import Sources
from yonixalpha_core.state_machine import CandidateState
from yonixalpha_core.testing.pump import CREATOR, MINT, SUPPLY, Curve, FakeRpc, create_event, logs_of, wallet
from yonixalpha_core.testing.pumpswap import FakePoolRpc, seed_sol_usd, trade_event

from app.gate_manage import manage_gate_positions

SERVICES = Path(__file__).resolve().parents[2]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gate_eval = _load("scen_gate_eval", SERVICES / "decision-engine" / "app" / "gate_eval.py")
funnel = _load("scen_funnel", SERVICES / "engine-solana-discovery" / "app" / "funnel.py")

NOW = datetime.now(timezone.utc).replace(microsecond=0)
FRESH = default_settings_for("solana_fresh")
PAPER_ENV = SimpleNamespace(TRADING_ENABLED=False, LIVE_TRADING_ENABLED=False, PAPER_TRADING=True, TELEGRAM_BOT_TOKEN=None,
                            TELEGRAM_CHAT_ID=None)
SOL = 1_000_000_000


async def ingest(redis, curve: Curve, trades: list[tuple[datetime, int, int, bool]], received_at: datetime) -> None:
    """trades: (time, wallet index, lamports, is_buy)."""
    evs = [curve.trade(wallet(w), at, sol, buy) for at, w, sol, buy in trades]
    await pump_stream.ingest_logs(redis, logs_of(*evs), f"sig-{received_at.timestamp()}-{len(evs)}", received_at)


async def one(session_factory, model, *where):
    async with session_factory() as s:
        return (await s.execute(select(model).where(*where))).scalar_one()


async def timeline(session_factory, **by) -> list[str]:
    async with session_factory() as s:
        q = select(TradeTimelineEvent.event_type).order_by(TradeTimelineEvent.occurred_at)
        for k, v in by.items():
            q = q.where(getattr(TradeTimelineEvent, k) == v)
        return list((await s.execute(q)).scalars())


async def test_good_fresh_token_with_no_dex_pool_is_observed_entered_and_exited_on_deterioration(session_factory, redis_client):
    # 1-2. A pump.fun launch, 12 s old: ~4 SOL on the bonding curve, no DEX
    # pool at all — far below the 20 SOL pool minimum that used to block it.
    t0 = NOW - timedelta(seconds=12)
    curve = Curve()
    await pump_stream.ingest_logs(redis_client, logs_of(create_event(t0)), "sigcreate", t0)
    # 5-8. Steady organic buying that grows through the window: 5 buyers in
    # the first half, 10 in the second (same SOL per second, more wallets).
    first = [(t0 + timedelta(seconds=1 + i), i, 100_000_000, True) for i in range(5)]
    second = [(t0 + timedelta(seconds=6 + i // 2), 5 + i, 50_000_000, True) for i in range(10)]
    await ingest(redis_client, curve, first + second, NOW - timedelta(seconds=1))

    # 3-4. Not rejected for missing liquidity: FRESH_OBSERVING, then promoted.
    counts = await funnel.run_funnel(redis_client, session_factory, FRESH, NOW)
    assert counts["promoted"] == 1, counts
    obs = await one(session_factory, TokenObservation, TokenObservation.mint == MINT)
    assert obs.outcome == "PROMOTE" and obs.trend == "INCREASING"
    assert obs.report["metrics"]["liquidity_state"].startswith("NO DEX POOL YET")
    assert [cp["label"] for cp in obs.report["checkpoints"]] == ["T0", "T+5s", "T+10s"]
    assert obs.report["halves"][0]["new_buyers"] == 5 and obs.report["halves"][1]["new_buyers"] == 10
    cand = await one(session_factory, TradingCandidate, TradingCandidate.engine == "discovery")

    # 9. First gate pass at 12 s old: too few price points to size a stop
    # from measured volatility, so no trade yet — but the candidate stays
    # under analysis (not rejected) and the reason is recorded.
    async with session_factory() as s:
        c = await s.get(TradingCandidate, cand.id)
        early = await gate_eval.evaluate_with_gate(s, redis_client, PAPER_ENV, Sources(redis_client, FakeRpc(curve)), c, NOW)
    # 15 trades in 10 s: volatility is measurable only trade-to-trade, so it is
    # LOW_CONFIDENCE — never auto-traded (approval required), never invented.
    assert not early.executable and early.decision.value in ("NO_TRADE", "WAIT", "REQUIRE_MANUAL_APPROVAL"), early.reasons
    assert "VOLATILITY_LOW_CONFIDENCE" in {f.code for f in early.findings}, [f.code for f in early.findings]
    assert "INSUFFICIENT_LIQUIDITY" not in {f.code for f in early.findings}
    c = await one(session_factory, TradingCandidate, TradingCandidate.id == cand.id)
    assert c.state == CandidateState.ANALYZING.value

    # 10-12. Buying continues steadily for ~40 s; the next pass measures volatility
    # (10-second returns), sizes the position and plans SL / TPs / trailing.
    t_entry = NOW + timedelta(seconds=40)
    more = [(t0 + timedelta(seconds=11 + i), 15 + i, 100_000_000, True) for i in range(40)]
    await ingest(redis_client, curve, more, t_entry - timedelta(seconds=1))
    await redis_client.delete(f"yx:gate:pace:{cand.id}")
    async with session_factory() as s:
        c = await s.get(TradingCandidate, cand.id)
        a = await gate_eval.evaluate_with_gate(s, redis_client, PAPER_ENV, Sources(redis_client, FakeRpc(curve)), c, t_entry)
    codes = {f.code for f in a.findings}
    assert "10-second returns" in a.inputs_snapshot["volatility_source"]
    assert a.decision.value == "EXECUTE", a.reasons
    assert "INSUFFICIENT_LIQUIDITY" not in codes and "WAITING_FOR_LIQUIDITY" not in codes
    bc = next(f for f in a.findings if f.code == "BONDING_CURVE_MARKET")
    assert "NO DEX POOL YET" in bc.message and "simulated on the curve" in bc.message
    assert codes & {"ACTIVITY_INCREASING", "ACTIVITY_STABLE"} and "ACTIVITY_DETERIORATING" not in codes
    assert a.plan.complete and a.plan.stop_loss.value > 0 and len(a.plan.take_profits) == 3 and a.plan.trailing is not None
    pos = await one(session_factory, PaperPosition, PaperPosition.candidate_id == cand.id)
    assert pos.status == "open" and pos.lifecycle == "FRESH" and pos.execution_route == "pump"
    initial = pos.initial_quantity

    # 13-16. Monitored; buyers stop and sellers take over: exit intelligence
    # sees the deterioration and reduces the position (partial exit).
    t1 = t_entry + timedelta(seconds=130)
    sells = [(t1 - timedelta(seconds=10 - i), i, 100_000_000, False) for i in range(6)]
    await ingest(redis_client, curve, sells, t1 - timedelta(seconds=1))
    await manage_gate_positions(session_factory, redis_client, None, t1, None, PAPER_ENV)
    pos = await one(session_factory, PaperPosition, PaperPosition.id == pos.id)
    assert pos.remaining_quantity < initial and pos.proceeds_quote > 0, (pos.status, pos.exit_reason)
    events = await timeline(session_factory, position_id=pos.id)
    assert "exit_intelligence.reduce" in events, events

    # Then the price collapses: the emergency exit (or the stop) closes the rest.
    t2 = t1 + timedelta(seconds=30)
    dump = [(t2 - timedelta(seconds=5), 60 + i, 450_000_000, False) for i in range(8)]
    await ingest(redis_client, curve, dump, t2 - timedelta(seconds=1))
    await manage_gate_positions(session_factory, redis_client, None, t2, None, PAPER_ENV)
    pos = await one(session_factory, PaperPosition, PaperPosition.id == pos.id)
    # 17. The actual result is recorded.
    assert pos.status == "closed" and pos.remaining_quantity == 0, pos.exit_reason
    assert pos.exit_reason in ("exit_intel_exit_now", "stop_loss", "exit_intel_exit", "trailing_stop")
    assert pos.realized_pnl == pos.proceeds_quote - pos.entry_cost_quote
    c = await one(session_factory, TradingCandidate, TradingCandidate.id == cand.id)
    assert c.state == CandidateState.CLOSED.value


class ConcentratedRpc(FakeRpc):
    """The creator's own wallet holds half the supply."""

    async def call(self, method, params=None):
        if method == "getTokenLargestAccounts":
            return {"value": [{"address": "curveATA", "amount": str(SUPPLY * 40 // 100)},
                              {"address": "creatorATA", "amount": str(SUPPLY * 50 // 100)},
                              {"address": "ta0", "amount": str(SUPPLY // 100)}]}
        if method == "getMultipleAccounts":
            from yonixalpha_core.testing.pump import CURVE

            owners = [CURVE, CREATOR, wallet(0)]
            return {"value": [{"data": {"parsed": {"info": {"owner": o}}}} for o in owners]}
        return await super().call(method, params)


async def test_bad_fresh_token_is_not_traded_and_its_monitoring_expires(session_factory, redis_client):
    # Low activity and a creator that sells.
    t0 = NOW - timedelta(seconds=12)
    curve = Curve()
    await pump_stream.ingest_logs(redis_client, logs_of(create_event(t0)), "sigcreate", t0)
    evs = [curve.trade(wallet(0), t0 + timedelta(seconds=2), 50_000_000, True),
           curve.trade(CREATOR, t0 + timedelta(seconds=3), 40_000_000, True),
           curve.trade(wallet(1), t0 + timedelta(seconds=7), 60_000_000, True),
           curve.trade(CREATOR, t0 + timedelta(seconds=8), 20_000_000, False)]
    await pump_stream.ingest_logs(redis_client, logs_of(*evs), "sigbad", NOW - timedelta(seconds=1))

    counts = await funnel.run_funnel(redis_client, session_factory, FRESH, NOW)
    assert counts["promoted"] == 0 and counts["continue_monitoring"] == 1, counts
    report = json.loads(await redis_client.get(pump_stream.obs_report_key(MINT)))
    assert "creator wallet sold" in report["negative"] and report["metrics"]["creator_sold"] is True

    # Monitoring is bounded: with no further activity the token expires.
    later = await funnel.run_funnel(redis_client, session_factory, FRESH, NOW + timedelta(seconds=FRESH.fresh_inactivity_timeout_seconds + 20))
    assert later["expired"] == 1
    obs = await one(session_factory, TokenObservation, TokenObservation.mint == MINT)
    assert obs.outcome == "NO_TRADE" and "inactive" in obs.reasons[-1]
    async with session_factory() as s:
        assert (await s.execute(select(TradingCandidate))).scalars().all() == []

    # Had it reached the gate, the gate would refuse it too, with the actual
    # reasons (not "risk too high"): the creator's own wallet holds 50%.
    from yonixalpha_core.solana.assembler import Controls, assemble_fresh
    from yonixalpha_core.safety.gate import assess
    from yonixalpha_core.testing.pump import empty_account

    inp, _ = await assemble_fresh(Sources(redis_client, ConcentratedRpc(curve)), MINT, NOW, Controls(FRESH, empty_account()))
    a = assess(inp, FRESH)
    assert not a.executable and a.decision.value in ("REJECT", "NO_TRADE")
    top1 = next(f for f in a.findings if f.code == "TOP1_CRITICAL")
    assert top1.message.startswith("TOP HOLDER (creator wallet)") and "50.0%" in top1.message
    assert {"CREATOR_CONCENTRATION", "CREATOR_SELLING"} <= {f.code for f in a.findings}


BASE, QUOTE = 700_000_000_000_000, 95 * SOL


def busy_pool(buys: int, sells: int, end: datetime, quote: int = QUOTE) -> FakePoolRpc:
    """A busy pool: a trade every 2 s, so its last 25 trades span under a
    minute — the case the old 'pool age' (oldest recent trade) got wrong."""
    pool = pumpswap.canonical_pool(MINT)
    out, base, q = [], BASE, quote
    for i in range(buys + sells):
        buy = i < buys
        sol = 300_000_000
        tok = base * sol // (q + sol)
        base, q = (base - tok, q + sol) if buy else (base + tok, q - sol)
        at = end - timedelta(seconds=2 * (buys + sells - i))
        out.append((f"b{end.timestamp()}-{i}", trade_event(pool, wallet(20 + i), at, buy, tok, sol, base, q)))
    return FakePoolRpc(MINT, BASE, quote, list(reversed(out)), inner=FakeRpc(None))


async def test_migrated_token_is_detected_entered_and_exited_when_volume_collapses(session_factory, redis_client):
    # Pump.fun token -> migration detected by the stream 10 min ago.
    launched = NOW - timedelta(minutes=40)
    await pump_stream.ingest_logs(redis_client, logs_of(create_event(launched)), "sigcreate", launched)
    migrated_at = int((NOW - timedelta(minutes=10)).timestamp())
    await redis_client.hset(pump_stream.curve_key(MINT), mapping={"vsol": 1, "vtok": 1, "updated_at": migrated_at, "complete": 1,
                                                                  "pool": pumpswap.canonical_pool(MINT), "migrated_at": migrated_at})
    await redis_client.zadd(pump_stream.MIGRATED, {MINT: migrated_at})
    counts = await funnel.run_funnel(redis_client, session_factory, FRESH, NOW)
    assert counts["migrations"] == 1
    cand = await one(session_factory, TradingCandidate, TradingCandidate.engine == "migration")

    # PumpSwap pool verified, liquidity (95 SOL = $14,250 usable at $150,
    # above the $10,000 minimum) and volume verified, buyers increasing, risk
    # acceptable -> automatic (paper) entry.
    await seed_sol_usd(redis_client, NOW)
    async with session_factory() as s:
        c = await s.get(TradingCandidate, cand.id)
        a = await gate_eval.evaluate_with_gate(s, redis_client, PAPER_ENV, Sources(redis_client, busy_pool(20, 5, NOW)), c, NOW)
    assert a.decision.value == "EXECUTE", a.reasons
    assert a.reports["migrated_liquidity"]["decision"] == "PASS"
    assert a.inputs_snapshot["pool"]["verified"] is True
    assert a.inputs_snapshot["pool_age_source"] == "migration event timestamp"
    assert a.inputs_snapshot["features"]["age_seconds"].startswith("600")
    assert not any("younger than 2 minutes" in r for r in a.reasons)
    pos = await one(session_factory, PaperPosition, PaperPosition.candidate_id == cand.id)
    assert pos.lifecycle == "MIGRATED" and pos.execution_route == "pump-amm"

    # Volume collapses and the pool drains: exit analysis closes the position.
    t1 = NOW + timedelta(seconds=90)
    await manage_gate_positions(session_factory, redis_client, None, t1, None, PAPER_ENV, busy_pool(0, 25, t1, QUOTE * 55 // 100))
    pos = await one(session_factory, PaperPosition, PaperPosition.id == pos.id)
    assert pos.status == "closed" and pos.remaining_quantity == 0
    assert pos.exit_reason in ("exit_intel_exit_now", "exit_intel_exit", "stop_loss")
    assert pos.realized_pnl == pos.proceeds_quote - pos.entry_cost_quote and pos.realized_pnl < 0


async def test_fresh_candidate_whose_token_migrates_is_handed_over_not_rejected(session_factory, redis_client):
    t0 = NOW - timedelta(seconds=12)
    curve = Curve()
    await pump_stream.ingest_logs(redis_client, logs_of(create_event(t0)), "sigcreate", t0)
    await ingest(redis_client, curve, [(t0 + timedelta(seconds=1 + i), i, 300_000_000, True) for i in range(14)], NOW)
    await funnel.run_funnel(redis_client, session_factory, FRESH, NOW)
    cand = await one(session_factory, TradingCandidate, TradingCandidate.engine == "discovery")
    await redis_client.hset(pump_stream.curve_key(MINT), mapping={"complete": 1, "pool": "POOLX", "migrated_at": int(NOW.timestamp())})
    async with session_factory() as s:
        c = await s.get(TradingCandidate, cand.id)
        assert await gate_eval.evaluate_with_gate(s, redis_client, PAPER_ENV, Sources(redis_client, FakeRpc(curve)), c, NOW) is None
    c = await one(session_factory, TradingCandidate, TradingCandidate.id == cand.id)
    assert c.state == CandidateState.MIGRATED.value and "MIGRATED_ANALYSIS" in c.state_history[-1]["reason"]
    assert "migration_detected" in await timeline(session_factory, candidate_id=cand.id)
    assert Decimal(0) == Decimal(0)
