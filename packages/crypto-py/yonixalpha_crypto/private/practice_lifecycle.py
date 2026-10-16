"""Practice-account order lifecycle test (run by the operator on the server).

    python -m yonixalpha_crypto.private.practice_lifecycle --connection <label or id> --symbol BTC/USDT --max-quote 20 --yes

Uses a DEMO / TESTNET connection saved in the dashboard (keys are read from
the encrypted store, never typed here) and REFUSES a LIVE connection. Every
step sends real requests to the exchange's official practice host and
reports PASS / FAIL / SKIP with the evidence the exchange returned:

 1. connection test (read-only: authentication, permissions);
 2. instrument rules, top of book, balances;
 3. a deliberately invalid order (below the minimum size) must be REJECTED;
 4. entry: risk-sized market buy (at most --max-quote USDT), then the
    protective stop (and target where the exchange holds it), confirmed open;
 5. the same signal again must be refused locally (no request sent);
 6. restart: a fresh executor reconciles from the exchange's state;
 7. tighten the stop (Bybit amends in place, Binance replaces) and confirm;
 8. partial exit of half (if both halves are above the minimum), re-protect;
 9. close: cancel protection (confirmed), market sell, CLOSED;
10. final reconciliation: nothing unresolved, entries not blocked.

Not testable on demand against a real exchange (covered by the simulated
exchanges in tests/test_execution.py instead): lost answers, dropped
requests, forced partial fills, protection removed by the exchange.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import or_, select

from yonixalpha_crypto.config import load_settings
from yonixalpha_crypto.db import ApiConnection, EngineState, Event, ExecGuard, ExecTrade, make_session_factory
from yonixalpha_crypto.private import connections
from yonixalpha_crypto.private.environments import LIVE
from yonixalpha_crypto.private.executor import EntryRefused, Executor
from yonixalpha_crypto.private.service import open_account
from yonixalpha_crypto.private.signedhttp import ExchangeRequestError
from yonixalpha_crypto.private.sizing import SizingRules


async def _kill_switch_engaged(redis_url: str) -> bool:
    """The platform kill switch (read only, never written here)."""
    from redis.asyncio import Redis

    from yonixalpha_crypto.engine import KILL_SWITCH_KEY
    r = Redis.from_url(redis_url, decode_responses=True)
    try:
        return bool(await r.get(KILL_SWITCH_KEY))
    finally:
        await r.aclose()


class Report:
    def __init__(self):
        self.rows: list[tuple[str, str, str]] = []

    def add(self, step: str, result: str, evidence: str) -> None:
        self.rows.append((step, result, evidence))
        print(f"[{result:4}] {step}: {evidence}", flush=True)


async def run(conn_ref: str, symbol: str, max_quote: Decimal, stop_pct: Decimal, *, transport=None, env=None,
              database_url: str | None = None) -> int:
    """`transport` / `env` exist for the tests (simulated exchanges); the
    operator's run uses the real practice host and the server's key."""
    settings = load_settings()
    engine, sf = make_session_factory(database_url or settings.database_url)
    rep = Report()
    try:
        async with sf() as s:
            try:
                cid = uuid.UUID(conn_ref)
                cond = ApiConnection.id == cid
            except ValueError:
                cond = ApiConnection.label == conn_ref
            rows = (await s.execute(select(ApiConnection).where(or_(cond)))).scalars().all()
            if len(rows) != 1:
                print(f"connection '{conn_ref}': {len(rows)} matches; use the id shown on the dashboard")
                return 2
            c = rows[0]
            if c.environment == LIVE:
                print("REFUSED: this tool runs on DEMO / TESTNET connections only; live orders are locked.")
                return 2
            eng = await s.get(EngineState, 1)
            if eng is not None and eng.emergency_stop:
                print("REFUSED: the Crypto Trading emergency stop is engaged.")
                return 2
            if transport is None and await _kill_switch_engaged(settings.redis_url):
                print("REFUSED: the platform kill switch is engaged.")
                return 2
            print(f"connection {c.exchange} {c.environment} '{c.label}' ({c.id}), host from the fixed environment table")

            out = await connections.test(s, c, transport=transport, env=env)
            await s.commit()
            perms = out.get("permissions") or {}
            rep.add("1 connection test", "PASS" if out["ok"] else "FAIL",
                    f"status {out['status']}; spot trading {perms.get('can_trade_spot')}; withdraw "
                    f"{perms.get('withdraw_enabled')}; error {out.get('error')}")
            if not out["ok"]:
                return 1
            if not c.enabled:
                c.enabled = True
                await s.commit()
                rep.add("1b enable", "PASS", "practice connection enabled for this test")

        acct = open_account(c, transport=transport, env=env)
        if acct is None:
            rep.add("2 keys", "FAIL", "saved keys cannot be decrypted")
            return 1
        ex = Executor(acct, c)
        try:
            spec = await acct.instrument(symbol)
            book = await acct.book(symbol)
            bal = await acct.balances()
            rep.add("2 market and account", "PASS",
                    f"{spec.venue_symbol} {spec.status}, tick {spec.tick_size}, step {spec.qty_step}, min qty "
                    f"{spec.min_qty}, min value {spec.min_notional}; bid {book.bid} ask {book.ask}; balances "
                    + ", ".join(f"{a} {b.total}" for a, b in bal.items() if a in (spec.base, spec.quote)))

            try:
                await acct.market(symbol, "BUY", spec.qty_step, f"yxreject{uuid.uuid4().hex[:10]}")
                rep.add("3 invalid order rejected", "FAIL", "an order below the minimum was accepted")
            except ExchangeRequestError as e:
                rep.add("3 invalid order rejected", "PASS" if not e.ambiguous else "FAIL", str(e)[:200])

            rules = SizingRules(risk_pct=Decimal("0.5"), max_loss_quote=max_quote * stop_pct / 100,
                                max_position_quote=max_quote, taker_fee_bps=Decimal(10), slippage_bps=Decimal(10),
                                stop_limit_gap_bps=Decimal(30))
            stop = book.bid * (1 - stop_pct / 100)
            tp = book.ask * (1 + 2 * stop_pct / 100)
            key = f"practice:{c.id.hex[:8]}:{symbol}:{datetime.now(timezone.utc):%Y%m%dT%H%M%S}"
            async with sf() as s:
                try:
                    t = await ex.open_long(s, strategy_id="practice_lifecycle", symbol=symbol, entry_key=key, rules=rules,
                                           stop_price=stop, take_profit=tp, plan=None,
                                           thesis={"reason": "practice lifecycle test", "tool": "practice_lifecycle"})
                except EntryRefused as e:
                    rep.add("4 entry and protection", "FAIL", f"refused before sending: {e}")
                    return 1
                ok = t.status == "OPEN" and t.protection == "VERIFIED"
                rep.add("4 entry and protection", "PASS" if ok else "FAIL",
                        f"trade {t.id}; status {t.status}; bought {t.qty_planned} planned, held {t.qty} at "
                        f"{t.entry_price}; stop {t.stop} (limit {t.stop_limit}); target {t.take_profit} "
                        f"({'exchange-held' if acct.exchange_take_profit else 'engine-managed'}); protection {t.protection}")
                if not ok:
                    return 1
                try:
                    await ex.open_long(s, strategy_id="practice_lifecycle", symbol=symbol, entry_key=key, rules=rules,
                                       stop_price=stop, take_profit=tp, plan=None, thesis={})
                    rep.add("5 duplicate signal refused", "FAIL", "a second entry was accepted")
                except EntryRefused as e:
                    rep.add("5 duplicate signal refused", "PASS", str(e))

            fresh = Executor(acct, c)
            async with sf() as s:
                r = await fresh.reconcile(s)
                t = await s.get(ExecTrade, t.id)
                rep.add("6 restart reconciliation", "PASS" if not r["problems"] and t.status == "OPEN" else "FAIL",
                        f"problems {r['problems']}; trade {t.status}, protection {t.protection}")

                book = await acct.book(symbol)
                new_stop = spec.round_price(min(t.stop * Decimal("1.004"), book.bid * Decimal("0.99")), up=False)
                await fresh.move_stop(s, t, new_stop, "practice test: tighten the stop")
                await s.commit()
                rep.add("7 tighten stop", "PASS" if t.stop == new_stop and t.protection == "VERIFIED" else "FAIL",
                        f"stop now {t.stop}, protection {t.protection}, "
                        f"{'amended in place' if acct.stop_amendable else 'replaced (cancel + place)'}")

                half = spec.round_qty(t.qty / 2)
                if half >= spec.min_qty and t.qty - half >= spec.min_qty and half * t.stop >= spec.min_notional:
                    plan = dict(t.plan)
                    plan["partials"] = [{"at_r": "1", "fraction": "0.5", "stop_to": None, "done": False}]
                    t.plan = plan
                    await fresh.partial_exit(s, t, 0, Decimal("0.5"), "practice test: partial exit of half")
                    await s.commit()
                    rep.add("8 partial exit", "PASS" if t.plan["partials"][0]["done"] and t.protection == "VERIFIED"
                            else "FAIL", f"held now {t.qty}, realized {t.realized_quote}, protection {t.protection}")
                else:
                    rep.add("8 partial exit", "SKIP", "half the position would be below the exchange minimum; raise --max-quote")

                await fresh.close(s, t, "practice test: close")
                await s.refresh(t)
                rep.add("9 close", "PASS" if t.status == "CLOSED" else "FAIL",
                        f"status {t.status}; realized {t.realized_quote} {spec.quote}; reason {t.close_reason}")

                r = await fresh.reconcile(s)
                g = await s.get(ExecGuard, c.id)
                rep.add("10 final reconciliation", "PASS" if not r["problems"] and not (g and g.entries_blocked) else "FAIL",
                        f"problems {r['problems']}; entries blocked {bool(g and g.entries_blocked)}")
                s.add(Event(at=datetime.now(timezone.utc), level="INFO", source="practice_lifecycle",
                            message=f"practice lifecycle on {c.exchange} {c.environment}",
                            detail={"rows": [list(x) for x in rep.rows]}))
                await s.commit()
        except ExchangeRequestError as e:
            rep.add("exchange", "FAIL", str(e)[:300])
            return 1
        finally:
            await acct.aclose()
    finally:
        await engine.dispose()
        fails = [x for x in rep.rows if x[1] == "FAIL"]
        print(json.dumps({"passed": sum(1 for x in rep.rows if x[1] == "PASS"), "failed": len(fails),
                          "skipped": sum(1 for x in rep.rows if x[1] == "SKIP")}))
    return 1 if fails else 0


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--connection", required=True, help="label or id of a DEMO / TESTNET connection")
    p.add_argument("--symbol", default="BTC/USDT")
    p.add_argument("--max-quote", type=Decimal, default=Decimal(20), help="largest position, in USDT (default 20)")
    p.add_argument("--stop-pct", type=Decimal, default=Decimal(2), help="stop distance below the bid, %% (default 2)")
    p.add_argument("--yes", action="store_true", help="confirm: send practice orders to the exchange's demo/testnet")
    a = p.parse_args()
    if not a.yes:
        print("Add --yes to send practice orders (demo / testnet only; a LIVE connection is refused).")
        sys.exit(2)
    if not (Decimal(5) <= a.max_quote <= Decimal(500)) or not (Decimal("0.5") <= a.stop_pct <= Decimal(10)):
        print("--max-quote must be 5-500 and --stop-pct 0.5-10")
        sys.exit(2)
    sys.exit(asyncio.run(run(a.connection, a.symbol, a.max_quote, a.stop_pct)))


if __name__ == "__main__":
    main()
