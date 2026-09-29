"""Dashboard updates of provider API keys, applied to the server's .env.

Standard library only: the host-side helper (scripts/apply-env-requests.py,
run by systemd as root outside the containers) imports this module straight
from the repository.

Flow:
1. The api (after the admin re-enters the password) validates the new
   values and writes one request file into a spool directory. That
   directory is the only thing the api container can write on the host; it
   never reads .env and has no access to Docker.
2. A systemd path unit notices the request. The helper validates it again
   with the same rules, backs up .env, merges the values in, and runs
   `docker compose up -d`, which recreates only the services whose
   environment changed.
3. The helper writes a result file (key names and status, never values),
   which the dashboard polls.

Only provider API keys, tokens and URLs are editable this way. These stay
server-only (scripts/set-keys.sh): wallet private keys, the admin password,
JWT/DB/Redis secrets, the testnet flags and the trading locks. A dashboard
session must never be able to open live trading or move the wallet.
"""

import json
import os
import re
import uuid
from datetime import datetime, timezone

# key -> kind: "secret" (never shown), "url" (https:// or wss://), "text"
EDITABLE_KEYS: dict[str, str] = {
    "HELIUS_API_KEY": "secret",
    "SOLANA_RPC_URL": "url",
    "SOLANA_WS_URL": "url",
    "SOLANA_RPC_BACKUP_URL": "url",
    "SOLANA_RPC_BACKUP_URL_2": "url",
    "SOLANA_RPC_BACKUP_URL_3": "url",
    "SOLANA_WS_BACKUP_URL": "url",
    "JUPITER_API_KEY": "secret",
    "PUMPPORTAL_API_KEY": "secret",
    "TELEGRAM_BOT_TOKEN": "secret",
    "TELEGRAM_CHAT_ID": "text",
    "BSC_RPC_URLS": "url_list",
    "ROBINHOOD_RPC_URLS": "url_list",
}

# Never editable from the dashboard, whatever a request says.
SERVER_ONLY = ("WALLET_PRIVATE_KEY", "WALLET_PUBLIC_KEY", "EVM_WALLET_PRIVATE_KEY", "EVM_WALLET_ADDRESS", "ADMIN_PASSWORD_HASH",
               "ADMIN_USERNAME", "JWT_SECRET", "POSTGRES_PASSWORD", "DATABASE_URL", "REDIS_PASSWORD", "REDIS_URL",
               "TRADING_ENABLED", "LIVE_TRADING_ENABLED", "PAPER_TRADING", "LIVE_SMOKE_TEST_ENABLED", "LIVE_SMOKE_TEST_MAX_SOL", "LIVE_SMOKE_TEST_MAX_TRADES")

MAX_VALUE_LENGTH = 2048
REQUEST_PREFIX, RESULT_PREFIX = "req-", "res-"
_ID = re.compile(r"^[0-9a-f]{32}$")


def validate(key: str, value: object) -> str | None:
    """An error message, or None when `value` may be written for `key`.
    An empty value clears the key."""
    if key not in EDITABLE_KEYS:
        where = " (server only: scripts/set-keys.sh)" if key in SERVER_ONLY else ""
        return f"{key} cannot be changed from the dashboard{where}"
    if not isinstance(value, str):
        return f"{key}: value must be text"
    if len(value) > MAX_VALUE_LENGTH:
        return f"{key}: longer than {MAX_VALUE_LENGTH} characters"
    if any(ch.isspace() or ch == "\0" for ch in value):
        return f"{key}: contains spaces or line breaks — keys never do; copy it again"
    if value[:1] in ("'", '"'):
        return f"{key}: remove the surrounding quotes"
    kind = EDITABLE_KEYS[key]
    if value and kind == "url" and not re.match(r"^(https|wss)://[^/]+", value):
        return f"{key}: must start with https:// or wss://"
    if value and kind == "url_list" and not all(re.match(r"^https://[^/,]+", u) for u in value.split(",")):
        return f"{key}: comma-separated https:// URLs"
    if value and key == "TELEGRAM_CHAT_ID" and not re.match(r"^-?\d+$", value):
        return f"{key}: must be a number (group chats start with -100)"
    return None


def validate_all(updates: object) -> list[str]:
    if not isinstance(updates, dict) or not updates:
        return ["no changes given"]
    if len(updates) > len(EDITABLE_KEYS):
        return ["too many keys"]
    return [e for k, v in updates.items() if (e := validate(k, v))]


def merge_env(text: str, updates: dict[str, str]) -> str:
    """`text` (a .env file) with each key set to its new value: the first
    `KEY=` line is replaced, later duplicates dropped, missing keys appended.
    Comments and every other line are kept. `$` is doubled, as compose's
    env_file interpolation expects (same as scripts/set-keys.sh)."""
    done: set[str] = set()
    out: list[str] = []
    for line in text.splitlines():
        key = line.split("=", 1)[0] if "=" in line and not line.lstrip().startswith("#") else None
        if key in updates:
            if key not in done:
                out.append(f"{key}={updates[key].replace('$', '$$')}")
                done.add(key)
            continue
        out.append(line)
    for key, value in updates.items():
        if key not in done:
            out.append(f"{key}={value.replace('$', '$$')}")
    return "\n".join(out) + "\n"


def new_request(updates: dict[str, str], user: str) -> tuple[str, dict]:
    rid = uuid.uuid4().hex
    return rid, {"id": rid, "created_at": datetime.now(timezone.utc).isoformat(), "user": user, "updates": updates}


def write_private(path: str, data: dict) -> None:
    """Atomic, owner-only (0600) JSON file."""
    tmp = f"{path}.tmp-{uuid.uuid4().hex}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def check_id(rid: str) -> str:
    if not isinstance(rid, str) or not _ID.match(rid):
        raise ValueError("bad request id")
    return rid


def request_path(spool: str, rid: str) -> str:
    return os.path.join(spool, f"{REQUEST_PREFIX}{check_id(rid)}.json")


def result_path(spool: str, rid: str) -> str:
    return os.path.join(spool, f"{RESULT_PREFIX}{check_id(rid)}.json")
