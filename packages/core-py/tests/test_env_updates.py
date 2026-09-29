"""Dashboard key updates: validation, the .env merge, and the host helper
(scripts/apply-env-requests.py) run end to end against a temporary .env with
`docker compose` replaced by `true`."""

import json
import os
import subprocess
import sys
from pathlib import Path

from yonixalpha_core import env_updates

REPO = Path(__file__).resolve().parents[3]
HELPER = REPO / "scripts" / "apply-env-requests.py"


def test_only_provider_keys_are_editable():
    assert env_updates.validate("HELIUS_API_KEY", "abc-123") is None
    assert env_updates.validate("HELIUS_API_KEY", "") is None  # clearing is allowed
    for locked in ("WALLET_PRIVATE_KEY", "TRADING_ENABLED", "LIVE_TRADING_ENABLED", "PAPER_TRADING", "ADMIN_PASSWORD_HASH",
                   "JWT_SECRET", "EVM_WALLET_PRIVATE_KEY", "EVM_WALLET_ADDRESS", "DATABASE_URL"):
        err = env_updates.validate(locked, "x")
        assert err and "server only" in err, locked
    # Keys of the removed futures / FX venues are no longer settable at all.
    for gone in ("SOMETHING_ELSE", "BINANCE_API_KEY", "BYBIT_API_KEY", "HYPERLIQUID_API_WALLET_PRIVATE_KEY", "MT5_BRIDGE_URL"):
        assert "cannot be changed" in env_updates.validate(gone, "x"), gone


def test_values_are_checked():
    assert "spaces" in env_updates.validate("HELIUS_API_KEY", "abc def")
    assert "spaces" in env_updates.validate("HELIUS_API_KEY", "abc\nTRADING_ENABLED=true")
    assert "quotes" in env_updates.validate("JUPITER_API_KEY", '"abc"')
    assert "https://" in env_updates.validate("SOLANA_RPC_URL", "http://x.example")
    assert env_updates.validate("SOLANA_WS_URL", "wss://mainnet.helius-rpc.com/?api-key=k") is None
    assert "number" in env_updates.validate("TELEGRAM_CHAT_ID", "abc")
    assert env_updates.validate("TELEGRAM_CHAT_ID", "-1003701096088") is None
    assert env_updates.validate("BSC_RPC_URLS", "https://a.example,https://b.example") is None
    assert env_updates.validate("BSC_RPC_URLS", "https://a.example,http://b.example")
    assert env_updates.validate_all({}) == ["no changes given"]


def test_merge_replaces_dedupes_appends_and_escapes():
    text = "# comment\nHELIUS_API_KEY=old\nOTHER=1\nHELIUS_API_KEY=dup\n"
    out = env_updates.merge_env(text, {"HELIUS_API_KEY": "new", "JUPITER_API_KEY": "a$b"})
    assert out == "# comment\nHELIUS_API_KEY=new\nOTHER=1\nJUPITER_API_KEY=a$$b\n"
    assert env_updates.merge_env("#HELIUS_API_KEY=x\n", {"HELIUS_API_KEY": "y"}) == "#HELIUS_API_KEY=x\nHELIUS_API_KEY=y\n"


def test_request_ids_are_strict(tmp_path):
    rid, _ = env_updates.new_request({"HELIUS_API_KEY": "k"}, "admin")
    assert env_updates.result_path(str(tmp_path), rid).endswith(f"res-{rid}.json")
    for bad in ("../x", "abc", "0" * 31 + "/"):
        try:
            env_updates.result_path(str(tmp_path), bad)
        except ValueError:
            continue
        raise AssertionError(bad)


def _run_helper(tmp_path: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "ENV_FILE": str(tmp_path / ".env"), "ENV_SPOOL": str(tmp_path / "spool"),
           "ENV_BACKUP_DIR": str(tmp_path / "backups"), "ENV_COMPOSE_CMD": "true", "ENV_SPOOL_UID": str(os.getuid())}
    return subprocess.run([sys.executable, str(HELPER)], env=env, capture_output=True, text=True, timeout=60)


def _queue(spool: Path, updates: dict) -> str:
    rid, req = env_updates.new_request(updates, "admin")
    env_updates.write_private(env_updates.request_path(str(spool), rid), req)
    return rid


def test_helper_applies_a_valid_request(tmp_path):
    (tmp_path / ".env").write_text("HELIUS_API_KEY=old-key\nTRADING_ENABLED=false\n")
    os.chmod(tmp_path / ".env", 0o600)
    spool = tmp_path / "spool"
    spool.mkdir()
    rid = _queue(spool, {"HELIUS_API_KEY": "brand-new-key", "TELEGRAM_CHAT_ID": "-100123"})
    proc = _run_helper(tmp_path)
    assert proc.returncode == 0, proc.stderr
    env = (tmp_path / ".env").read_text()
    assert "HELIUS_API_KEY=brand-new-key" in env and "TELEGRAM_CHAT_ID=-100123" in env and "TRADING_ENABLED=false" in env
    assert oct((tmp_path / ".env").stat().st_mode)[-3:] == "600"
    res = json.loads((spool / f"res-{rid}.json").read_text())
    assert res["status"] == "APPLIED" and res["keys"] == ["HELIUS_API_KEY", "TELEGRAM_CHAT_ID"]
    assert not (spool / f"req-{rid}.json").exists()
    backups = list((tmp_path / "backups").iterdir())
    assert len(backups) == 1 and "old-key" in backups[0].read_text() and oct(backups[0].stat().st_mode)[-3:] == "600"
    # Values are never printed or put in the result.
    assert "brand-new-key" not in proc.stdout + proc.stderr and "brand-new-key" not in json.dumps(res)


def test_helper_rejects_a_forbidden_request_without_touching_env(tmp_path):
    original = "WALLET_PRIVATE_KEY=keep\nTRADING_ENABLED=false\n"
    (tmp_path / ".env").write_text(original)
    spool = tmp_path / "spool"
    spool.mkdir()
    # A request the api would never write (e.g. forged inside a compromised container).
    rid = _queue(spool, {"TRADING_ENABLED": "true", "WALLET_PRIVATE_KEY": "attacker"})
    proc = _run_helper(tmp_path)
    assert proc.returncode == 0
    assert (tmp_path / ".env").read_text() == original
    res = json.loads((spool / f"res-{rid}.json").read_text())
    assert res["status"] == "REJECTED" and any("server only" in e for e in res["errors"])
    assert not (tmp_path / "backups").exists()


def test_helper_reports_a_failed_restart(tmp_path):
    (tmp_path / ".env").write_text("HELIUS_API_KEY=old\n")
    spool = tmp_path / "spool"
    spool.mkdir()
    rid = _queue(spool, {"HELIUS_API_KEY": "new"})
    env = {**os.environ, "ENV_FILE": str(tmp_path / ".env"), "ENV_SPOOL": str(spool),
           "ENV_BACKUP_DIR": str(tmp_path / "backups"), "ENV_COMPOSE_CMD": "false", "ENV_SPOOL_UID": str(os.getuid())}
    subprocess.run([sys.executable, str(HELPER)], env=env, capture_output=True, text=True, timeout=60, check=True)
    res = json.loads((spool / f"res-{rid}.json").read_text())
    assert res["status"] == "APPLIED — RESTART FAILED" and res["compose_exit_code"] != 0
