"""Execution latency trace and price-execution analysis: numbers come from
recorded stages and the program's own (byte-exact) trade event; the cause
is named only when a component is large enough, with its evidence."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

from yonixalpha_core import execution_analysis as xa
from yonixalpha_core.testing.pump import MINT, Curve, logs_of, wallet

WALLET = wallet(1)
T0 = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
DEC = 6


def curve_buy(sol: int, pre_trades: list[int] = ()) -> tuple[dict, Curve, Decimal, Decimal]:
    """(event dict, curve, spot before other traders, spot right before our trade)."""
    c = Curve()
    spot0 = c.price(DEC)
    for i, s in enumerate(pre_trades):
        c.trade(wallet(100 + i), T0, s, True)
    spot_pre = c.price(DEC)
    raw = c.trade(WALLET, T0, sol, True)
    ev = xa.own_trade_event(logs_of(raw), WALLET, MINT)
    return ev, c, spot0, spot_pre


def result_for(ev: dict, venue_curve: tuple[int, int], sol_spent: int, tokens: int, stages=None) -> dict:
    return {"fill": {"sol_change_lamports": -sol_spent, "token_change_raw": tokens, "fee_lamports": 5000},
            "trade_event": ev, "venue": {"curve": {"virtual_sol": venue_curve[0], "virtual_tokens": venue_curve[1]}},
            "stages": stages or [{"stage": "TRANSACTION_BUILT", "at": T0.timestamp(), "tokens_out": tokens,
                                  "blockhash_slot": 100},
                                 {"stage": "TRANSACTION_CONFIRMED", "at": T0.timestamp() + 1, "slot": 102}]}


def test_own_trade_event_reads_our_curve_trade_and_the_reserves_before_it():
    c = Curve()
    vs0, vt0 = c.vsol, c.vtok
    raw = c.trade(WALLET, T0, 10**8, True)
    other = Curve().trade(wallet(7), T0, 10**8, True)
    ev = xa.own_trade_event(logs_of(other, raw), WALLET, MINT)
    assert ev["venue"] == "PUMP_BONDING_CURVE" and ev["is_buy"] and ev["quote_lamports"] == 10**8
    assert ev["reserves_before"] == {"quote": vs0, "base": vt0}
    assert xa.own_trade_event(logs_of(other), WALLET, MINT) is None


def test_small_buy_on_an_unchanged_curve_is_within_expected():
    ev, c, spot0, spot_pre = curve_buy(10**7)
    venue = (spot_pre * 0 + Curve().vsol, Curve().vtok)
    res = result_for(ev, venue, ev["quote_lamports"] + 5000, ev["token_raw"])
    out = xa.price_analysis("BUY", "0.01", DEC, res, {"price_sol": str(spot0), "price_age_seconds": 1}, {})
    assert out["classification"] == "WITHIN_EXPECTED", out
    assert Decimal(out["components_pct"]["decision_to_build_pct"]) == 0
    assert Decimal(out["components_pct"]["build_to_landing_pct"]) == 0


def test_other_trades_landing_first_is_curve_movement_and_slow_inclusion_is_named():
    ev, c, spot0, spot_pre = curve_buy(10**7, pre_trades=[3 * 10**9])  # someone bought 3 SOL just before us
    base = Curve()
    res = result_for(ev, (base.vsol, base.vtok), ev["quote_lamports"] + 5000, ev["token_raw"])
    fast = xa.price_analysis("BUY", "0.01", DEC, res, {"price_sol": str(spot0), "price_age_seconds": 1},
                             {"slots_to_land": 2, "submission_latency_ms": 200})
    assert fast["classification"] == "CURVE_MOVEMENT" and Decimal(fast["components_pct"]["build_to_landing_pct"]) > 10
    slow = xa.price_analysis("BUY", "0.01", DEC, res, {"price_sol": str(spot0), "price_age_seconds": 1},
                             {"slots_to_land": 30, "submission_latency_ms": 200})
    assert slow["classification"] == "PRIORITY_FEE_DELAY" and "30 slots" in slow["evidence"][0]
    rpc = xa.price_analysis("BUY", "0.01", DEC, res, {"price_sol": str(spot0), "price_age_seconds": 1},
                            {"slots_to_land": 2, "submission_latency_ms": 4000})
    assert rpc["classification"] == "RPC_LATENCY"


def test_price_moving_before_the_build_is_market_moved_or_stale_data():
    ev, c, spot0, spot_pre = curve_buy(10**7, pre_trades=[3 * 10**9])
    # The build already saw the moved curve: the move happened before it.
    moved = Curve()
    moved.trade(wallet(8), T0, 3 * 10**9, True)
    res = result_for(ev, (moved.vsol, moved.vtok), ev["quote_lamports"] + 5000, ev["token_raw"])
    out = xa.price_analysis("BUY", "0.01", DEC, res, {"price_sol": str(spot0), "price_age_seconds": 1}, {})
    assert out["classification"] == "MARKET_MOVED"
    stale = xa.price_analysis("BUY", "0.01", DEC, res, {"price_sol": str(spot0), "price_age_seconds": 45}, {})
    assert stale["classification"] == "DATA_STALENESS" and "45s old" in stale["evidence"][0]


def test_a_large_buy_against_thin_reserves_is_price_impact():
    ev, c, spot0, spot_pre = curve_buy(8 * 10**9)  # 8 SOL into a fresh curve
    base = Curve()
    res = result_for(ev, (base.vsol, base.vtok), ev["quote_lamports"] + 5000, ev["token_raw"])
    out = xa.price_analysis("BUY", "8", DEC, res, {"price_sol": str(spot0), "price_age_seconds": 1}, {})
    assert out["classification"] == "PRICE_IMPACT" and Decimal(out["components_pct"]["price_impact_pct"]) > 10


def test_without_a_trade_event_nothing_is_guessed():
    base = Curve()
    res = {"fill": {"sol_change_lamports": -10**7, "token_change_raw": 10**9, "fee_lamports": 5000},
           "venue": {"curve": {"virtual_sol": base.vsol, "virtual_tokens": base.vtok}}, "stages": []}
    out = xa.price_analysis("BUY", "0.01", DEC, res, {"price_sol": str(base.price(DEC))}, {})
    assert out["components_pct"]["price_impact_pct"] is None and any("no Pump/PumpSwap trade event" in e for e in out["evidence"])


def test_timing_trace_from_recorded_stages():
    t = T0.timestamp()
    stages = [{"stage": "VENUE_RESOLVED", "at": t + 1.0}, {"stage": "TRANSACTION_BUILT", "at": t + 1.2, "blockhash_slot": 500},
              {"stage": "TRANSACTION_GUARD_PASSED", "at": t + 1.21}, {"stage": "TRANSACTION_SIGNED", "at": t + 1.3},
              {"stage": "SIMULATED", "at": t + 1.5}, {"stage": "TRANSACTION_SUBMITTED", "at": t + 1.6},
              {"stage": "TRANSACTION_SEEN", "at": t + 2.1, "slot": 503},
              {"stage": "TRANSACTION_CONFIRMED", "at": t + 2.6, "slot": 503}, {"stage": "FILL_VERIFIED", "at": t + 2.6}]
    calls = [{"method": "getMultipleAccounts", "at": t + 0.9, "ms": 80}, {"method": "getLatestBlockhash", "at": t + 1.1, "ms": 60},
             {"method": "sendTransaction", "at": t + 1.55, "ms": 50}, {"method": "getSignatureStatuses", "at": t + 2.0, "ms": 40}]
    decision = {"decision_at": (T0 - timedelta(seconds=2)).isoformat(), "decision_started_at": (T0 - timedelta(seconds=5)).isoformat(),
                "decision_eval_ms": 3000}
    out = xa.timing(T0 + timedelta(seconds=0.5), {"stages": stages, "rpc_calls": calls}, decision,
                    discovered_at=T0 - timedelta(seconds=60))
    assert out["queue_wait_ms"] == 500 and out["quote_latency_ms"] == 0  # pickup = first stage
    assert out["build_ms"] == 200 and out["simulation_ms"] == 200 and out["submission_latency_ms"] == 100
    assert out["submit_to_confirm_ms"] == 1000 and out["decision_to_submit_ms"] == 3600 and out["decision_to_confirm_ms"] == 4600
    assert out["slots_to_land"] == 3 and out["rpc_calls_before_submit"] == 3 and out["rpc_latency_before_submit_ms"] == 190
    assert out["discovery_to_decision_ms"] == 58000


def test_analyze_records_timing_for_failed_orders_and_price_only_when_confirmed():
    failed = SimpleNamespace(diagnostics={"decision": {"decision_at": T0.isoformat()}}, limits={}, created_at=T0,
                             result={"stages": [{"stage": "VENUE_RESOLVED", "at": T0.timestamp() + 1}]}, status="FAILED",
                             side="BUY", amount="0.01", amount_kind="sol", priority_fee_sol=Decimal("0.0001"))
    d = xa.analyze(failed)
    assert d["timing"]["queue_wait_ms"] == 1000 and "price" not in d


# --- trade_report over the database -------------------------------------------------
import os  # noqa: E402

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://yonixalpha:yonixalpha_test_pw@localhost:5432/yonixalpha_test")


async def test_trade_report_measures_old_and_new_orders(capsys, monkeypatch):
    import json

    from sqlalchemy.ext.asyncio import create_async_engine

    from yonixalpha_core.db import models  # noqa: F401
    from yonixalpha_core.db.base import Base, make_session_factory
    from yonixalpha_core.db.models import ExecutionOrder, PaperPosition
    from yonixalpha_core.tools import trade_report

    engine = create_async_engine(os.environ["DATABASE_URL"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    ev, c, spot0, _ = curve_buy(10**7, pre_trades=[3 * 10**9])
    base = Curve()
    t = T0.timestamp()
    async with make_session_factory(engine)() as s:
        pos = PaperPosition(symbol="TEST", provider="live", side="LONG", entry_price=Decimal("1"), quantity=Decimal(1), entry_at=T0,
                            status="open", execution_mode="LIVE", plan={"entry_price": str(spot0), "venue": {"decimals": DEC}})
        s.add(pos)
        await s.flush()
        s.add(ExecutionOrder(position_id=pos.id, mode="LIVE", side="BUY", reason="entry", mint=MINT, provider="pumpportal_local",
                             route="pump", amount="0.01", amount_kind="sol", slippage_pct=Decimal(10),
                             priority_fee_sol=Decimal("0.0001"), status="CONFIRMED", idempotency_key="k1", created_at=T0,
                             result={**result_for(ev, (base.vsol, base.vtok), ev["quote_lamports"] + 5000, ev["token_raw"],
                                                  stages=[{"stage": "VENUE_RESOLVED", "at": t + 0.7},
                                                          {"stage": "TRANSACTION_BUILT", "at": t + 0.9, "tokens_out": ev["token_raw"]},
                                                          {"stage": "TRANSACTION_SIGNED", "at": t + 1.0},
                                                          {"stage": "TRANSACTION_SUBMITTED", "at": t + 1.3},
                                                          {"stage": "TRANSACTION_CONFIRMED", "at": t + 2.3, "slot": 9}]),
                                     "trade_event": None, "logs": logs_of(Curve().trade(WALLET, T0, 1, True))}))
        await s.commit()
    await engine.dispose()
    monkeypatch.setenv("WALLET_PUBLIC_KEY", WALLET)
    monkeypatch.setenv("JWT_SECRET", "x" * 40)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", "$2b$12$" + "x" * 53)
    from yonixalpha_core.config import get_settings
    get_settings.cache_clear()
    try:
        assert await trade_report.main(["--json"]) == 0
    finally:
        get_settings.cache_clear()
    out = json.loads(capsys.readouterr().out)
    row = out["orders"][0]
    assert row["timing"]["queue_wait_ms"] == 700 and row["timing"]["submit_to_confirm_ms"] == 1000
    assert row["decision"]["reconstructed"] and row["price"]["decision_price_sol"] == str(spot0)
    assert out["summary"]["submit_to_confirm_ms"]["worst"] == 1000


# --- where the wallet's SOL went ------------------------------------------------

ATA_RENT = 2_039_280


def parsed_tx(accounts: list[tuple[str, int, int]], fee: int, inner: list[dict], token_balances: list[tuple]) -> dict:
    """A getTransaction (jsonParsed) result: accounts are (key, pre, post)
    lamports, fee payer first; token_balances are (index, mint, owner, pre, post)."""
    def tb(which):
        return [{"accountIndex": i, "mint": m, "owner": o, "uiTokenAmount": {"amount": str(pre if which == "pre" else post),
                                                                            "decimals": DEC}}
                for i, m, o, pre, post in token_balances if (pre if which == "pre" else post) is not None]
    return {"version": 0, "slot": 5, "transaction": {"signatures": ["s"], "message": {
                "accountKeys": [{"pubkey": k, "signer": i == 0, "writable": True} for i, (k, _, _) in enumerate(accounts)],
                "instructions": []}},
            "meta": {"err": None, "fee": fee, "preBalances": [a[1] for a in accounts], "postBalances": [a[2] for a in accounts],
                     "preTokenBalances": tb("pre"), "postTokenBalances": tb("post"),
                     "innerInstructions": [{"index": 1, "instructions": inner}], "logMessages": []}}


def create(src: str, new: str, lamports: int, space: int) -> dict:
    return {"program": "system", "parsed": {"type": "createAccount",
                                            "info": {"source": src, "newAccount": new, "lamports": lamports, "space": space}}}


def test_cost_breakdown_itemizes_a_curve_buy_that_opened_a_token_account():
    """A 0.004 SOL buy that creates the token account: the rent deposit, not
    the market, makes the all-in price ~50% above the trade price."""
    ata, curve_acct = wallet(20), wallet(21)
    trade, fees, fee = 4_000_000, 50_000, 105_000
    spent = trade + fees + fee + ATA_RENT
    tx = parsed_tx([(WALLET, 10**9, 10**9 - spent), (ata, 0, ATA_RENT), (curve_acct, 5 * 10**9, 5 * 10**9 + trade)],
                   fee, [create(WALLET, ata, ATA_RENT, 165)], [(1, MINT, WALLET, None, 1000)])
    ev = {"venue": "PUMP_BONDING_CURVE", "is_buy": True, "quote_lamports": trade, "token_raw": 1000,
          "fee_lamports": 40_000, "creator_fee_lamports": 10_000,
          "reserves_before": {"quote": trade * 10**4, "base": 1000 * 10**4}}
    c = xa.cost_breakdown(tx, WALLET, MINT, ev)
    assert c["wallet_change_lamports"] == -spent and c["trade_lamports"] == trade and c["program_fees_lamports"] == fees
    assert c["network_fee_lamports"] == fee and c["priority_fee_lamports"] == 100_000
    assert c["net_deposits_lamports"] == ATA_RENT == c["token_account_rent_lamports"] and c["residual_lamports"] == 0
    assert c["deposits"][0]["what"].startswith("token account for this token")

    res = {"fill": {"sol_change_lamports": -spent, "token_change_raw": 1000, "fee_lamports": fee}, "trade_event": ev,
           "costs": c, "stages": []}
    out = xa.price_analysis("BUY", "0.004", DEC, res, {"price_sol": str(Decimal(trade) / 10**9 / (Decimal(1000) / 10**DEC))}, {})
    assert out["classification"] == "ACCOUNT_RENT", out
    assert out["components_pct"]["deposits_pct"] == "50.98" and out["components_pct"]["program_fees_pct"] == "1.25"
    assert out["costs_sol"]["token_account_rent_lamports"] == "0.00203928"
    assert not any("within the slippage limit" in e for e in out["evidence"])


def test_cost_breakdown_nets_a_wrapped_sol_account_opened_and_closed_in_the_same_swap():
    """PumpSwap buy: the temporary wrapped-SOL account's deposit comes back
    when it is closed; only the token account's rent stays deposited."""
    ata, wsol, pool = wallet(30), wallet(31), wallet(32)
    trade, lp_fees, fee = 3_000_000, 30_000, 105_000
    spent = trade + lp_fees + fee + ATA_RENT
    tx = parsed_tx([(WALLET, 10**9, 10**9 - spent), (ata, 0, ATA_RENT), (wsol, 0, 0), (pool, 10**10, 10**10 + trade + lp_fees)],
                   fee, [create(WALLET, wsol, ATA_RENT, 165), create(WALLET, ata, ATA_RENT, 170),
                         {"program": "spl-token", "parsed": {"type": "closeAccount",
                                                             "info": {"account": wsol, "destination": WALLET, "owner": WALLET}}}],
                   [(1, MINT, WALLET, None, 500), (2, "So11111111111111111111111111111111111111112", WALLET, None, 0)])
    ev = {"venue": "PUMP_AMM", "is_buy": True, "quote_lamports": trade, "user_quote_lamports": trade + lp_fees, "token_raw": 500}
    c = xa.cost_breakdown(tx, WALLET, MINT, ev)
    assert c["net_deposits_lamports"] == ATA_RENT and c["program_fees_lamports"] == lp_fees and c["residual_lamports"] == 0
    assert {d["what"] for d in c["deposits"]} == {"wrapped-SOL token account",
                                                  "token account for this token (rent: returned when the account is closed)"}


