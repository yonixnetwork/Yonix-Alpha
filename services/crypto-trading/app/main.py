"""Crypto Trading worker (Bybit / Binance spot, DEMO only).

It runs only while there is work: the engine is switched ON in the dashboard
(Crypto Trading > Strategy Lab) or a demo position is open. Otherwise it
exits at once with status 0 and uses no memory; compose restarts it only
after a failure. The switch lives in the crypto schema, not in .env, so
turning it on never changes the configuration of any Memecoin service."""

from __future__ import annotations

import sys


def main() -> int:
    from yonixalpha_crypto.engine import main as run

    run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
