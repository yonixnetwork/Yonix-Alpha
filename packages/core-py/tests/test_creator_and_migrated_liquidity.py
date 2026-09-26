"""Creator (developer) history, the migrated-token USD liquidity minimum and
the token-name filters.

The ten required cases are marked REQUIRED-n. They run through the real
assemblers (stream in Redis, RPC faked at the JSON-RPC boundary) and the
real safety gate; the dashboard case goes through the versioned settings
store the dashboard writes to."""

import json
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://yonixalpha:yonixalpha_test_pw@localhost:5432/yonixalpha_test")

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from redis.asyncio import from_url  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from yonixalpha_core.db import models  # noqa: F401,E402
from yonixalpha_core.db.base import Base, make_session_factory  # noqa: E402
from yonixalpha_core.safety import store  # noqa: E402
from yonixalpha_core.safety.gate import assess  # noqa: E402
from yonixalpha_core.safety.models import FinalDecision, StrategyMode  # noqa: E402
from yonixalpha_core.safety.settings import SafetySettings, default_settings_for, settings_from_dict, validate  # noqa: E402
from yonixalpha_core.solana import creator_history, pump_stream, pumpswap, sol_price  # noqa: E402
from yonixalpha_core.solana.assembler import Controls, Sources, assemble_fresh, assemble_migrated  # noqa: E402
from yonixalpha_core.solana.codec import b58encode  # noqa: E402
from yonixalpha_core.solana.market_data import PoolData, QuoteResult  # noqa: E402
from yonixalpha_core.solana.pumpfun import MIGRATION_EVENT  # noqa: E402
from yonixalpha_core.testing.pump import (  # noqa: E402
    CREATOR,
    CURVE,
    MINT,
    Curve,
    FakeRpc,
    create_event,
    empty_account,
    i64,
    logs_of,
    pk,
    program_accounts,
    seed_healthy_launch,
    u64,
    wallet,
)
from yonixalpha_core.testing.pumpswap import FakePoolRpc, seed_sol_usd, trade_history  # noqa: E402

from tests.test_safety_gate import healthy  # noqa: E402

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
FRESH = default_settings_for("solana_fresh")
MIGRATED = default_settings_for("solana_migration")


