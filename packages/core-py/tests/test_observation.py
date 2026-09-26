from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from yonixalpha_core.exit_intel import ExitConfig, solana_exit_decision
from yonixalpha_core.safety.gate import assess
from yonixalpha_core.safety.models import HolderInfo, Observation
from yonixalpha_core.safety.settings import SafetySettings, validate
from yonixalpha_core.solana import observation
from yonixalpha_core.solana.flow import Trade, measured_volatility
from yonixalpha_core.solana.pump_stream import StreamCurve
from yonixalpha_core.solana.token_safety import MAYHEM_AUTHORITIES, owner_kind, parse_holders

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
S = SafetySettings()
SOL = 10**9


def tr(at: datetime, who: str, buy: bool, sol: float = 0.1, vsol: float = 30.0) -> Trade:
    return Trade(at, who, buy, int(sol * SOL), 10**12, int(vsol * SOL), 10**15)


def curve(complete=False, pool=None, rtok=700_000_000_000_000) -> StreamCurve:
    return StreamCurve(vsol=30 * SOL, vtok=10**15, rsol=2 * SOL, rtok=rtok, fee_bps=125, updated_at=NOW,
                       complete=complete, pool=pool)


def launch(age: float) -> int:
    return int((NOW - timedelta(seconds=age)).timestamp())


# --- observation window ---------------------------------------------------------

def test_inside_the_window_the_token_is_only_observed():
    r = observation.evaluate("M", [], launch(4), NOW, S, curve=curve())
    assert r.outcome == observation.OBSERVING and "FRESH_OBSERVING" in r.reasons[0]


