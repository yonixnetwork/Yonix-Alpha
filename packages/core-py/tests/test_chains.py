"""Launchpad registry, evidence-based status and operator trading controls."""
from datetime import datetime, timedelta, timezone

from yonixalpha_core.chains import controls
from yonixalpha_core.chains.base import CHECKS, Chain, Lifecycle
from yonixalpha_core.chains.registry import CHAINS, LAUNCHPADS, launchpads_for
from yonixalpha_core.chains.verification import compute_status
from yonixalpha_core.safety import SafetySettings, assess
from yonixalpha_core.safety.models import FinalDecision
from tests.test_safety_gate import healthy

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)


def passed(*names, at=NOW):
    return {c: {"status": "PASS", "at": at, "ever_passed": True} for c in names}


def test_registry_has_exactly_the_three_chains_and_honest_lifecycles():
    assert set(CHAINS) == {Chain.SOLANA, Chain.BSC, Chain.ROBINHOOD}
    assert CHAINS[Chain.ROBINHOOD].evm_chain_id == 4663 and CHAINS[Chain.BSC].evm_chain_id == 56
    assert {s.key for s in launchpads_for(Chain.BSC)} == {"fourmeme", "flap"}
    assert LAUNCHPADS["noxa"].lifecycle == Lifecycle.INSTANT_POOL and not LAUNCHPADS["noxa"].active
    assert LAUNCHPADS["odyssey_curve"].lifecycle == Lifecycle.BONDING_CURVE_TO_DEX
    assert LAUNCHPADS["pons_v1"].migration_model.startswith("none")  # no invented graduation event
    for s in LAUNCHPADS.values():
        assert s.sources and s.supported_events
        for addr in s.contracts.values():
            if s.chain != Chain.SOLANA:
                assert addr.startswith("0x") and len(addr) == 42


def test_status_follows_evidence_never_the_label():
    fm = LAUNCHPADS["fourmeme"]
    assert compute_status(fm, {}, "PAPER", NOW)["status"] == "UNVERIFIED"
    paper = passed("ACTIVE", "DISCOVERY", "EVENTS", "QUOTE", "SAFETY")
    st = compute_status(fm, paper, "PAPER", NOW)
    assert st["status"] == "PAPER_ONLY" and st["paper_allowed"] and not st["live_allowed"] and "BUY" in st["missing_for_live"]
    all_ok = passed(*CHECKS)
    assert compute_status(fm, all_ok, "LIVE", NOW)["status"] == "LIVE"
    assert compute_status(fm, all_ok, "PAPER", NOW)["status"] == "PAPER_ONLY"  # operator decides live
    assert compute_status(fm, all_ok, "OFF", NOW)["status"] == "DISABLED"
    stale = passed("ACTIVE", "DISCOVERY", "EVENTS", "QUOTE", "SAFETY", at=NOW - timedelta(days=2))
    assert compute_status(fm, stale, "PAPER", NOW)["status"] == "DEGRADED"  # verified once, liveness expired
    failing = {**paper, "DISCOVERY": {"status": "FAIL", "at": NOW, "ever_passed": True}}
    assert compute_status(fm, failing, "PAPER", NOW)["status"] == "DEGRADED"


def test_inactive_and_observe_only_venues_are_disabled_whatever_the_evidence():
    assert compute_status(LAUNCHPADS["noxa"], passed(*CHECKS), "LIVE", NOW)["status"] == "DISABLED"
    refl = compute_status(LAUNCHPADS["odyssey_reflection"], passed(*CHECKS), "LIVE", NOW)
    assert refl["status"] == "DISABLED" and "observe only" in refl["why"]


def test_switches_block_the_right_entries():
    none = {}
    assert controls.blocked_by(none, Chain.SOLANA, "sniper") is None
    assert controls.blocked_by({"new_entries": {"enabled": False}}, Chain.BSC, "manual") == "NEW ENTRIES OFF"
    bsc_off = {"chain:bsc": {"enabled": False}}
    assert controls.blocked_by(bsc_off, Chain.BSC, "copy") == "BSC OFF"
    assert controls.blocked_by(bsc_off, Chain.SOLANA, "sniper") is None
    sniper_off = {"sniper": {"enabled": False}}
    assert controls.blocked_by(sniper_off, Chain.SOLANA, "sniper") == "SNIPER OFF"
    assert controls.blocked_by(sniper_off, Chain.SOLANA, "manual") is None  # operator trades are not sniping
    assert controls.blocked_by({"copy": {"enabled": False}}, Chain.ROBINHOOD, "copy") == "COPY TRADING OFF"
    assert controls.launchpad_mode({}, "pumpfun", Chain.SOLANA) == "LIVE"
    assert controls.launchpad_mode({}, "flap", Chain.BSC) == "PAPER"
    assert controls.launchpad_mode({"launchpad:flap": {"enabled": False}}, "flap", Chain.BSC) == "OFF"


def test_gate_refuses_new_entries_when_a_switch_is_off():
    inp = healthy()
    from dataclasses import replace
    inp.account = replace(inp.account, trading_blocked_by="SOLANA OFF")
    a = assess(inp, SafetySettings())
    f = next(f for f in a.findings if f.code == "TRADING_CONTROL_OFF")
    assert f.action == FinalDecision.NO_TRADE and "SOLANA OFF" in f.message and not a.executable