@pytest_asyncio.fixture
async def redis():
    r = from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/9"), decode_responses=True)
    await r.flushdb()
    yield r
    await r.flushdb()
    await r.aclose()


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(os.environ["DATABASE_URL"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with make_session_factory(engine)() as session:
        yield session
    await engine.dispose()


def codes(a):
    return {f.code for f in a.findings}


def finding(a, code):
    return next(f for f in a.findings if f.code == code)


async def fresh(redis, creator_tokens: int, settings: SafetySettings = FRESH, **rpc_args):
    curve = await seed_healthy_launch(redis, NOW)
    inp, ev = await assemble_fresh(Sources(redis, FakeRpc(curve, creator_tokens=creator_tokens, **rpc_args)), MINT, NOW,
                                   Controls(settings, empty_account()))
    return inp, ev, assess(inp, settings)


# --- creator history ---------------------------------------------------------


async def test_required_1_ten_launches_pass(redis):
    inp, ev, a = await fresh(redis, 10)
    assert inp.creator_history["tokens_created"] == 10 and inp.creator_history["previous_launches"] == 9
    assert inp.creator_history["status"] == "VERIFIED"
    r = a.reports["creator_history"]
    assert r["result"] == "PASS" and r["reason"] == "CREATOR_HISTORY_OK" and r["minimum"] == 5
    msg = finding(a, "CREATOR_HISTORY_OK").message
    assert "CREATOR TOKENS CREATED: 10" in msg and "CREATOR HISTORY: PASS" in msg
    assert a.decision == FinalDecision.EXECUTE, a.reasons


async def test_required_2_exactly_five_launches_pass(redis):
    inp, _, a = await fresh(redis, 5)
    assert inp.creator_history["tokens_created"] == 5
    assert a.reports["creator_history"]["result"] == "PASS"
    assert a.decision == FinalDecision.EXECUTE, a.reasons


@pytest.mark.parametrize("action, decision, result", [
    ("WARN", FinalDecision.EXECUTE, "WARNING"),
    ("REDUCE_SIZE", FinalDecision.REDUCE_SIZE, "WARNING"),
    ("REQUIRE_MANUAL_APPROVAL", FinalDecision.REQUIRE_MANUAL_APPROVAL, "WARNING"),
    ("REJECT", FinalDecision.REJECT, "REJECT"),
])
async def test_required_3_four_launches_apply_the_configured_action(redis, action, decision, result):
    settings = replace(FRESH, creator_below_threshold_action=action)
    inp, _, a = await fresh(redis, 4, settings)
    assert inp.creator_history["tokens_created"] == 4
    r = a.reports["creator_history"]
    assert r["result"] == result and r["action"] == action and r["reason"] == "CREATOR_HISTORY_BELOW_THRESHOLD"
    f = finding(a, "CREATOR_HISTORY_BELOW_THRESHOLD")
    assert f.action == decision
    assert (f"Creator Tokens Created: 4 · Minimum: 5 · Action: {action} · Reason: CREATOR_HISTORY_BELOW_THRESHOLD"
            in f.message)
    assert f"CREATOR HISTORY: {result}" in f.message
    assert a.decision == decision, a.reasons


async def test_required_3_manual_approval_in_auto_mode_is_no_trade(redis):
    # AUTO has no approval step: the configured approval becomes a no-trade.
    settings = replace(FRESH, creator_below_threshold_action="REQUIRE_MANUAL_APPROVAL")
    curve = await seed_healthy_launch(redis, NOW)
    inp, _ = await assemble_fresh(Sources(redis, FakeRpc(curve, creator_tokens=4)), MINT, NOW,
                                  Controls(settings, empty_account(), strategy_mode=StrategyMode.AUTO))
    a = assess(inp, settings)
    assert a.decision == FinalDecision.NO_TRADE and "AUTO_NO_APPROVAL" in codes(a)


async def test_required_4_unavailable_history_is_unknown_never_invented(redis):
    inp, ev, a = await fresh(redis, 10, fail={"getProgramAccounts", "getProgramAccountsV2"})
    h = inp.creator_history
    # The only evidence left is the stream, which saw this one launch: a
    # lower bound of 1, not a count.
    assert h["status"] == "LOWER_BOUND" and h["tokens_created"] == 1 and h["source"] == creator_history.SOURCE_STREAM
    assert "creator curve query failed" in h["error"] and "getProgramAccountsV2" in h["error"]
    r = a.reports["creator_history"]
    assert r["result"] == "UNKNOWN" and r["tokens_created"] is None and r["reason"] == "CREATOR_HISTORY_UNKNOWN"
    f = finding(a, "CREATOR_HISTORY_UNKNOWN")
    assert "CREATOR TOKENS CREATED: UNKNOWN" in f.message and "CREATOR HISTORY: UNKNOWN" in f.message
    assert f.action == FinalDecision.EXECUTE  # default unknown action: WARN
    assert "CREATOR TOKENS CREATED: 1" not in f.message  # a lower bound below the minimum is never shown as the count


async def test_unknown_history_action_is_configurable(redis):
    settings = replace(FRESH, creator_history_unknown_action="REJECT")
    _, _, a = await fresh(redis, 10, settings, fail={"getProgramAccounts", "getProgramAccountsV2"})
    assert a.decision == FinalDecision.REJECT and finding(a, "CREATOR_HISTORY_UNKNOWN").hard_block


async def test_stream_lower_bound_passes_only_when_it_already_meets_the_minimum(redis):
    # The RPC refuses the query, but the stream itself saw this creator launch 5 tokens.
    for i in range(4):
        other = b58encode(bytes([30 + i]) * 32)
        await pump_stream.ingest_logs(redis, logs_of(create_event(NOW - timedelta(hours=2 + i), mint=other, symbol=f"P{i}")),
                                      f"c{i}", NOW - timedelta(hours=2 + i))
    inp, _, a = await fresh(redis, 10, fail={"getProgramAccounts", "getProgramAccountsV2"})
    assert inp.creator_history["status"] == "LOWER_BOUND" and inp.creator_history["tokens_created"] == 5
    r = a.reports["creator_history"]
    assert r["result"] == "PASS" and r["tokens_created_display"] == "at least 5"


async def test_creator_history_off_skips_the_query(redis):
    settings = replace(FRESH, creator_history_check=False)
    curve = await seed_healthy_launch(redis, NOW)
    rpc = FakeRpc(curve, creator_tokens=1)
    inp, _ = await assemble_fresh(Sources(redis, rpc), MINT, NOW, Controls(settings, empty_account()))
    assert not {"getProgramAccounts", "getProgramAccountsV2"} & set(rpc.calls) and inp.creator_history is None
    assert not {c for c in codes(assess(inp, settings)) if c.startswith("CREATOR_HISTORY")}


async def test_serial_launcher_ceiling_needs_approval(redis):
    settings = replace(FRESH, max_creator_tokens_created=50)
    _, _, a = await fresh(redis, 80, settings)
    f = finding(a, "CREATOR_SERIAL_LAUNCHER")
    assert f.action == FinalDecision.REQUIRE_MANUAL_APPROVAL and "CREATOR TOKENS CREATED: 80" in f.message


async def test_creator_history_does_not_replace_other_checks(redis):
    # A long, clean creator history never lifts another block: an active mint
    # authority still rejects.
    curve = await seed_healthy_launch(redis, NOW)
    inp, _ = await assemble_fresh(Sources(redis, FakeRpc(curve, creator_tokens=40, mint_authority=CREATOR)), MINT, NOW,
                                  Controls(FRESH, empty_account()))
    a = assess(inp, FRESH)
    assert "CREATOR_HISTORY_OK" in codes(a) and a.decision == FinalDecision.REJECT and "MINT_AUTHORITY" in codes(a)
    # ... nor does it lift holder concentration.
    inp2 = replace(healthy(), creator_history={"status": "VERIFIED", "tokens_created": 40, "creator": CREATOR},
                   holders=replace(healthy().holders, top1_share=Decimal("0.5")))
    a = assess(inp2, SafetySettings())
    assert "CREATOR_HISTORY_OK" in codes(a) and "TOP1_CRITICAL" in codes(a) and a.decision == FinalDecision.REJECT


# --- creator history module --------------------------------------------------


class RecordingRpc:
    """A plain RPC (no getProgramAccountsV2): answers getProgramAccounts."""

    def __init__(self, rows):
        self.rows, self.calls = rows, []

    async def call(self, method, params=None):
        self.calls.append((method, params))
        if method == "getProgramAccountsV2":
            raise RuntimeError("RPC error from primary: {'code': -32601, 'message': 'Method not found'}")
        return self.rows


class HeliusRpc:
    """Helius-like: plain getProgramAccounts refused on the Pump program,
    getProgramAccountsV2 served in pages of `page` rows."""

    def __init__(self, rows, page: int, total: bool = False):
        self.rows, self.page, self.total, self.calls = rows, page, total, []

    async def call(self, method, params=None):
        self.calls.append((method, params))
        if method == "getProgramAccounts":
            raise RuntimeError("RPC error from primary: {'code': -32600, 'message': 'Too many accounts requested'}")
        start = int(params[1].get("paginationKey") or 0)
        end = start + self.page
        out = {"accounts": self.rows[start:end], "paginationKey": str(end) if end < len(self.rows) else None}
        if self.total:
            out["totalResults"] = len(self.rows)
        return out


async def test_query_shape_counts_and_cache(redis):
    rpc = HeliusRpc(program_accounts(7, current_curve=CURVE, migrated=2), page=10000)
    h = await creator_history.creator_history(redis, rpc, CREATOR, MINT, CURVE, NOW)
    method, params = rpc.calls[0]
    assert method == "getProgramAccountsV2" and params[0] == "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
    cfg = params[1]
    assert cfg["dataSlice"] == {"offset": 48, "length": 1} and cfg["commitment"] == "confirmed" and cfg["limit"] == 10000
    assert {"memcmp": {"offset": 49, "bytes": CREATOR}} in cfg["filters"]
    assert h.status == "VERIFIED" and h.tokens_created == 7 and h.previous_launches == 6 and h.previous_migrated == 2
    assert "getProgramAccountsV2" in h.source
    # Cached: a second read makes no RPC call.
    await creator_history.creator_history(redis, rpc, CREATOR, MINT, CURVE, NOW)
    assert len(rpc.calls) == 1


async def test_v2_pages_are_followed_to_the_end(redis):
    rpc = HeliusRpc(program_accounts(12, current_curve=CURVE), page=5)
    h = await creator_history.creator_history(redis, rpc, CREATOR, MINT, CURVE, NOW)
    assert [c[0] for c in rpc.calls] == ["getProgramAccountsV2"] * 3
    assert [c[1][1].get("paginationKey") for c in rpc.calls] == [None, "5", "10"]
    assert h.status == "VERIFIED" and h.tokens_created == 12


async def test_scan_stopped_at_the_page_limit_is_a_lower_bound(redis):
    rows = program_accounts(20, current_curve=None)
    rpc = HeliusRpc(rows, page=2)  # 10 pages needed, 5 allowed
    h = await creator_history.creator_history(redis, rpc, CREATOR, MINT, CURVE, NOW)
    assert len(rpc.calls) == creator_history.MAX_V2_PAGES
    # 10 curves seen, plus this launch (not among them): at least 11 — never "11" as a count.
    assert h.status == "LOWER_BOUND" and h.tokens_created == 11 and "stopped after 5 page(s)" in h.source
    a = assess(replace(healthy(), creator_history=h.to_dict()), SafetySettings())
    assert a.reports["creator_history"]["tokens_created_display"] == "at least 11"
    # Below the minimum, a lower bound is UNKNOWN, not a low count.
    a = assess(replace(healthy(), creator_history=h.to_dict()), SafetySettings(min_creator_tokens_created=30))
    assert a.reports["creator_history"]["result"] == "UNKNOWN"


async def test_rpc_total_results_raises_the_lower_bound(redis):
    rpc = HeliusRpc(program_accounts(20, current_curve=None), page=2, total=True)
    h = await creator_history.creator_history(redis, rpc, CREATOR, MINT, CURVE, NOW)
    assert h.status == "LOWER_BOUND" and h.tokens_created == 20


async def test_plain_rpc_without_v2_falls_back(redis):
    rpc = RecordingRpc(program_accounts(4, current_curve=CURVE))
    h = await creator_history.creator_history(redis, rpc, CREATOR, MINT, CURVE, NOW)
    assert [c[0] for c in rpc.calls] == ["getProgramAccountsV2", "getProgramAccounts"]
    assert h.status == "VERIFIED" and h.tokens_created == 4 and "via getProgramAccounts" in h.source


async def test_request_errors_do_not_disable_the_primary_rpc():
    import httpx

    from yonixalpha_core.solana.rpc import RpcAllEndpointsFailedError, RpcManager

    def handler(request):
        body = json.loads(request.content)
        if body["method"] == "getProgramAccounts":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"],
                                             "error": {"code": -32600, "message": "Too many accounts requested"}})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": 1})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        rpc = RpcManager.create(client=client, primary_url="https://primary.example")
        for _ in range(5):
            with pytest.raises(RpcAllEndpointsFailedError):
                await rpc.call("getProgramAccounts", [])
        snap = rpc.health_snapshot()[0]
        assert snap["consecutive_failures"] == 0 and snap["disabled"] is False
        assert await rpc.call("getSlot") == 1


