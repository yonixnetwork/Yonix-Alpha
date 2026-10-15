"""Recovery plan R2, R3, R10 (2026-10-15).

R2: the migration engine refuses tokens whose market cap at the decision is
far above a completed curve (established tokens such as ZEREBRO, D O T F).
R3: a Solana automatic stop gets a 10% floor when the measured volatility is
below 1%, so the size shrinks for the same planned loss.
R10: cooldown_scope STRATEGY limits the loss cooldown to the strategy that
lost; ACCOUNT (default) keeps the original account-wide behaviour."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

from yonixalpha_core.safety import ConstantProductModel, SafetySettings, assess
from yonixalpha_core.safety.gate import account_blockers, entry_market_cap_sol
from yonixalpha_core.safety.models import AccountState, ManualOverrides, MarketInfo
from yonixalpha_core.safety.planning import plan_trade
from yonixalpha_core.safety.settings import settings_from_dict, validate

from tests.test_sol_sizing import FRESH, NOW, account, codes, healthy

SUPPLY_WHOLE = Decimal(10**15) / Decimal(10**6)  # healthy(): 6 decimals, 10**15 raw
POOL = ConstantProductModel(Decimal("2000"), Decimal("2000000000"), Decimal("25"))  # a quiet PumpSwap-like pool


def migration(mcap_sol: str, **changes):
    """A migration-engine decision at the given market cap (SOL)."""
    price = Decimal(mcap_sol) / SUPPLY_WHOLE
    m = MarketInfo(FRESH, price=price, volatility=Decimal("0.05"), liquidity_quote=Decimal("85"), age_seconds=300,
                   curve_complete=True, migrated=True)
    return healthy(engine="solana_migration", strategy_name="post_migration_flow", market=m, **changes)


# --- R2 ------------------------------------------------------------------------

def test_market_cap_at_the_decision_is_price_times_whole_token_supply():
    assert entry_market_cap_sol(migration("93354")) == Decimal("93354")


def test_established_tokens_are_refused_by_the_migration_engine():
    s = SafetySettings()
    for name, mcap in (("ZEREBRO", "250146"), ("D O T F", "93354")):
        a = assess(migration(mcap), s)
        assert "ESTABLISHED_TOKEN_NOT_MIGRATION" in codes(a), name
        f = next(f for f in a.findings if f.code == "ESTABLISHED_TOKEN_NOT_MIGRATION")
        assert f.hard_block and f.action.value == "REJECT" and "above migration_max_entry_market_cap_sol 3000" in f.message
        assert a.decision.value == "REJECT"


def test_fresh_migrations_other_engines_operator_buys_and_the_off_switch_are_not_refused():
    s = SafetySettings()
    assert "ESTABLISHED_TOKEN_NOT_MIGRATION" not in codes(assess(migration("420"), s))  # just-completed curve
    assert "ESTABLISHED_TOKEN_NOT_MIGRATION" not in codes(assess(migration("2999"), s))
    fresh = replace(migration("250146"), engine="solana_fresh", strategy_name="fresh_launch_flow")
    assert "ESTABLISHED_TOKEN_NOT_MIGRATION" not in codes(assess(fresh, s))
    assert "ESTABLISHED_TOKEN_NOT_MIGRATION" not in codes(assess(migration("250146", operator_request=True), s))
    off = SafetySettings(migration_max_entry_market_cap_sol=Decimal(0))
    assert "ESTABLISHED_TOKEN_NOT_MIGRATION" not in codes(assess(migration("250146"), off))
    no_supply = replace(migration("250146"), token=None)
    assert entry_market_cap_sol(no_supply) is None  # unknown is never guessed into a refusal


# --- R3 ------------------------------------------------------------------------

def _plan(volatility: str, floor: str = "0.10", **kw):
    s = SafetySettings(max_position_size_quote=Decimal("10"), **kw)
    return plan_trade(entry_price=POOL.marginal_price, volatility=Decimal(volatility), liquidity_quote=Decimal("2000"),
                      settings=s, account=account("10"), overrides=ManualOverrides(), model=POOL, quote=None,
                      transfer_fee_bps=None, low_volatility_floor=Decimal(floor))


def test_zerebro_inputs_get_the_10_percent_floor_and_a_smaller_size():
    quiet = _plan("0.002")  # ZEREBRO: near-zero volatility on a quiet pool
    before = _plan("0.002", floor="0")  # the old behaviour: 5% min_stop_pct
    assert before.stop_distance_pct == Decimal("0.05") and quiet.stop_distance_pct == Decimal("0.10")
    assert "low-volatility floor" in quiet.stop_loss.method
    assert quiet.stop_loss.inputs["low_volatility_min_stop_pct"] == Decimal("0.10")
    assert quiet.position_size.value < before.position_size.value
    # the planned loss at the stop stays within the same budget
    assert quiet.planned_loss_at_stop <= quiet.max_loss.value


def test_normal_volatility_and_the_off_switch_keep_the_old_stop():
    assert _plan("0.04").stop_distance_pct == Decimal("0.08")  # 2 x 4%, above every floor
    assert _plan("0.012").stop_distance_pct == Decimal("0.05")  # 1.2% volatility is measurable
    assert _plan("0.002", low_volatility_stop_threshold=Decimal(0)).stop_distance_pct == Decimal("0.05")


def test_the_gate_applies_the_floor_to_solana_engines_only():
    quiet = replace(healthy(), market=replace(healthy().market, volatility=Decimal("0.002")))
    a = assess(quiet, SafetySettings())
    assert a.plan.stop_distance_pct == Decimal("0.10")


def test_low_volatility_settings_are_validated():
    assert validate(settings_from_dict({"low_volatility_min_stop_pct": "0.6"}))  # above max_stop_pct
    assert validate(settings_from_dict({"low_volatility_stop_threshold": "1"}))
    assert validate(settings_from_dict({"migration_max_entry_market_cap_sol": "-1"}))
    assert validate(settings_from_dict({"cooldown_scope": "EVERYONE"}))
    assert not validate(settings_from_dict({"cooldown_scope": "STRATEGY", "low_volatility_min_stop_pct": "0"}))


# --- R10 -----------------------------------------------------------------------

def _acct(account_loss_min_ago: float | None, own_loss_min_ago: float | None, known: bool = True) -> AccountState:
    def at(m):
        return NOW - timedelta(minutes=m) if m is not None else None
    return replace(account(), last_loss_at=at(account_loss_min_ago), strategy_last_loss_at=at(own_loss_min_ago),
                   strategy_loss_known=known)


def test_account_scope_is_the_default_and_pauses_every_strategy():
    s = SafetySettings()
    assert s.cooldown_scope == "ACCOUNT"
    f = account_blockers(_acct(1, None), s, NOW)
    assert [x.code for x in f] == ["LOSS_COOLDOWN"] and "a loss on the account" in f[0].message


def test_strategy_scope_waits_only_for_the_strategy_that_lost():
    s = SafetySettings(cooldown_scope="STRATEGY")
    # fresh lost a minute ago; migration has no loss of its own: migration is not paused
    assert account_blockers(_acct(1, None), s, NOW) == []
    own = account_blockers(_acct(1, 2), s, NOW)
    assert [x.code for x in own] == ["LOSS_COOLDOWN"] and "this strategy's last loss" in own[0].message
    assert account_blockers(_acct(1, 10), s, NOW) == []  # its own loss is older than 300 s
    # a caller that did not compute the strategy's losses keeps the account-wide rule
    assert [x.code for x in account_blockers(_acct(1, None, known=False), s, NOW)] == ["LOSS_COOLDOWN"]


def test_daily_limits_are_unchanged_by_the_cooldown_scope():
    s = SafetySettings(cooldown_scope="STRATEGY")
    blocked = replace(_acct(None, None), daily_realized_pnl=Decimal("-0.6"))
    assert {f.code for f in account_blockers(blocked, s, NOW)} == {"DAILY_LOSS_LIMIT"}


def test_account_state_reads_this_strategys_own_last_loss():
    import asyncio
    import os

    os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://yonixalpha:yonixalpha_test_pw@localhost:5432/yonixalpha_test")
    from sqlalchemy.ext.asyncio import create_async_engine

    from yonixalpha_core.db.base import Base, make_session_factory
    from yonixalpha_core.db.models import PaperAccount, PaperPosition
    from yonixalpha_core.safety import store

    async def run():
        engine = create_async_engine(os.environ["DATABASE_URL"])
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
        sf = make_session_factory(engine)
        async with sf() as s:
            acct = PaperAccount(name="solana", quote_currency="SOL", starting_balance=Decimal(10), cash_balance=Decimal(10))
            s.add(acct)
            await s.flush()
            for eng, minutes, pnl in (("solana_fresh", 1, "-0.01"), ("solana_migration", 30, "-0.02"),
                                      ("solana_migration", 5, "0.03")):
                at = NOW - timedelta(minutes=minutes)
                s.add(PaperPosition(symbol="X", provider="paper", side="LONG", entry_price=Decimal(1), quantity=Decimal(1),
                                    initial_quantity=Decimal(1), take_profit=[], status="closed", entry_at=at, exit_at=at,
                                    engine=eng, asset_id=f"M{minutes}", account_id=acct.id, realized_pnl=Decimal(pnl)))
            await s.commit()
            mig = await store.account_state(s, acct, None, NOW, False, engine="solana_migration")
            old = await store.account_state(s, acct, None, NOW, False)
        await engine.dispose()
        return mig, old

    mig, old = asyncio.run(run())
    assert mig.last_loss_at == NOW - timedelta(minutes=1)  # fresh's loss, account-wide
    assert mig.strategy_last_loss_at == NOW - timedelta(minutes=30) and mig.strategy_loss_known  # its own loss; wins ignored
    assert old.strategy_loss_known is False and old.strategy_last_loss_at is None
    s = SafetySettings(cooldown_scope="STRATEGY")
    assert account_blockers(mig, s, NOW) == []  # migration's own loss is 30 min old
    assert [f.code for f in account_blockers(mig, SafetySettings(), NOW)] == ["LOSS_COOLDOWN"]


# --- R9 (holder cache) ---------------------------------------------------------

def test_holder_reads_are_cached_longer_for_migrated_pools_and_never_past_the_data_age(monkeypatch):
    import asyncio
    import time

    from yonixalpha_core.solana import assembler

    calls = []

    async def fake_fetch(rpc, mint, supply_raw, excluded, creator, now):
        calls.append(mint)
        return object(), None

    monkeypatch.setattr(assembler, "fetch_holders", fake_fetch)
    rpc = type("R", (), {})()

    async def run():
        await assembler.cached_holders(rpc, "CURVE", 1, set(), None, NOW, 60)
        await assembler.cached_holders(rpc, "POOL", 1, set(), None, NOW, 60,
                                       cache_seconds=assembler.HOLDERS_CACHE_MIGRATED_SECONDS)
        await assembler.cached_holders(rpc, "STRICT", 1, set(), None, NOW, 30,
                                       cache_seconds=assembler.HOLDERS_CACHE_MIGRATED_SECONDS)
        await assembler.cached_holders(rpc, "POOL", 1, set(), None, NOW, 60,
                                       cache_seconds=assembler.HOLDERS_CACHE_MIGRATED_SECONDS)

    asyncio.run(run())
    assert calls == ["CURVE", "POOL", "STRICT"]  # the second POOL read came from the cache
    left = {k[1]: exp - time.monotonic() for k, (exp, _) in assembler._cache_of(rpc).items()}
    assert 14 < left["CURVE"] <= 15  # bonding curve: unchanged
    assert 49 < left["POOL"] <= 50  # migrated pool: max_data_age_seconds 60 - 10
    assert 19 < left["STRICT"] <= 20  # a stricter data age wins
