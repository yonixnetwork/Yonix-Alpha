"""Live activation status: each reason the switch or a live order is refused
is named, the locks are never opened, and no secret appears in the result."""

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

from pydantic import SecretStr
from solders.keypair import Keypair

from yonixalpha_core.live_activation import evaluate

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
SECRET = json.dumps(list(bytes(Keypair())))
OPEN = dict(TRADING_ENABLED=True, LIVE_TRADING_ENABLED=True, PAPER_TRADING=False)
CLOSED = dict(TRADING_ENABLED=True, LIVE_TRADING_ENABLED=False, PAPER_TRADING=True)
READY = {"status": "ready", "reason": None, "at": NOW.isoformat(), "wallet_max_age_seconds": "120"}
SYNCED = {"sol": "0.5", "at": (NOW - timedelta(seconds=10)).isoformat()}


def env(locks=OPEN, key=SECRET, rpc="https://rpc.example"):
    return SimpleNamespace(**locks, WALLET_PRIVATE_KEY=SecretStr(key) if key else None, WALLET_PUBLIC_KEY=None,
                           SOLANA_RPC_URL=rpc)


def run(settings, *, global_mode="PAPER", modes=None, ctl=None, kill=False, worker=READY, wallet=SYNCED,
        reserve=Decimal("0.05"), errors=(), eligible=0):
    return evaluate(settings, global_mode=global_mode, strategy_modes=modes or {"solana_fresh": "AUTO"}, ctl=ctl or {},
                    kill_engaged=kill, kill_reason="manual" if kill else None, worker=worker, wallet_sync=wallet,
                    reserve=reserve, config_errors=list(errors), eligible_recent=eligible, now=NOW)


def codes(r):
    return [b["code"] for b in r["blockers"]]


def test_closed_env_locks_block_the_switch_and_name_each_lock():
    r = run(env(CLOSED), worker={"status": "disabled", "reason": "environment locks closed"})
    assert r["state"] == "LOCKED" and r["can_activate"] is False
    b = r["blockers"][0]
    assert b["code"] == "ENV_LOCKS_CLOSED" and b["category"] == "LOCKED_BY_SAFETY_POLICY" and b["blocks_activation"]
    assert "LIVE_TRADING_ENABLED=true" in b["message"] and "PAPER_TRADING=false" in b["message"]
    assert "TRADING_ENABLED=true" not in b["message"].replace("LIVE_TRADING_ENABLED=true", "")
    # a worker "disabled" by the closed locks is by design, not an extra blocker
    assert codes(r) == ["ENV_LOCKS_CLOSED"]


def test_everything_ready_means_ready_to_activate_and_then_active():
    assert run(env())["state"] == "READY_TO_ACTIVATE" and run(env())["blockers"] == []
    live = run(env(), global_mode="LIVE", eligible=0)
    assert live["state"] == "ACTIVE" and live["live_orders_possible"] is True
    assert "no token currently qualifies" in live["summary"]  # active switch, no trade due
    assert "3 decision(s)" in run(env(), global_mode="LIVE", eligible=3)["summary"]


def test_switch_accepted_but_orders_blocked_is_reported_as_not_ready():
    r = run(env(), global_mode="LIVE", worker=None)
    assert r["state"] == "ACTIVE_NOT_READY" and r["live_orders_possible"] is False
    assert codes(r) == ["WORKER_NOT_RUNNING"] and r["blockers"][0]["category"] == "PROVIDER_DISCONNECTED"
    assert run(env(), worker=None)["state"] == "READY_TO_ACTIVATE"  # the switch itself is not refused


def test_stale_container_after_editing_env_is_detected_both_ways():
    r = run(env(), worker={"status": "disabled", "reason": "environment locks closed"})
    assert codes(r) == ["STALE_DEPLOYMENT"]
    r2 = run(env(CLOSED), worker=READY)
    assert "STALE_DEPLOYMENT" in codes(r2) and r2["state"] == "LOCKED"


def test_wallet_signing_rpc_and_balance_reasons():
    assert "WALLET_NOT_CONFIGURED" in codes(run(env(key=None)))
    bad = run(env(key="[1,2,3]"))
    assert "WALLET_KEY_INVALID" in codes(bad) and bad["blockers"][0]["category"] == "SIGNING_UNAVAILABLE"
    assert "RPC_NOT_CONFIGURED" in codes(run(env(rpc=None)))
    assert codes(run(env(), wallet=None)) == ["WALLET_NOT_SYNCED"]
    low = run(env(), wallet={"sol": "0.05", "at": NOW.isoformat()})
    assert codes(low) == ["BALANCE_AT_RESERVE"] and low["blockers"][0]["category"] == "INSUFFICIENT_BALANCE"
    old = run(env(), wallet={"sol": "1", "at": (NOW - timedelta(minutes=10)).isoformat()})
    assert codes(old) == ["WALLET_SYNC_STALE"]
    signer = run(env(), worker={"status": "disabled", "reason": "WALLET_PRIVATE_KEY not configured"})
    assert codes(signer) == ["WORKER_NO_SIGNER"]


def test_policy_switches_and_strategy_modes():
    r = run(env(), kill=True, ctl={"new_entries": {"enabled": False}},
            modes={"solana_fresh": "PAPER", "solana_migration": "OFF", "solana_momentum": "PAPER"})
    assert {"KILL_SWITCH", "SWITCH_OFF:new_entries", "NO_STRATEGY_LIVE"} <= set(codes(r))
    assert next(b for b in r["blockers"] if b["code"] == "NO_STRATEGY_LIVE")["category"] == "STRATEGY_DISABLED"
    assert r["state"] == "READY_TO_ACTIVATE"  # none of these refuse the switch itself


def test_configuration_errors_refuse_the_switch_only_when_the_locks_are_open():
    r = run(env(), errors=["solana_fresh: WALLET_PRIVATE_KEY is not set"])
    assert r["state"] == "LOCKED" and "CONFIGURATION_ERROR" in codes(r)


def test_no_secret_in_the_result():
    out = json.dumps(run(env(), global_mode="LIVE"), default=str)
    assert SECRET not in out and SECRET[1:20] not in out