async def test_current_token_not_yet_in_the_cached_count_is_added(redis):
    # Count cached before this launch existed: 6 earlier curves, not this one.
    rpc = RecordingRpc(program_accounts(6, current_curve=None))
    h = await creator_history.creator_history(redis, rpc, CREATOR, MINT, CURVE, NOW)
    assert h.tokens_created == 7 and h.previous_launches == 6


async def test_curve_address_is_derived_for_a_migrated_token(redis):
    pda = creator_history.bonding_curve_pda(MINT)
    rpc = RecordingRpc(program_accounts(3, current_curve=pda))
    h = await creator_history.creator_history(redis, rpc, CREATOR, MINT, None, NOW)
    assert h.tokens_created == 3


async def test_unknown_creator_is_unknown(redis):
    h = await creator_history.creator_history(redis, RecordingRpc([]), None, MINT, CURVE, NOW)
    assert h.status == "UNKNOWN" and h.tokens_created is None and "creator wallet unknown" in h.error


async def test_previous_launch_behaviour_from_the_stream(redis):
    # An earlier launch by the same creator, where the creator sold 60 s in,
    # and whose observation outcome was REJECT.
    prev = b58encode(bytes([44]) * 32)
    t0 = NOW - timedelta(minutes=50)
    await pump_stream.ingest_logs(redis, logs_of(create_event(t0, mint=prev, symbol="OLD")), "cprev", t0)
    c = Curve(prev)
    await pump_stream.ingest_logs(redis, logs_of(c.trade(wallet(1), t0 + timedelta(seconds=10), 500_000_000, True),
                                                 c.trade(CREATOR, t0 + timedelta(seconds=60), 100_000_000, False)),
                                  "tprev", t0 + timedelta(seconds=60))
    await redis.set(pump_stream.obs_report_key(prev), json.dumps({"outcome": "REJECT", "reasons": ["creator sold while sellers dominated"]}))
    await seed_healthy_launch(redis, NOW)
    h = await creator_history.creator_history(redis, RecordingRpc(program_accounts(2)), CREATOR, MINT, CURVE, NOW)
    assert h.previous_checked_for_sells == 1 and h.previous_creator_sold_early == 1
    assert h.previous_observation_rejected == 1 and "creator sold" in h.previous_rejection_reasons[0]
    a = assess(replace(healthy(), creator_history=h.to_dict()), FRESH)
    assert "CREATOR_PRIOR_EARLY_SELLS" in codes(a)


