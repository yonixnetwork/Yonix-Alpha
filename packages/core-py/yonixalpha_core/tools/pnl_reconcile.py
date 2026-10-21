"""PnL reconciliation (read-only): what each LIVE position's SOL was spent
on, which part is a recoverable deposit, and whether the books agree.
Nothing is written; no RPC call (for the on-chain view of empty token
accounts use tools.cost_report).

    $C exec -T api python -m yonixalpha_core.tools.pnl_reconcile [--engine solana_momentum] [--last 40]

For each LIVE position, from the confirmed transaction's own breakdown
(plan.fill.costs_sol, written by execution_analysis):
  consumed   program fees (protocol / creator / LP) + network fee (incl. priority)
  deposit    the token account's rent: SOL moved into an account the wallet
             owns, returned when the empty account is closed (not a cost)
  reclaimed  rent credited back after the account was closed (plan.fill.rent_reclaimed_sol)
  locked     deposit not reclaimed yet: SOL tied up in an empty token account
  booked     realized PnL as stored (proceeds - everything the wallet paid,
             re-booked when the rent came back)
  economic   booked + locked: the result once the account is closed (minus
             the closing transaction's ~0.000005 SOL fee)
  MFE        best price after entry, against the MARKET fill price and against
             the cost basis (the cost-basis figure includes the deposit and fees,
             which is why small LIVE buys looked like they "never went up")

PAPER positions model the same items as estimates (paper_engine.paper_fixed_fees:
buy / sell network fees and the close transaction or unreclaimed rent); their
totals are listed apart, labelled estimates.

Paper accounts: starting balance + realized PnL of positions closed since
the reset - cost of positions still open (+ partial proceeds) should equal
the cash balance; the difference is printed (0 when the books agree). The
LIVE account's cash is the synced wallet balance, so it is shown, not checked.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
from decimal import Decimal
from typing import Any

from sqlalchemy import text

from yonixalpha_core.config import get_settings
from yonixalpha_core.db.base import make_engine, make_session_factory
from yonixalpha_core.live_trading import LIVE_ACCOUNT, market_reference

POSITIONS_SQL = """
SELECT id, execution_mode, engine, symbol, status, entry_at, exit_at, entry_price, highest_price, lowest_price,
       entry_cost_quote, proceeds_quote, fees_paid_quote, realized_pnl, plan, account_id
FROM paper_positions
WHERE engine LIKE 'solana%' AND (CAST(:engine AS text) IS NULL OR engine = :engine)
ORDER BY entry_at DESC LIMIT :limit
"""
ACCOUNTS_SQL = """
SELECT a.id, a.name, a.starting_balance, a.cash_balance, a.reset_at,
       coalesce(sum(p.realized_pnl) FILTER (WHERE p.status = 'closed' AND p.created_at >= a.reset_at), 0) realized,
       coalesce(sum(p.entry_cost_quote) FILTER (WHERE p.status <> 'closed' AND p.created_at >= a.reset_at
                                                AND p.entry_cost_quote IS NOT NULL), 0) open_cost,
       coalesce(sum(p.proceeds_quote) FILTER (WHERE p.status <> 'closed' AND p.created_at >= a.reset_at), 0) open_proceeds,
       count(p.id) FILTER (WHERE p.status NOT IN ('open', 'closed') AND p.created_at >= a.reset_at) other
