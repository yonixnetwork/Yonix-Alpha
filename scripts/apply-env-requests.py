#!/usr/bin/env python3
"""Applies API-key changes queued from the dashboard (Settings -> Providers).

Run by systemd as root on the server (installed by
scripts/install-env-updater.sh), never inside a container. For each request
file in the spool directory, oldest first:

1. validate it again, with the same rules as the api (editable provider
   keys only: no wallet keys, passwords, testnet flags or trading locks);
2. back up .env (mode 600) and merge the new values in;
3. run `docker compose ... up -d`, which recreates only the services whose
   environment changed;
4. write a result file (key names and status, never values) for the
   dashboard, then delete the request.

Values are never printed or logged.
"""

import glob
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "packages", "core-py"))

from yonixalpha_core import env_updates  # noqa: E402

ENV_FILE = os.environ.get("ENV_FILE", os.path.join(REPO, ".env"))
SPOOL = os.environ.get("ENV_SPOOL", os.path.join(REPO, "runtime", "env-requests"))
BACKUP_DIR = os.environ.get("ENV_BACKUP_DIR", "/opt/yonixalpha-backups/env")
COMPOSE = os.environ.get("ENV_COMPOSE_CMD", "docker compose --env-file .env -f infra/docker/docker-compose.yml "
                                            "-f infra/docker/docker-compose.prod.yml").split()
SPOOL_UID = int(os.environ.get("ENV_SPOOL_UID", "1000"))  # the api container's user, so it can read results


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _result(rid: str, data: dict) -> None:
    path = env_updates.result_path(SPOOL, rid)
    env_updates.write_private(path, {"id": rid, "finished_at": _now().isoformat(), **data})
    try:
        os.chown(path, SPOOL_UID, SPOOL_UID)
    except PermissionError:  # not root (tests)
        pass


def apply_one(path: str) -> str:
    rid = os.path.basename(path)[len(env_updates.REQUEST_PREFIX):-len(".json")]
    try:
        env_updates.check_id(rid)
    except ValueError:
        os.remove(path)
        return "SKIPPED"
    keys: list[str] = []
    try:
        with open(path) as f:
            req = json.load(f)
        updates = req.get("updates")
        keys = sorted(updates) if isinstance(updates, dict) else []
        errors = env_updates.validate_all(updates)
        if req.get("id") != rid:
            errors.append("request id mismatch")
    except (OSError, ValueError, AttributeError) as exc:
        req, updates, errors = {}, {}, [f"unreadable request: {type(exc).__name__}"]
    finally:
        os.remove(path)  # a request is applied at most once
    if errors:
        _result(rid, {"status": "REJECTED", "keys": keys, "errors": errors})
        print(f"request {rid}: REJECTED {errors}", flush=True)
        return "REJECTED"

    os.makedirs(BACKUP_DIR, mode=0o700, exist_ok=True)
    backup = os.path.join(BACKUP_DIR, f"env.{_now().strftime('%Y%m%dT%H%M%SZ')}.{rid[:8]}")
    shutil.copy2(ENV_FILE, backup)
    os.chmod(backup, 0o600)
    with open(ENV_FILE) as f:
        merged = env_updates.merge_env(f.read(), updates)
    with open(ENV_FILE, "w") as f:  # rewritten in place: keeps the file's owner and mode
        f.write(merged)

    try:
        proc = subprocess.run(COMPOSE + ["up", "-d"], cwd=REPO, capture_output=True, text=True, timeout=600)
        code, output = proc.returncode, proc.stdout + proc.stderr
    except (OSError, subprocess.TimeoutExpired) as exc:
        code, output = -1, f"{type(exc).__name__}: {exc}"
    status = "APPLIED" if code == 0 else "APPLIED — RESTART FAILED"
    _result(rid, {"status": status, "keys": keys, "user": req.get("user"), "backup": backup,
                  "compose_exit_code": code, "compose_output": output[-1500:]})
    print(f"request {rid}: {status} keys={keys} backup={backup}", flush=True)
    return status


def main() -> int:
    if not os.path.isdir(SPOOL):
        print(f"spool {SPOOL} missing — run scripts/install-env-updater.sh", file=sys.stderr)
        return 1
    pending = glob.glob(os.path.join(SPOOL, f"{env_updates.REQUEST_PREFIX}*.json"))
    for path in sorted(pending, key=os.path.getmtime):
        apply_one(path)
    # Results are polled for a few minutes; a day is plenty.
    cutoff = _now().timestamp() - 86400
    for old in glob.glob(os.path.join(SPOOL, f"{env_updates.RESULT_PREFIX}*.json")):
        if os.path.getmtime(old) < cutoff:
            os.remove(old)
    return 0


if __name__ == "__main__":
    sys.exit(main())