# --- migrated liquidity (USD) --------------------------------------------------


def migrated(sol: str, sol_usd: str | None):
    inp = healthy(engine="solana_migration")
    return replace(inp, market=replace(inp.market, curve_complete=True, migrated=True, liquidity_quote=Decimal(sol)),
                   sol_usd=Decimal(sol_usd) if sol_usd else None, sol_usd_source="test")


@pytest.mark.parametrize("sol, usd, expected_usd", [("100", "150", "15000.00"), ("100", "100", "10000.00")])
def test_required_5_6_usable_liquidity_at_or_above_minimum_passes(sol, usd, expected_usd):
    a = assess(migrated(sol, usd), SafetySettings())
    r = a.reports["migrated_liquidity"]
    assert r["applies"] and r["usable_liquidity_usd"] == expected_usd and r["decision"] == "PASS"
    assert "MIGRATED_LIQUIDITY_OK" in codes(a) and "INSUFFICIENT_MIGRATED_LIQUIDITY" not in codes(a)
    assert f"Usable Liquidity: ${Decimal(expected_usd):,.0f}" in finding(a, "MIGRATED_LIQUIDITY_OK").message
    assert a.decision != FinalDecision.NO_TRADE, a.reasons


def test_required_7_usable_liquidity_below_minimum_is_no_trade():
    a = assess(migrated("99.99", "100"), SafetySettings())  # $9,999
    r = a.reports["migrated_liquidity"]
    assert r["usable_liquidity_usd"] == "9999.00" and r["decision"] == "NO_TRADE"
    assert r["reason"] == "INSUFFICIENT_MIGRATED_LIQUIDITY"
    f = finding(a, "INSUFFICIENT_MIGRATED_LIQUIDITY")
    assert "Usable Liquidity: $9,999" in f.message and "Minimum: $10,000" in f.message
    assert "Decision: NO_TRADE · Reason: INSUFFICIENT_MIGRATED_LIQUIDITY" in f.message
    assert a.decision == FinalDecision.NO_TRADE and f.hard_block


