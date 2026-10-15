"""Crypto schema migrations, separate from the main Alembic chain.

    python -m yonixalpha_crypto.migrate upgrade     # to head
    python -m yonixalpha_crypto.migrate downgrade   # one step back
    python -m yonixalpha_crypto.migrate check       # models == migrations

Additive and reversible: the first revision creates the `crypto` schema and
its tables; downgrading it drops only those."""

from __future__ import annotations

import sys
from pathlib import Path

from alembic import command
from alembic.config import Config

from yonixalpha_crypto.config import load_settings


def alembic_config(database_url: str | None = None) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    cfg.set_main_option("sqlalchemy.url", (database_url or load_settings().database_url).replace("%", "%%"))
    return cfg


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    cmd = args[0] if args else "upgrade"
    cfg = alembic_config()
    if cmd == "upgrade":
        command.upgrade(cfg, "head")
    elif cmd == "downgrade":
        command.downgrade(cfg, args[1] if len(args) > 1 else "-1")
    elif cmd == "check":
        command.check(cfg)
    elif cmd == "current":
        command.current(cfg)
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
