"""Read-only check of the public market data the Crypto Trading engine
uses, from wherever it runs (the server's location decides what answers).

    python -m yonixalpha_crypto.tools.check_exchanges [--exchange bybit] [--symbols BTC/USDT,ETH/USDT]

For each exchange: reachable or not (HTTP status), and for each symbol:
listed, trading status, tick size, quantity step, minimum order value, the
newest 5m candles and the current bid / ask. Public GET requests only, no
key, nothing written anywhere."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone

from yonixalpha_crypto.candles import clean
from yonixalpha_crypto.config import load_settings
from yonixalpha_crypto.exchanges import EXCHANGES, PROFILES, market_data


async def check(exchanges: list[str], symbols: list[str]) -> list[str]:
    lines = [f"PUBLIC MARKET DATA CHECK {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC (read only)"]
    overrides = dict(load_settings().base_urls)
    for ex in exchanges:
        md = market_data(ex, overrides.get(ex))
        p = PROFILES[ex]
        lines.append(f"\n== {p.label}  {overrides.get(ex) or p.base_url}")
        lines.append(f"   fees: maker {p.maker_fee_bps} bps / taker {p.taker_fee_bps} bps  [{p.fee_source}]")
        try:
            await md.ping()
            lines.append("   reachable: YES")
        except Exception as e:  # noqa: BLE001
            lines.append(f"   reachable: NO  ({e})"[:300])
            await md.aclose()
            continue
        for sym in symbols:
            try:
                spec = await md.instrument(sym)
                cs = clean(await md.candles(sym, "5m", 5), "5m", datetime.now(timezone.utc))
                book = await md.top_of_book(sym)
                last = cs.candles[-1] if cs.candles else None
                lines.append(f"   {sym:<9} {spec.status:<8} tick {spec.tick_size} step {spec.qty_step} "
                             f"min value {spec.min_notional} | bid {book.bid} ask {book.ask} "
                             f"spread {book.spread_bps:.2f} bps | last 5m close {last.close if last else '-'}"
                             f"{' STALE' if cs.stale else ''}")
            except Exception as e:  # noqa: BLE001
                lines.append(f"   {sym:<9} NOT AVAILABLE ({e})"[:300])
        await md.aclose()
    lines.append("\nNothing was written. No key was used.")
    return lines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--exchange", choices=EXCHANGES, action="append")
    ap.add_argument("--symbols", default="BTC/USDT,ETH/USDT,SOL/USDT")
    a = ap.parse_args(argv)
    print("\n".join(asyncio.run(check(a.exchange or list(EXCHANGES), [s.strip().upper() for s in a.symbols.split(",")]))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