def test_unknown_sol_usd_is_no_trade_not_a_guess():
    a = assess(migrated("500", None), SafetySettings())
    assert a.decision == FinalDecision.NO_TRADE and "MIGRATED_LIQUIDITY_USD_UNKNOWN" in codes(a)


def test_migrated_report_has_depth_impact_and_slippage():
    a = assess(migrated("100", "150"), SafetySettings())
    r = a.reports["migrated_liquidity"]
    for k in ("total_liquidity_usd", "usable_liquidity_sol", "executable_max_size_sol", "executable_max_size_usd",
              "entry_impact_bps", "exit_impact_bps", "entry_slippage_bps", "exit_slippage_bps", "sol_usd"):
        assert r.get(k) is not None, k
    # Slippage is impact plus the pool fee, so never below the impact.
    assert Decimal(r["entry_slippage_bps"]) >= Decimal(r["entry_impact_bps"])


def test_migrated_slippage_limit_blocks():
    a = assess(migrated("100", "150"), SafetySettings(migrated_max_entry_slippage_bps=Decimal("1")))
    assert "MIGRATED_ENTRY_SLIPPAGE" in codes(a) and a.decision == FinalDecision.NO_TRADE


def test_migrated_liquidity_check_can_be_switched_off():
    a = assess(migrated("50", "100"), SafetySettings(migrated_liquidity_check=False))
    assert not {c for c in codes(a) if "MIGRATED_LIQUIDITY" in c}


