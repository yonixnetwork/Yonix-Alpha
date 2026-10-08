"""System profile (yonixalpha_core.system_profile, 2026-10-08 Solana-first
production): which chains, workers and ML run, and that
scripts/compose-profiles.sh (used by deploy.sh on the host, without Python)
applies the same rules to .env as compose_profiles()."""

import asyncio
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from yonixalpha_core import events, operating_mode, system_profile
from yonixalpha_core.config import Settings

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "compose-profiles.sh"


def settings(**kw):
    return Settings(JWT_SECRET="x" * 32, ADMIN_PASSWORD_HASH="x", **kw)


def test_production_default_is_solana_only_without_copy_trading():
    s = settings()
    d = system_profile.describe(s)
    assert d["profile"] == "SOLANA_ONLY" and d["enabled_chains"] == ["solana"]
    assert d["compose_profiles"] == [] and d["copy_trading_enabled"] is False
    assert set(system_profile.disabled_services(s)) == {"data-evm", "copy-engine"}
    assert system_profile.disabled_reason(s, "data-evm").startswith("DISABLED — SOLANA_ONLY MODE")
    assert d["ml"] == {"dataset_scope": "SOLANA", "model_scope": "SOLANA",
                       "evm_ml": system_profile.disabled_reason(s, "evm")}
    assert system_profile.disabled_reason(s, "paper-trading") is None  # the Solana stack is never switched off


def test_solana_only_ignores_evm_chain_flags_and_multi_chain_reads_them():
    s = settings(CHAIN_BSC_ENABLED=True, CHAIN_SOLANA_ENABLED=False)
    assert system_profile.enabled_chains(s) == ["solana"]
    assert len(system_profile.describe(s)["notes"]) == 2  # both flags reported as not applied
    m = settings(SYSTEM_PROFILE="MULTI_CHAIN")
    assert system_profile.enabled_chains(m) == ["solana", "bsc", "robinhood"]  # unset = on
    assert system_profile.ml_scope(m) == "ALL" and system_profile.evm_ml_enabled(m)
    m = settings(SYSTEM_PROFILE="MULTI_CHAIN", CHAIN_BSC_ENABLED=False)
    assert system_profile.enabled_chains(m) == ["solana", "robinhood"] and system_profile.compose_profiles(m) == ["evm"]
    m = settings(SYSTEM_PROFILE="MULTI_CHAIN", CHAIN_BSC_ENABLED=False, CHAIN_ROBINHOOD_ENABLED=False,
                 COPY_TRADING_ENABLED=True)
    assert system_profile.compose_profiles(m) == ["copy"] and not system_profile.evm_ml_enabled(m)
    m = settings(SYSTEM_PROFILE="MULTI_CHAIN", ML_MODEL_SCOPE="SOLANA")
    assert not system_profile.evm_ml_enabled(m)
    assert system_profile.disabled_reason(m, "evm_ml") == "DISABLED: ML_MODEL_SCOPE / ML_DATASET_SCOPE = SOLANA"
    assert system_profile.profile(SimpleNamespace(SYSTEM_PROFILE="nonsense")) == "SOLANA_ONLY"


def test_position_engines_map_to_their_chain_and_orders_on_off_chains_are_refused():
    assert [system_profile.chain_of_engine(e) for e in ("evm_bsc", "evm_copy_robinhood", "solana_fresh", "copy_solana", None)] \
        == ["bsc", "robinhood", "solana", "solana", "solana"]
    no = system_profile.refusal(settings(), "bsc", "BUY")
    assert no["code"] == "CHAIN_DISABLED" and "BUY refused" in no["message"]
    assert system_profile.refusal(settings(), "solana", "SELL") is None
    assert system_profile.refusal(settings(SYSTEM_PROFILE="MULTI_CHAIN"), "bsc", "BUY") is None


def test_copy_trading_disabled_overrides_the_dashboard_status():
    assert operating_mode.effective_copy_status("NORMAL", "ACTIVE", enabled=False) == "SUSPENDED"
    assert operating_mode.effective_copy_status("NORMAL", "ACTIVE", enabled=True) == "ACTIVE"
    assert operating_mode.effective_copy_status("EMERGENCY", "ACTIVE", enabled=True) == "SUSPENDED"


ENV_CASES = [
    ("", {}),
    ("SYSTEM_PROFILE=SOLANA_ONLY\nCHAIN_BSC_ENABLED=true\nCOPY_TRADING_ENABLED=false\n", {}),
    ("SYSTEM_PROFILE=MULTI_CHAIN\n", {"SYSTEM_PROFILE": "MULTI_CHAIN"}),
    ('SYSTEM_PROFILE="multi_chain"\nCHAIN_BSC_ENABLED=false\nCOPY_TRADING_ENABLED=yes  # copy on\n',
     {"SYSTEM_PROFILE": "MULTI_CHAIN", "CHAIN_BSC_ENABLED": False, "COPY_TRADING_ENABLED": True}),
    ("SYSTEM_PROFILE=MULTI_CHAIN\nCHAIN_BSC_ENABLED=0\nCHAIN_ROBINHOOD_ENABLED=off\n",
     {"SYSTEM_PROFILE": "MULTI_CHAIN", "CHAIN_BSC_ENABLED": False, "CHAIN_ROBINHOOD_ENABLED": False}),
    ("SYSTEM_PROFILE=MULTI_CHAIN\nSYSTEM_PROFILE=SOLANA_ONLY\nCOPY_TRADING_ENABLED=1\n", {"COPY_TRADING_ENABLED": True}),
]


@pytest.mark.parametrize("env,kw", ENV_CASES)
def test_compose_profiles_script_matches_the_python_rules(tmp_path, env, kw):
    f = tmp_path / ".env"
    f.write_text(env)
    out = subprocess.run(["bash", str(SCRIPT), str(f)], capture_output=True, text=True, check=True).stdout.strip()
    assert out == ",".join(system_profile.compose_profiles(settings(**kw)))


def test_compose_file_puts_the_optional_workers_behind_their_profiles():
    text = (REPO / "infra" / "docker" / "docker-compose.yml").read_text()
    for svc, prof in system_profile.COMPOSE_PROFILE.items():
        block = text.split(f"\n  {svc}:\n", 1)[1].split("\n  ", 1)[0]
        assert block.strip() == f'profiles: ["{prof}"]', svc
    deploy = (REPO / "scripts" / "deploy.sh").read_text()
    assert "compose-profiles.sh" in deploy and "rm -s -f" in deploy
    # after pulling, deploy.sh runs the pulled version of itself (bash keeps reading the old file otherwise)
    pull, rest = deploy.split('exec bash "${REPO_ROOT}/scripts/deploy.sh" "$@"', 1)
    assert "git merge --ff-only" in pull and "DEPLOY_PULLED=1" in pull and "rm -s -f" in rest
    assert all(f"{svc}:{prof}" in deploy for svc, prof in system_profile.COMPOSE_PROFILE.items())


async def test_a_disabled_worker_started_anyway_only_heartbeats_disabled(monkeypatch):
    seen = []

    async def fake_loop(st, service, stop, detail_fn=None, status="ok"):
        seen.append((service, status, detail_fn()))

    monkeypatch.setattr(events, "heartbeat_loop", fake_loop)
    stop = asyncio.Event()
    stop.set()
    await system_profile.idle_while_disabled(settings(), "data-evm", "DISABLED — SOLANA_ONLY MODE", stop)
    assert seen == [("data-evm", "disabled", {"reason": "DISABLED — SOLANA_ONLY MODE", "profile": "SOLANA_ONLY"})]
    json.dumps(system_profile.describe(settings()))  # the API returns it as is