def test_checkpoints_compare_t0_half_and_window_and_classify_increasing():
    created = launch(12)
    t0 = datetime.fromtimestamp(created, tz=timezone.utc)
    trades = [tr(t0 + timedelta(seconds=1 + i), f"a{i}", True, vsol=30 + i) for i in range(3)]
    trades += [tr(t0 + timedelta(seconds=6 + i // 2), f"b{i}", True, vsol=33 + i) for i in range(7)]
    r = observation.evaluate("M", trades, created, NOW, S, curve=curve())
    assert r.outcome == observation.PROMOTE and r.trend == observation.INCREASING
    assert [c["label"] for c in r.checkpoints] == ["T0", "T+5s", "T+10s"]
    assert [c["trades"] for c in r.checkpoints] == [0, 3, 10]
    assert r.halves[0]["new_buyers"] == 3 and r.halves[1]["new_buyers"] == 7
    assert r.metrics["liquidity_state"].startswith("NO DEX POOL YET") and r.metrics["curve_progress"] == "0.1174"


def test_no_liquidity_requirement_before_migration():
    # 0.01 SOL real reserve: irrelevant to the observation outcome.
    created = launch(12)
    t0 = datetime.fromtimestamp(created, tz=timezone.utc)
    trades = [tr(t0 + timedelta(seconds=1 + i), f"w{i}", True) for i in range(10)]
    c = replace(curve(), rsol=10_000_000)
    assert observation.evaluate("M", trades, created, NOW, S, curve=c).outcome == observation.PROMOTE


def test_deterioration_and_price_reversal():
    created = launch(12)
    t0 = datetime.fromtimestamp(created, tz=timezone.utc)
    up = [tr(t0 + timedelta(seconds=1 + i // 2), f"u{i}", True, vsol=30 + 2 * i) for i in range(8)]
    down = [tr(t0 + timedelta(seconds=7 + i), f"u{i}", False, 0.3, vsol=44 - 9 * i) for i in range(3)]  # peak -41%
    r = observation.evaluate("M", up + down, created, NOW, S, curve=curve())
    assert r.trend == observation.DETERIORATING and r.outcome == observation.REJECT
    assert any("sells accelerating" in n for n in r.negative) and "below its observed peak" in r.reasons[-1]


def test_continue_monitoring_then_expiry_limits():
    created = launch(12)
    t0 = datetime.fromtimestamp(created, tz=timezone.utc)
    trades = [tr(t0 + timedelta(seconds=2 + i), f"w{i}", True) for i in range(3)]
    r = observation.evaluate("M", trades, created, NOW, S, curve=curve())
    assert r.outcome == observation.CONTINUE_MONITORING
    off = observation.evaluate("M", trades, created, NOW, replace(S, fresh_continue_monitoring=False), curve=curve())
    assert off.outcome == observation.NO_TRADE and "disabled" in off.reasons[-1]
    later = NOW + timedelta(seconds=S.fresh_inactivity_timeout_seconds + 5)
    idle = observation.evaluate("M", trades, created, later, S, curve=curve(), monitoring_since=NOW)
    assert idle.outcome == observation.NO_TRADE and "inactive" in idle.reasons[-1]
    old = observation.evaluate("M", trades + [tr(later - timedelta(seconds=1), "x", True)], launch(S.fresh_max_monitoring_seconds + 1),
                               later, S, curve=curve(), monitoring_since=NOW)
    assert old.outcome == observation.NO_TRADE and "maximum monitoring time" in old.reasons[-1]


def test_migration_during_observation_switches_to_pool_rules():
    r = observation.evaluate("M", [], launch(30), NOW, S, curve=curve(complete=True, pool="POOL"))
    assert r.outcome == observation.MIGRATION_DETECTED and "MIGRATED_ANALYSIS" in r.reasons[0]
    assert r.metrics["liquidity_state"] == "MIGRATED (PumpSwap pool)"


def test_observation_settings_are_validated():
    assert validate(replace(S, fresh_max_monitoring_seconds=5)) == ["fresh_max_monitoring_seconds must be at least fresh_observation_seconds"]
    assert "exit_volume_collapse_ratio must be between 0 and 1" in validate(replace(S, exit_volume_collapse_ratio=Decimal(1)))


# --- holders ----------------------------------------------------------------------

WALLET_A = "6o1yaafezQ7bRBWAKnAwpYmmBxvky2hKMqYzhZycxUEX"  # on curve: a keypair
PDA = "BwWK17cbHxwWBKZkUYvzxLcNQ1YVyaFezduWbtm2de6s"  # Mayhem sol-vault PDA (off curve)


def test_owner_kinds():
    assert owner_kind(WALLET_A) == "wallet" and PDA in MAYHEM_AUTHORITIES and owner_kind(PDA) == "protocol_agent"
    from solders.pubkey import Pubkey

    other_pda = str(Pubkey.find_program_address([b"x"], Pubkey.from_string(WALLET_A))[0])
    assert owner_kind(other_pda) == "program"


def test_mayhem_vault_is_not_reported_as_a_wallet_holding_half_the_supply():
    largest = [{"address": "curve", "amount": "400"}, {"address": "vault", "amount": "500"}, {"address": "a", "amount": "30"}]
    h = parse_holders(largest, {"curve": "CURVE", "vault": PDA, "a": WALLET_A}, 1000, {"CURVE"}, WALLET_A, NOW, "rpc")
    assert h.top1_share == Decimal("0.03") and h.top1_owner == WALLET_A and h.protocol_agent_share == Decimal("0.5")


def _healthy():
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location("sg_fixture", Path(__file__).with_name("test_safety_gate.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.healthy


def test_holder_findings_use_precise_labels():
    healthy = _healthy()

    h = HolderInfo(Observation("t", NOW), Decimal("0.03"), Decimal("0.10"), Decimal("0.03"), 5, 1, top1_owner=WALLET_A,
                   protocol_agent_share=Decimal("0.5"))
    a = assess(replace(healthy(holders=h), creator=WALLET_A), S)
    msgs = {f.code: f.message for f in a.findings}
    assert "TOP1_CRITICAL" not in msgs  # the old "one wallet holds 50%"
    assert msgs["PROTOCOL_AGENT_HOLDING"].startswith("PUMP.FUN MAYHEM AGENT VAULT: 50.0%")
    big = HolderInfo(Observation("t", NOW), Decimal("0.45"), Decimal("0.5"), Decimal("0.45"), 5, 1, top1_owner=WALLET_A,
                     largest_program_owner=PDA[:-1] + "x", largest_program_share=Decimal("0.2"))
    a = assess(replace(healthy(holders=big), creator=WALLET_A), S)
    msgs = {f.code: f.message for f in a.findings}
    assert msgs["TOP1_CRITICAL"].startswith("TOP HOLDER (creator wallet)") and "45.0%" in msgs["TOP1_CRITICAL"]
    assert msgs["CREATOR_CONCENTRATION"].startswith("CREATOR WALLET holding: 45.0%")
    assert msgs["PROGRAM_CONTROLLED_HOLDING"].startswith("PROGRAM-CONTROLLED ACCOUNT")


# --- volatility, exits --------------------------------------------------------------

def test_young_token_volatility_is_measured_from_10_second_returns():
    trades = [tr(NOW - timedelta(seconds=45 - i), f"w{i}", True, vsol=30 + i * 0.1) for i in range(45)]
    vol, source = measured_volatility(trades, NOW, 900, 6)
    assert vol is not None and "10-second" in source
    assert measured_volatility(trades[:5], NOW, 900, 6)[0] is None


def test_emergency_exit_now_on_a_single_severe_signal():
    quiet = [tr(NOW - timedelta(seconds=30), "b", True)]
    assert solana_exit_decision(quiet, NOW, None, Decimal(30), Decimal(10)).action == "EXIT_NOW"
    d = solana_exit_decision(quiet, NOW, None, None, None, highest_price=Decimal(100), current_price=Decimal(60))
    assert d.action == "EXIT_NOW" and "40%" in d.reasons[0]
    cfg = ExitConfig.from_settings(replace(S, exit_emergency_price_drop=Decimal("0.5")))
    assert solana_exit_decision(quiet, NOW, None, None, None, cfg=cfg, highest_price=Decimal(100), current_price=Decimal(60)).action == "HOLD"


def test_volume_collapse_with_buyers_stopping_reduces():
    prev = [tr(NOW - timedelta(seconds=200 - i), f"p{i}", True, 0.5) for i in range(6)]
    now_w = [tr(NOW - timedelta(seconds=20), "s0", False, 0.05)]
    d = solana_exit_decision(prev + now_w, NOW, None, None, None)
    assert d.action == "REDUCE" and "volume collapsed" in d.reasons[0] and d.metrics["buyers_stopped"] is True