async def test_required_8_fresh_token_without_a_pool_is_not_rejected_for_missing_migrated_liquidity(redis):
    # No SOL/USD source at all and no pool: the fresh token is judged on the curve.
    _, _, a = await fresh(redis, 10)
    r = a.reports["migrated_liquidity"]
    assert r["applies"] is False and "bonding-curve token" in r["reason"]
    assert not {c for c in codes(a) if "MIGRATED" in c}
    assert "BONDING_CURVE_MARKET" in codes(a) and a.decision == FinalDecision.EXECUTE, a.reasons


def migration_event(mint: str, pool: str, at: datetime) -> bytes:
    return MIGRATION_EVENT + pk(CREATOR) + pk(mint) + u64(10**15) + u64(85 * 10**9) + u64(0) + pk(CURVE) + i64(int(at.timestamp())) + pk(pool)


async def test_required_9_fresh_token_that_migrates_gets_the_migrated_rule(redis):
    _, _, before = await fresh(redis, 10)
    assert before.reports["migrated_liquidity"]["applies"] is False
    pool = pumpswap.canonical_pool(MINT)
    await pump_stream.ingest_logs(redis, logs_of(migration_event(MINT, pool, NOW - timedelta(minutes=30))), "mig",
                                  NOW - timedelta(minutes=30))
    assert (await pump_stream.load_curve(redis, MINT)).pool == pool
    rpc = FakePoolRpc(MINT, 700_000_000_000_000, 60 * 10**9,
                      trade_history(pool, 25, 0, NOW - timedelta(seconds=25 * 20), every=20), inner=FakeRpc(None))
    await seed_sol_usd(redis, NOW, Decimal("150"))  # 60 SOL x $150 = $9,000 usable
    inp, ev = await assemble_migrated(Sources(redis, rpc), MINT, NOW, Controls(MIGRATED, empty_account()))
    a = assess(inp, MIGRATED)
    r = a.reports["migrated_liquidity"]
    assert r["applies"] and r["usable_liquidity_usd"] == "9000.00" and r["reason"] == "INSUFFICIENT_MIGRATED_LIQUIDITY"
    assert a.decision == FinalDecision.NO_TRADE
    # Creator history follows the token across migration (curve PDA derived).
    assert inp.creator_history["status"] == "VERIFIED"


async def test_sol_usd_sources(redis):
    class Jup:
        def __init__(self, out):
            self.out = out

        async def quote(self, *a):
            return QuoteResult("ok", data={"outAmount": self.out, "routePlan": []}) if self.out else QuoteResult("error", error="down")

    class Dex:
        async def pool(self, mint):
            return PoolData(NOW, "p", "pumpswap", Decimal("0.0001"), None, None, None, None, None, None, None, None, None, None,
                            price_usd=Decimal("0.0147")), None

    price, source, _ = await sol_price.sol_usd(redis, Jup("147250000"), None, MINT, NOW)
    assert price == Decimal("147.25") and "Jupiter" in source
    await redis.flushdb()
    price, source, errors = await sol_price.sol_usd(redis, Jup(None), Dex(), MINT, NOW)
    assert price == Decimal("147") and "DexScreener" in source and errors
    await redis.flushdb()
    price, source, errors = await sol_price.sol_usd(redis, Jup(None), None, MINT, NOW)
    assert price is None and source == "unavailable" and errors


async def test_migrated_without_any_sol_usd_source_is_no_trade(redis):
    pool = pumpswap.canonical_pool(MINT)
    rpc = FakePoolRpc(MINT, 700_000_000_000_000, 95 * 10**9,
                      trade_history(pool, 25, 0, NOW - timedelta(seconds=25 * 20), every=20), inner=FakeRpc(None))
    inp, ev = await assemble_migrated(Sources(redis, rpc), MINT, NOW, Controls(MIGRATED, empty_account()))
    a = assess(inp, MIGRATED)
    assert inp.sol_usd is None and "MIGRATED_LIQUIDITY_USD_UNKNOWN" in codes(a) and a.decision == FinalDecision.NO_TRADE


