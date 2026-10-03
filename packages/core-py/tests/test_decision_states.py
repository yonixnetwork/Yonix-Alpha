"""Master §76-77: the safety hierarchy decides, every decision is one of the
six states, and the record carries its evidence."""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from yonixalpha_core import decision_states as ds
from yonixalpha_core.chains.evm.paper import EVIDENCE_KEYS, EntryDecision, attach_evidence
from yonixalpha_core.chains.evm.rpc import EvmRpc
from yonixalpha_core.db.models import EvmToken
from yonixalpha_core.safety.models import AccountState

T = datetime(2026, 10, 4, 12, tzinfo=timezone.utc)


def b(*codes):
    return [{"code": c, "message": c.lower()} for c in codes]


def test_every_code_maps_to_a_known_layer_and_state_and_ml_has_none():
    for code, (layer, state) in ds.CODES.items():
        assert layer in ds.LAYERS and state in ds.STATES[2:], code
    assert not [c for c, (layer, _) in ds.CODES.items() if layer in ("ML", "EXECUTION")]
    assert ds.ML_EVIDENCE["contribution_pct"] == 0


@pytest.mark.parametrize("codes, decision, layer", [
    ((), ds.EXECUTE, "EXECUTION"),
    (("TOO_FEW_BUYERS",), ds.WAIT, "STRATEGY"),
    # a strategy (or a copy target, or ML) never outranks safety
    (("TOO_FEW_BUYERS", "SAFETY_NOT_PASSED"), ds.REJECT, "TOKEN_SAFETY"),
    (("MAX_OPEN_POSITIONS", "LIQUIDITY_TOO_LOW"), ds.WAIT, "LIQUIDITY_SAFETY"),
    # missing data decides first: NO_TRADE, never assumed safe (§70)
    (("SAFETY_NOT_PASSED", "SAFETY_STALE", "TOO_FEW_BUYS"), ds.NO_TRADE, "DATA_SAFETY"),
    (("GAS_PRICE_UNAVAILABLE", "ROUND_TRIP_EXCEEDS_STOP_BUDGET"), ds.NO_TRADE, "DATA_SAFETY"),
    (("COORDINATION_MANUAL_APPROVAL", "TOO_FEW_BUYS"), ds.MANUAL_APPROVAL, "TOKEN_SAFETY"),
    # within one layer the more restrictive state wins
    (("COORDINATION_MANUAL_APPROVAL", "SAFETY_NOT_PASSED"), ds.REJECT, "TOKEN_SAFETY"),
    (("INSUFFICIENT_GAS", "ROUND_TRIP_EXCEEDS_STOP_BUDGET"), ds.REJECT, "EXECUTION_SAFETY"),
    (("KILL_SWITCH", "CATEGORY_NOT_TRADED"), ds.NO_TRADE, "RISK"),
    (("SOME_NEW_PLAN_FINDING",), ds.NO_TRADE, "RISK"),  # unknown: never a pass
])
def test_the_highest_blocking_layer_decides(codes, decision, layer):
    r = ds.resolve(b(*codes))
    assert (r["decision"], r["layer"]) == (decision, layer)
    assert [x["code"] for x in r["blockers"]][:1] == ([r["reason"].split(":")[0]] if codes else [])
    assert sorted(x["code"] for x in r["blockers"]) == sorted(codes)


def test_reduced_size_is_its_own_state():
    assert ds.resolve([], size_reduced=True)["decision"] == ds.REDUCE_SIZE
    assert ds.resolve(b("TOO_FEW_BUYS"), size_reduced=True)["decision"] == ds.WAIT


def test_entry_decision_record_has_every_field_and_no_secret():
    row = EvmToken(chain="bsc", token="0x" + "1" * 40, launchpad="fourmeme", category="FRESH", stage="CURVE",
                   stats={"buys": 9, "unique_buyers": 7, "net_buy_ratio": "0.8", "window_s": 60, "ignored": 1},
                   state={"liquidity_quote": "4.2"}, safety_verdict="PASS", safety_at=T,
                   safety={"findings": [{"code": "SELL_SIMULATED"}]}, coordination={"action": "NONE"})

    class Ad:
        rpc = EvmRpc("bsc", 56, ["https://bsc.example.org/v1/SECRETKEY123"])

    d = EntryDecision("bsc", row.token, "fourmeme", "FRESH", T)
    d.block("TOO_FEW_BUYERS", "7 < 8")
    acct = AccountState(equity=Decimal(1), available_balance=Decimal("0.9"), open_positions=1,
                        current_exposure=Decimal("0.1"), daily_realized_pnl=Decimal(0), last_loss_at=None,
                        token_exposure=Decimal(0), kill_switch_engaged=False)
    attach_evidence(d, row, acct, Ad(), T, "sniper")
    rec = d.to_dict()
    assert rec["decision"] == ds.WAIT and rec["layer"] == "STRATEGY" and rec["reason"] == "TOO_FEW_BUYERS: 7 < 8"
    assert rec["at"] == T.isoformat() and all(k in rec for k in (*EVIDENCE_KEYS, "ml_evidence"))
    assert rec["features"]["buys"] == 9 and "ignored" not in rec["features"] and rec["features"]["liquidity_quote"] == "4.2"
    assert rec["safety_evidence"]["verdict"] == "PASS" and rec["safety_evidence"]["findings"] == ["SELL_SIMULATED"]
    assert rec["wallet_evidence"]["source"] == "sniper" and "never permission" in rec["wallet_evidence"]["note"]
    assert rec["risk"]["account"]["open_positions"] == 1 and rec["ml_evidence"]["contribution_pct"] == 0
    assert rec["provider_status"]["rpc"]["endpoints"][0]["url"] == "https://bsc.example.org/…"
    assert "SECRETKEY123" not in str(rec)


def test_no_plan_and_no_blocker_is_never_a_pass():
    rec = EntryDecision("bsc", "0x" + "2" * 40, "fourmeme", "FRESH", T).to_dict()
    assert rec["decision"] == ds.NO_TRADE and rec["blockers"][0]["code"] == "PLAN_INCOMPLETE"
