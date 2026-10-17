"""Memecoin pause (operator request 2026-10-17): while runtime/memecoin-paused
exists, nothing on the server may start a Memecoin worker again (deploy.sh,
the dashboard key updater, a plain `docker compose up -d`), and
scripts/memecoin.sh refuses to pause while LIVE positions or orders are
active (stopping paper-trading would leave them without exits)."""

import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
MEMECOIN = ("data-solana", "engine-solana-discovery", "decision-engine", "ml", "paper-trading")
ALWAYS_ON = ("postgres", "redis", "api", "web", "crypto-trading")


def _block(text: str, svc: str) -> str:
    return text.split(f"\n  {svc}:\n", 1)[1].split("\n  ", 1)[0]


def _profiles(env_text: str, runtime: Path) -> str:
    env = runtime / ".env"
    env.write_text(env_text)
    return subprocess.run(["bash", str(REPO / "scripts" / "active-profiles.sh"), str(env)], capture_output=True,
                          text=True, check=True, env={**os.environ, "YONIXALPHA_RUNTIME_DIR": str(runtime)}).stdout.strip()


def test_memecoin_workers_need_the_memecoin_profile_and_the_rest_do_not():
    text = (REPO / "infra" / "docker" / "docker-compose.yml").read_text()
    for svc in MEMECOIN:
        assert 'profiles: ["memecoin"]' in _block(text, svc), svc
    for svc in ALWAYS_ON:
        assert "profiles:" not in _block(text, svc), svc


def test_active_profiles_turn_memecoin_on_unless_paused(tmp_path):
    assert _profiles("", tmp_path) == "memecoin"
    assert _profiles("SYSTEM_PROFILE=MULTI_CHAIN\nCOPY_TRADING_ENABLED=true\n", tmp_path) == "memecoin,evm,copy"
    (tmp_path / "memecoin-paused").write_text("2026-10-17T12:00:00Z by root\n")
    assert _profiles("", tmp_path) == ""
    assert _profiles("SYSTEM_PROFILE=MULTI_CHAIN\nCOPY_TRADING_ENABLED=true\n", tmp_path) == ""  # evm / copy off too


def test_every_way_of_starting_services_uses_the_pause_aware_profiles():
    deploy = (REPO / "scripts" / "deploy.sh").read_text()
    assert "bash scripts/active-profiles.sh .env" in deploy
    assert all(f"{svc}:memecoin" in deploy for svc in MEMECOIN)  # removed by the rm -s -f loop when paused
    assert "runtime/memecoin-paused" in deploy
    assert "scripts/active-profiles.sh" in (REPO / "scripts" / "apply-env-requests.py").read_text()
    assert "scripts/active-profiles.sh" in (REPO / "scripts" / "set-keys.sh").read_text()


def _fake_docker(tmp_path: Path, live_positions: str) -> dict:
    """A `docker` on PATH that answers the script's read-only queries and
    reports no containers; every call is logged."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "docker.log"
    (bin_dir / "docker").write_text(f"""#!/bin/bash
echo "$*" >> {log}
case "$*" in
  *"execution_mode='LIVE'"*) echo {live_positions} ;;
  *"mode='LIVE'"*) echo 0 ;;
  *psql*) echo 2 ;;
esac
exit 0
""")
    (bin_dir / "docker").chmod(0o755)
    return {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "YONIXALPHA_RUNTIME_DIR": str(tmp_path / "rt")}


def test_pause_refuses_while_live_positions_are_open(tmp_path):
    env = _fake_docker(tmp_path, live_positions="1")
    r = subprocess.run(["bash", str(REPO / "scripts" / "memecoin.sh"), "pause"], capture_output=True, text=True, env=env)
    assert r.returncode == 1 and "REFUSED" in r.stderr and "CLOSE POSITIONS" in r.stderr
    assert not (tmp_path / "rt" / "memecoin-paused").exists()
    assert " rm " not in (tmp_path / "docker.log").read_text()  # nothing was stopped


def test_pause_writes_the_marker_and_reports_paper_positions_frozen(tmp_path):
    env = _fake_docker(tmp_path, live_positions="0")
    r = subprocess.run(["bash", str(REPO / "scripts" / "memecoin.sh"), "pause"], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "rt" / "memecoin-paused").exists()
    assert "2 PAPER" in r.stdout and "PAUSED" in r.stdout