def test_cost_breakdown_reports_what_it_cannot_explain():
    tx = parsed_tx([(WALLET, 10**9, 10**9 - 5_000_000)], 105_000, [], [])
    ev = {"venue": "PUMP_BONDING_CURVE", "is_buy": True, "quote_lamports": 4_000_000, "token_raw": 1,
          "fee_lamports": 0, "creator_fee_lamports": 0}
    assert xa.cost_breakdown(tx, WALLET, MINT, ev)["residual_lamports"] == 895_000
    assert xa.cost_breakdown(tx, WALLET, MINT, None)["residual_lamports"] is None  # no event: nothing is guessed


async def test_cost_report_itemizes_confirmed_orders_and_counts_empty_token_accounts(capsys, monkeypatch):
    import json

    import httpx
    from sqlalchemy.ext.asyncio import create_async_engine

    from yonixalpha_core.db.base import Base, make_session_factory
    from yonixalpha_core.db.models import ExecutionOrder
    from yonixalpha_core.tools import cost_report

    engine = create_async_engine(os.environ["DATABASE_URL"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with make_session_factory(engine)() as s:
        s.add(ExecutionOrder(mode="LIVE", side="BUY", reason="entry", mint=MINT, provider="pumpportal_local", route="pump",
                             amount="0.004", amount_kind="sol", slippage_pct=Decimal(10), priority_fee_sol=Decimal("0.0001"),
                             status="CONFIRMED", idempotency_key="c1", signature="sig-c1", created_at=T0))
        await s.commit()
    await engine.dispose()

    ata = wallet(20)
    trade, fee = 4_000_000, 105_000
    tx = parsed_tx([(WALLET, 10**9, 10**9 - trade - fee - ATA_RENT), (ata, 0, ATA_RENT)], fee,
                   [create(WALLET, ata, ATA_RENT, 165)], [(1, MINT, WALLET, None, 1000)])
    tx["meta"]["logMessages"] = logs_of(Curve().trade(WALLET, T0, trade, True))
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body["method"])
        if body["method"] == "getTransaction":
            return httpx.Response(200, json={"result": tx})
        if body["method"] == "getTokenAccountsByOwner":
            accs = [{"pubkey": wallet(40 + i), "account": {"lamports": ATA_RENT, "data": {"parsed": {"info": {
                "mint": wallet(60 + i), "tokenAmount": {"amount": "0" if i < 3 else "7"}}}}}} for i in range(4)]
            return httpx.Response(200, json={"result": {"value": accs if "Tokenkeg" in body["params"][1]["programId"] else []}})
        return httpx.Response(200, json={"error": {"code": -32601, "message": "unexpected"}})

    real = httpx.AsyncClient
    monkeypatch.setattr(cost_report.httpx, "AsyncClient", lambda *a, **k: real(transport=httpx.MockTransport(handler)))
    monkeypatch.setenv("WALLET_PUBLIC_KEY", WALLET)
    monkeypatch.setenv("SOLANA_RPC_URL", "https://rpc.example/key")
    monkeypatch.setenv("JWT_SECRET", "x" * 40)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", "$2b$12$" + "x" * 53)
    from yonixalpha_core.config import get_settings
    get_settings.cache_clear()
    try:
        assert await cost_report.main(["--json"]) == 0
    finally:
        get_settings.cache_clear()
    out = json.loads(capsys.readouterr().out)
    c = out["orders"][0]["costs"]
    assert c["trade_lamports"] == trade and c["token_account_rent_lamports"] == ATA_RENT and c["residual_lamports"] == 0
    acc = out["token_accounts"]
    assert len(acc["empty"]) == 3 and acc["empty_lamports"] == 3 * ATA_RENT and acc["with_balance_lamports"] == ATA_RENT
    assert set(calls) == {"getTransaction", "getTokenAccountsByOwner"}  # read-only: nothing signed or sent