FROM paper_accounts a LEFT JOIN paper_positions p ON p.account_id = a.id
GROUP BY a.id ORDER BY a.name
"""


def _d(v: Any) -> Decimal | None:
    try:
        return Decimal(str(v)) if v not in (None, "") else None
    except Exception:  # noqa: BLE001 - a stored non-number is treated as missing
        return None


def breakdown(r: Any) -> dict[str, Any]:
    """The classified amounts of one position (SOL; None = not recorded)."""
    plan = r.plan or {}
    fill = plan.get("fill") or {}
    c = fill.get("costs_sol") or {}
    program, network = _d(c.get("program_fees_lamports")), _d(c.get("network_fee_lamports"))
    deposit = _d(c.get("token_account_rent_lamports"))
    reclaimed = _d(fill.get("rent_reclaimed_sol"))
    locked = None
    if deposit is not None and r.status == "closed":
        locked = Decimal(0) if reclaimed is not None else deposit
    booked = _d(r.realized_pnl)
    ref = market_reference(r)
    mfe_market = (ref["high"] / ref["entry"] - 1) if ref.get("entry") and ref.get("high") else None
    mfe_cost = (r.highest_price / r.entry_price - 1) if r.highest_price and r.entry_price else None
    return {"consumed": (program or 0) + (network or 0) if (program is not None or network is not None) else None,
            "program": program, "network": network, "deposit": deposit, "reclaimed": reclaimed, "locked": locked,
            "booked": booked, "economic": (booked + locked) if booked is not None and locked is not None else booked,
            "mfe_market": mfe_market, "mfe_cost_basis": mfe_cost, "mfe_measured": ref.get("high_measured", True),
            "cost": _d(r.entry_cost_quote)}


def _f(v: Decimal | None, n: int = 6) -> str:
    return "-" if v is None else f"{v:+.{n}f}"


def _p(v: Decimal | None) -> str:
    return "-" if v is None else f"{v * 100:+.1f}%"


async def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="PnL reconciliation, read-only")
    ap.add_argument("--engine", default=None, help="e.g. solana_momentum (default: every Solana engine)")
    ap.add_argument("--last", type=int, default=40, help="positions listed one by one (totals use up to 5000)")
    args = ap.parse_args(argv)
    settings = get_settings()
    engine = make_engine(settings, pool_size=1, max_overflow=0)
    sf = make_session_factory(engine)
    try:
        async with sf() as s:
            await report(s, args.engine, args.last)
    finally:
        await engine.dispose()
    return 0


async def report(s, engine_name: str | None, last: int) -> None:
    rows = (await s.execute(text(POSITIONS_SQL), {"engine": engine_name, "limit": 5000})).all()
    accounts = (await s.execute(text(ACCOUNTS_SQL))).all()

    live = [(r, breakdown(r)) for r in rows if (r.execution_mode or "PAPER") == "LIVE"]
    print(f"PNL RECONCILIATION ({engine_name or 'all Solana engines'}): {len(live)} LIVE, {len(rows) - len(live)} PAPER positions")
    print("\nLIVE positions, newest first (SOL)")
    print(f"  {'entry':<16} {'engine':<17} {'symbol':<10} {'status':<8} {'cost':>9} {'consumed':>9} {'deposit':>9} "
          f"{'reclaimed':>9} {'locked':>9} {'booked':>10} {'economic':>10} {'MFE mkt':>8} {'MFE cost':>8}")
    for r, b in live[:last]:
        print(f"  {str(r.entry_at)[:16]:<16} {r.engine:<17} {(r.symbol or '')[:10]:<10} {r.status:<8} {_f(b['cost'], 5):>9} "
              f"{_f(b['consumed'], 5):>9} {_f(b['deposit'], 5):>9} {_f(b['reclaimed'], 5):>9} {_f(b['locked'], 5):>9} "
              f"{_f(b['booked']):>10} {_f(b['economic']):>10} {_p(b['mfe_market']):>8}{'' if b['mfe_measured'] else '*'} "
              f"{_p(b['mfe_cost_basis']):>8}")
    if any(not b["mfe_measured"] for _, b in live[:last]):
        print("  * filled before market fill prices were kept: MFE is against the planned entry price (a lower bound)")

    totals: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    unknown: dict[str, int] = defaultdict(int)
    for r, b in live:
        t = totals[r.engine]
        t["n"] += 1
        for k in ("cost", "consumed", "deposit", "reclaimed", "locked", "booked", "economic"):
            if b[k] is not None:
                t[k] += b[k]
        if b["deposit"] is None:
            unknown[r.engine] += 1
    print("\nLIVE totals per engine (closed and open; SOL)")
    for e, t in sorted(totals.items()):
        print(f"  {e:<18} n {int(t['n']):>4}  spent {t['cost']:.6f}  consumed {t['consumed']:.6f}  deposits {t['deposit']:.6f} "
              f"(reclaimed {t['reclaimed']:.6f}, still locked {t['locked']:.6f})  booked {t['booked']:+.6f}  "
              f"economic {t['economic']:+.6f}" + (f"  [{unknown[e]} without a cost breakdown]" if unknown[e] else ""))

    paper = defaultdict(lambda: defaultdict(Decimal))
    for r in rows:
        if (r.execution_mode or "PAPER") == "LIVE":
            continue
        t = paper[r.engine]
        t["n"] += 1
        t["fees"] += _d(r.fees_paid_quote) or 0
        t["booked"] += _d(r.realized_pnl) or 0
    if paper:
        print("\nPAPER totals per engine (ESTIMATES: simulated fills, modelled fees and rent)")
        for e, t in sorted(paper.items()):
            print(f"  {e:<18} n {int(t['n']):>4}  modelled fees {t['fees']:.6f}  booked {t['booked']:+.6f}")

    print("\nACCOUNTS: starting + realized since reset - open cost + open proceeds vs cash")
    for a in accounts:
        if a.name == LIVE_ACCOUNT:
            print(f"  {a.name:<14} cash {a.cash_balance:.6f} = the synced wallet balance (SOL sent to or taken from the wallet "
                  "outside the bot is not in the ledger, so this account has no identity check)")
            continue
        expected = a.starting_balance + a.realized - a.open_cost + a.open_proceeds
        diff = a.cash_balance - expected
        print(f"  {a.name:<14} start {a.starting_balance:.6f}  cash {a.cash_balance:.6f}  realized {a.realized:+.6f}  "
              f"open cost {a.open_cost:.6f}  difference {diff:+.9f}"
              + (f"  ({a.other} position(s) pending / needing review are not in the sum)" if a.other else ""))
    print("\nNothing was written. On-chain view of empty token accounts: tools.cost_report (run in paper-trading).")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