# --- dashboard settings -----------------------------------------------------


async def test_required_10_dashboard_threshold_change_is_used_by_the_engine(db, redis):
    history = {"creator": CREATOR, "status": "VERIFIED", "tokens_created": 7, "previous_launches": 6, "previous_migrated": 1,
               "source": creator_history.SOURCE_ONCHAIN}
    settings, _ = await store.load_settings(db, "solana_fresh")
    a = assess(replace(healthy(engine="solana_fresh"), creator_history=history), settings)
    assert a.reports["creator_history"]["result"] == "PASS"
    # The operator raises the minimum to 10 and picks REJECT on Risk Settings.
    await store.save_settings(db, "solana_fresh", {"min_creator_tokens_created": 10,
                                                   "creator_below_threshold_action": "REJECT"}, None, "dashboard")
    settings, _ = await store.load_settings(db, "solana_fresh")
    assert settings.min_creator_tokens_created == 10 and settings.creator_below_threshold_action == "REJECT"
    a = assess(replace(healthy(engine="solana_fresh"), creator_history=history), settings)
    assert a.decision == FinalDecision.REJECT and "Creator Tokens Created: 7 · Minimum: 10 · Action: REJECT" in \
        finding(a, "CREATOR_HISTORY_BELOW_THRESHOLD").message

    # Same for the migrated USD minimum: $15,000 passes at 10,000, fails at 20,000.
    settings, _ = await store.load_settings(db, "solana_migration")
    assert assess(migrated("100", "150"), settings).reports["migrated_liquidity"]["decision"] == "PASS"
    await store.save_settings(db, "solana_migration", {"min_migrated_liquidity_usd": "20000"}, None, "dashboard")
    settings, _ = await store.load_settings(db, "solana_migration")
    a = assess(migrated("100", "150"), settings)
    assert a.decision == FinalDecision.NO_TRADE and "Minimum: $20,000" in finding(a, "INSUFFICIENT_MIGRATED_LIQUIDITY").message


def test_settings_validation_and_coercion():
    s = settings_from_dict({"min_creator_tokens_created": "3", "creator_below_threshold_action": "REDUCE_SIZE",
                            "min_migrated_liquidity_usd": "25000", "creator_history_check": False})
    assert s.min_creator_tokens_created == 3 and s.min_migrated_liquidity_usd == Decimal("25000")
    assert s.creator_history_check is False and not validate(s)
    assert validate(replace(s, creator_below_threshold_action="BLOCK"))
    assert validate(replace(s, max_creator_tokens_created=2))  # ceiling must exceed the minimum


# --- name filters -------------------------------------------------------------


def test_name_too_short_and_non_ascii():
    a = assess(replace(healthy(), token_name="X"), SafetySettings())
    assert "NAME_TOO_SHORT" in codes(a) and a.decision == FinalDecision.REJECT
    a = assess(replace(healthy(), token_name="Ｐｅｐｅ"), SafetySettings(ascii_names_only=True))
    assert "NON_ASCII_NAME" in codes(a)
    a = assess(replace(healthy(), token_name="Ｐｅｐｅ"), SafetySettings())
    assert "NON_ASCII_NAME" not in codes(a)  # off by default


async def test_duplicate_name_from_the_stream(redis):
    first = b58encode(bytes([55]) * 32)
    await pump_stream.ingest_logs(redis, logs_of(create_event(NOW - timedelta(hours=1), mint=first, symbol="AAA")), "c1",
                                  NOW - timedelta(hours=1))
    # seed_healthy_launch creates MINT named "Pipeline Coin" too, later.
    inp, _, a = await fresh(redis, 10)
    assert inp.duplicate_of == first and "DUPLICATE_NAME" in codes(a) and a.decision == FinalDecision.REJECT
    assert await pump_stream.duplicate_of(redis, first, "pipeline  COIN!") is None  # the first launch keeps its name
    off = replace(FRESH, skip_duplicate_names=False)
    inp, _ = await assemble_fresh(Sources(redis, FakeRpc(Curve(), creator_tokens=10)), MINT, NOW, Controls(off, empty_account()))
    assert inp.duplicate_of is None
