"""Dashboard-managed operating mode and the mandatory LIVE checks
(seven-day investigation, Phase 8, 2026-10-24).

Who decides what
  .env ceiling   TRADING_ENABLED, LIVE_TRADING_ENABLED, PAPER_TRADING=false
                 (safety.store.live_trading_permitted). A deployment-level hard
                 limit and emergency stop, set once by the server owner; while
                 it is closed nothing can trade live. It is not the ordinary
                 mode selector.
  dashboard      the global mode (PAPER / MANUAL / LIVE, stored in the
                 database) is the ordinary selector. LIVE is armed only
                 through arm(): re-authentication, a typed confirmation, a
                 reason, and every mandatory check below passing on the
                 server at that moment. disarm() returns to PAPER at once.
  kill switch    stops every new entry immediately (Redis); exits continue.

Operating states (state())
  OBSERVATION_ONLY   no strategy may enter (all OFF, or the new-entries switch
                     off): the scanner still observes and records
  PAPER              entries are simulated (global PAPER or MANUAL)
  LIVE_ARMED_BLOCKED the global mode is LIVE, but a mandatory check fails: no
                     live order is created or sent; the reasons are listed
  LIVE_ELIGIBLE      the global mode is LIVE and every check passes: a decision
                     that passes the gate may execute in the live wallet

Continuous enforcement: the order worker (paper-trading) runs readiness()
every CONTROL_SECONDS while the ceiling is open and stores the result in
Redis (CONTROL_KEY, short TTL). live_trading.live_readiness refuses to create
a LIVE order unless that record is fresh and passing, and the worker refuses
to send a BUY while the global mode is not LIVE, the kill switch is engaged
or the record is not passing. Exits (SELL) are never blocked by these
checks: turning LIVE off never strands or closes a position.

Readiness checks (block every live order, the smoke test included)
  ceiling, kill_switch, wallet, signer, rpc, network, program_addresses,
  balance, duplicate_protection, exposure, scanner, risk_engine,
  critical_faults
Arming checks (also needed to arm LIVE)
  entry_switches, strategy_selected, configuration, sizing, stop_policy,
  slippage_limits
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core import events, live_activation, live_trading
from yonixalpha_core.db.models import ExecutionOrder, PaperPosition, PlatformSetting
from yonixalpha_core.safety import store
from yonixalpha_core.safety.models import GlobalMode
from yonixalpha_core.solana import txguard

CONTROL_KEY = "yx:live:control"
CONTROL_TTL = 60
CONTROL_SECONDS = 15
LAST_CHANGE_KEY = "live_control"  # platform setting: the last arm / disarm
MAINNET_GENESIS = "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d"
EXPECTED_PROGRAMS = {"PUMP": "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
                     "PUMP_AMM": "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",
                     "WSOL": "So11111111111111111111111111111111111111112"}
STUCK_ORDER_SECONDS = 300
HEARTBEAT_MAX_AGE = 120
WORKER_MAX_AGE = 60  # the worker republishes every loop; its key expires after 60 s
MAX_RATE_LIMITED_SHARE = 0.5
READINESS = ("ceiling", "kill_switch", "wallet", "signer", "rpc", "network", "program_addresses", "balance",
             "duplicate_protection", "exposure", "scanner", "risk_engine", "critical_faults")
# checks the order worker can only pass once the ceiling is open (it publishes "disabled" while it is closed)
CEILING_DEPENDENT = ("signer", "rpc", "network", "balance")
ARMING = ("entry_switches", "strategy_selected", "configuration", "sizing", "stop_policy", "slippage_limits")
PASS, FAIL = "PASS", "FAIL"
LIVE_ENGINES = live_activation.ENGINES


@dataclass
class Check:
    code: str
    label: str
    status: str  # PASS | FAIL
    detail: str
    action: str = ""
    kind: str = "readiness"  # readiness | arming


def _c(code: str, label: str, ok: bool, detail: str, action: str = "", kind: str = "readiness") -> Check:
    return Check(code, label, PASS if ok else FAIL, detail, "" if ok else action, kind)


def _age(at: Any, now: datetime) -> float | None:
    try:
        return (now - datetime.fromisoformat(str(at))).total_seconds()
    except (TypeError, ValueError):
        return None


async def _json(redis: Any, key: str) -> dict | None:
    raw = await redis.get(key) if redis is not None else None
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


async def evaluate(session: AsyncSession, redis: Any, settings: Any, now: datetime | None = None) -> dict[str, Any]:
    """Every check, from what the server records now. Read-only."""
    from yonixalpha_core import scanner_health

    now = now or datetime.now(timezone.utc)
    act = await live_activation.status(session, redis, settings, now)
    codes = {b["code"]: b for b in act["blockers"]}
    worker = await _json(redis, live_trading.READY_KEY) or {}
    wallet = await _json(redis, live_trading.WALLET_KEY) or {}
    checks: list[Check] = []

    # --- readiness ---------------------------------------------------------------------------------------
    checks.append(_c("ceiling", "Server ceiling (.env locks)", act["locks_open"],
                     "open: the dashboard decides" if act["locks_open"] else
                     codes.get("ENV_LOCKS_CLOSED", {}).get("message", "closed"),
                     "Set once on the server: TRADING_ENABLED=true, LIVE_TRADING_ENABLED=true, PAPER_TRADING=false, "
                     "then scripts/deploy.sh. It is a ceiling, not the mode switch."))
    checks.append(_c("kill_switch", "Kill switch released", not act["kill_switch"],
                     "released" if not act["kill_switch"] else codes.get("KILL_SWITCH", {}).get("message", "engaged"),
                     "Release it on the Risk page when you are ready."))
    w = act["wallet"]
    checks.append(_c("wallet", "Wallet configured", bool(w["configured"] and w["valid"]),
                     f"public key {w['pubkey']}" if w["configured"] and w["valid"] else
                     (codes.get("WALLET_KEY_INVALID") or codes.get("WALLET_NOT_CONFIGURED") or {}).get("message", "not configured"),
                     "Set WALLET_PRIVATE_KEY on the server with scripts/set-keys.sh (never in the dashboard)."))
    ready = worker.get("status") == "ready" and (_age(worker.get("at"), now) or 1e9) <= WORKER_MAX_AGE
    checks.append(_c("signer", "Order worker ready to sign", ready,
                     "ready" if ready else f"worker {worker.get('status', 'not reporting')}"
                     + (f": {worker['reason']}" if worker.get("reason") else ""),
                     "Check the paper-trading container (it needs the open ceiling, the wallet key and an RPC)."))
    sync_age = _age(wallet.get("at"), now)
    max_age = float(worker.get("wallet_max_age_seconds") or 120)
    rpc_health = None
    if redis is not None:
        try:
            rpc_health = (await scanner_health.report(session, redis, now))
        except Exception as exc:  # noqa: BLE001 - an unreadable health record fails the checks that need it
            rpc_health = {"overall": "UNKNOWN", "error": f"{type(exc).__name__}"}
    share = ((rpc_health or {}).get("rpc_last_hour") or {}).get("rate_limited_share")
    rpc_ok = sync_age is not None and sync_age <= max_age and (share is None or share <= MAX_RATE_LIMITED_SHARE)
    checks.append(_c("rpc", "Solana RPC answering", rpc_ok,
                     f"wallet read {sync_age:.0f} s ago" if sync_age is not None else "the wallet has not been read",
                     "Check the Solana RPC provider (Settings: providers)."))
    if share is not None and share > MAX_RATE_LIMITED_SHARE:
        checks[-1].detail += f"; {share * 100:.0f} % of RPC calls rate-limited in the last hour"
    genesis = worker.get("genesis_hash")
    checks.append(_c("network", "Network is Solana mainnet", genesis == MAINNET_GENESIS,
                     "mainnet (genesis hash verified by the worker)" if genesis == MAINNET_GENESIS else
                     f"genesis hash {genesis or 'not reported'}; expected mainnet {MAINNET_GENESIS}",
                     "Point SOLANA_RPC_URL at a mainnet endpoint; the worker reads the genesis hash at start-up."))
    actual = {"PUMP": txguard.PUMP, "PUMP_AMM": txguard.PUMP_AMM, "WSOL": txguard.WSOL}
    wrong = [k for k, v in EXPECTED_PROGRAMS.items() if actual[k] != v]
    checks.append(_c("program_addresses", "Program and token addresses", not wrong,
                     "Pump, PumpSwap and wrapped-SOL addresses are the mainnet ones" if not wrong else
                     f"unexpected addresses: {', '.join(wrong)}", "Do not trade: the build is not the reviewed one."))
    reserve = Decimal(str(worker.get("min_sol_reserve") or "0.05"))
    live = await live_trading.load_live_settings(session)
    fixed, _ = live_trading.fixed_trade_costs(live)
    sizes = []
    for e in act["live_strategies"]:
        s, _ = await store.load_settings(session, e)
        sizes.append(s.fixed_trade_size_sol if s.trade_size_mode == "FIXED_SOL" else s.max_position_size_quote)
    need = reserve + (max(sizes) if sizes else Decimal(0)) + fixed + live_trading.TOKEN_ACCOUNT_RENT_SOL
    try:
        sol = Decimal(str(wallet.get("sol"))) if wallet.get("sol") is not None else None
    except ArithmeticError:
        sol = None
    checks.append(_c("balance", "Balance covers one trade and its costs", sol is not None and sol >= need,
                     f"wallet {sol if sol is not None else 'unknown'} SOL; needed {need} SOL (reserve {reserve} + largest "
                     f"live size {max(sizes) if sizes else 0} + fixed costs {fixed} + token-account rent "
                     f"{live_trading.TOKEN_ACCOUNT_RENT_SOL})",
                     "Add SOL to the trading wallet or lower the live size."))
    idx = (await session.execute(text(
        "SELECT count(*) FROM pg_indexes WHERE tablename = 'execution_orders' AND indexdef ILIKE '%UNIQUE%idempotency_key%'"
    ))).scalar_one()
    stuck = (await session.execute(select(func.count()).select_from(ExecutionOrder).where(
        ExecutionOrder.mode == "LIVE", ExecutionOrder.status.in_(("SIGNED", "SUBMITTED")),
        ExecutionOrder.updated_at < now - timedelta(seconds=STUCK_ORDER_SECONDS)))).scalar_one()
    checks.append(_c("duplicate_protection", "Duplicate-order and replay protection", bool(idx) and not stuck,
                     ("one order per decision (unique idempotency key in the database)" if idx else
                      "the unique idempotency index is missing") + (f"; {stuck} sent order(s) unresolved for more than "
                                                                     f"{STUCK_ORDER_SECONDS // 60} min" if stuck else ""),
                     "Wait for reconciliation to resolve the sent orders (Live Execution: orders)."))
    g, _ = await store.load_settings(session, store.GLOBAL_SCOPE)
    open_live = (await session.execute(select(func.count()).select_from(PaperPosition).where(
        PaperPosition.execution_mode == "LIVE", PaperPosition.status.in_(("pending_entry", "open", "needs_review"))))).scalar_one()
    checks.append(_c("exposure", "Open positions and exposure within limits",
                     g.max_open_positions >= 1 and open_live < g.max_open_positions and g.max_total_exposure_quote > 0,
                     f"{open_live} LIVE position(s) open of max {g.max_open_positions}; total exposure cap "
                     f"{g.max_total_exposure_quote} SOL", "Close positions or review the limits in Risk settings."))
    overall = (rpc_health or {}).get("overall")
    checks.append(_c("scanner", "Scanner and data fresh", overall == "CONNECTED",
                     f"scanner {overall or 'not reported'}", "See Health: scanner pipeline."))
    hb = (await events.read_heartbeats(redis, ["decision-engine"]))["decision-engine"] if redis is not None else None
    hb_age = _age((hb or {}).get("at"), now)
    checks.append(_c("risk_engine", "Risk engine running", hb_age is not None and hb_age <= HEARTBEAT_MAX_AGE,
                     f"decision-engine heartbeat {hb_age:.0f} s ago" if hb_age is not None else "decision-engine not reporting",
                     "Start the decision-engine container."))
    review = (await session.execute(select(func.count()).select_from(PaperPosition).where(
        PaperPosition.execution_mode == "LIVE", PaperPosition.status == "needs_review"))).scalar_one()
    failing_exits = (await session.execute(select(func.count()).select_from(PaperPosition).where(
        PaperPosition.execution_mode == "LIVE", PaperPosition.status == "open", PaperPosition.exit_failures > 0))).scalar_one()
    checks.append(_c("critical_faults", "No unresolved critical fault", not review and not failing_exits,
                     "none" if not review and not failing_exits else
                     f"{review} LIVE position(s) need review; {failing_exits} open with failed exits",
                     "Resolve them on Live Execution first."))

    # --- arming ------------------------------------------------------------------------------------------
    off = [b["message"] for c, b in codes.items() if c.startswith("SWITCH_OFF:")]
    checks.append(_c("entry_switches", "Entry switches on", not off, "on" if not off else "; ".join(off),
                     "Turn them on under Launchpads: trading controls.", "arming"))
    checks.append(_c("strategy_selected", "A strategy may trade live", bool(act["live_strategies"]),
                     ", ".join(act["live_strategies"]) or "no Solana strategy is AUTO or MANUAL",
                     "Set the strategy that may trade live to MANUAL (approve each entry) or AUTO.", "arming"))
    # validated as it would be with the global mode LIVE: the same rule arming applies after the switch
    from yonixalpha_core import config_validation
    from yonixalpha_core.strategies.catalog import MODE_KEYS

    modes_now = {k: await store.load_strategy_mode(session, k) for k in MODE_KEYS}
    res = config_validation.validate(settings, modes_now, GlobalMode.LIVE)
    cfg_errs = [e for name, r in res.items() if r.get("mode") in ("AUTO", "MANUAL")
                for e in config_validation.blocking_errors(res, name)]
    checks.append(_c("configuration", "Strategy configuration complete", not cfg_errs,
                     "; ".join(cfg_errs) or "complete for every live strategy",
                     "Set the named values in the server's .env (scripts/set-keys.sh for secrets).", "arming"))
    bad_sizing = []
    bad_stop = []
    for e in act["live_strategies"]:
        s, _ = await store.load_settings(session, e)
        if s.max_loss_per_trade_sol <= 0:
            bad_sizing.append(f"{e}: max_loss_per_trade_sol is not set")
        if s.trade_size_mode == "FIXED_SOL" and s.fixed_trade_size_sol <= 0:
            bad_sizing.append(f"{e}: fixed_trade_size_sol is not set")
        if not (Decimal(0) < s.min_stop_pct < s.max_stop_pct):
            bad_stop.append(f"{e}: stop range {s.min_stop_pct}..{s.max_stop_pct}")
    checks.append(_c("sizing", "Position size and maximum loss per trade set", not bad_sizing and bool(act["live_strategies"]),
                     "; ".join(bad_sizing) or ("set for " + ", ".join(act["live_strategies"]) if act["live_strategies"]
                                               else "no live strategy"),
                     "Set a maximum loss per trade (SOL) in Risk settings for each live strategy.", "arming"))
    checks.append(_c("stop_policy", "Every entry has a stop", not bad_stop,
                     "the gate refuses a plan without a stop; stop range valid" if not bad_stop else "; ".join(bad_stop),
                     "Fix the stop range in Risk settings.", "arming"))
    lim = live_trading.LIMITS["entry_slippage_pct"][1]
    slip_ok = live.entry_slippage_pct <= lim and g.max_entry_impact_bps <= Decimal(1000)
    checks.append(_c("slippage_limits", "Liquidity, impact and slippage limits", slip_ok,
                     f"entry slippage limit {live.entry_slippage_pct} %, max entry impact {g.max_entry_impact_bps} bps, "
                     f"max slippage {g.max_slippage_bps} bps", "Lower the limits on Live Execution / Risk settings.", "arming"))

    if not act["locks_open"]:
        # While the ceiling is closed the order worker does not start the signer, read the wallet or check the
        # network (services/paper-trading live_worker): those checks fail because of the ceiling. They stay checks.
        for c in checks:
            if c.status == FAIL and c.code in CEILING_DEPENDENT:
                c.detail += " (because the server ceiling is closed: the order worker starts the live signer only after it is opened)"
                c.action = "Open the server ceiling first (see the first check); this check is then re-run. " + c.action
    failed = [c for c in checks if c.status == FAIL]
    readiness_ok = not [c for c in failed if c.kind == "readiness"]
    return {"checked_at": now.isoformat(), "checks": [asdict(c) for c in checks], "readiness_ok": readiness_ok,
            "arming_ok": not failed, "failed": [c.code for c in failed], "global_mode": act["global_mode"],
            "live_strategies": act["live_strategies"], "strategy_modes": act["strategies"],
            "locks": act["locks"], "locks_open": act["locks_open"]}


def state(ev: dict[str, Any], entries_possible: bool) -> dict[str, Any]:
    gm = ev["global_mode"]
    failed = [c for c in ev["checks"] if c["status"] == FAIL]
    if gm == GlobalMode.LIVE.value:
        blocking = [c for c in failed if c["kind"] == "readiness" or c["code"] in ("strategy_selected", "entry_switches")]
        if blocking:
            return {"state": "LIVE_ARMED_BLOCKED", "label": "LIVE armed, blocked",
                    "summary": "The global mode is LIVE, but no live order will be created or sent: "
                               + "; ".join(f"{c['label']}: {c['detail']}" for c in blocking)}
        return {"state": "LIVE_ELIGIBLE", "label": "LIVE enabled and eligible",
                "summary": "Decisions that pass the gate may execute in the live wallet. This does not mean a trade is due."}
    if not entries_possible:
        return {"state": "OBSERVATION_ONLY", "label": "Observation only",
                "summary": "No strategy may enter; the scanner keeps observing and recording."}
    return {"state": "PAPER", "label": "PAPER trading",
            "summary": "Entries are simulated. " + ("LIVE could be armed now." if not failed else
                                                     "LIVE is blocked by: " + "; ".join(c["label"] for c in failed))}


async def overview(session: AsyncSession, redis: Any, settings: Any, now: datetime | None = None) -> dict[str, Any]:
    ev = await evaluate(session, redis, settings, now)
    modes = ev["strategy_modes"]
    entries_possible = any(m != "OFF" for m in modes.values()) and "entry_switches" not in ev["failed"]
    last = await session.get(PlatformSetting, LAST_CHANGE_KEY)
    enforced = await _json(redis, CONTROL_KEY)
    return {**ev, **state(ev, entries_possible), "last_change": dict(last.value) if last else None,
            "worker_enforcement": enforced and {k: enforced.get(k) for k in ("ok", "at", "failed")},
            "toggle": {"on": ev["global_mode"] == GlobalMode.LIVE.value},
            "how_it_works": [
                "The server ceiling (.env) is set once; while it is closed LIVE cannot be switched on.",
                "Switching LIVE on: confirm in the dialog; the server then runs every check below and switches only if all "
                "pass. A refusal keeps LIVE off and lists each failing check with what to do.",
                "The order worker re-runs the readiness checks every 15 s; a failing check stops new live orders.",
                "Disarming returns to PAPER at once: queued live buys are cancelled, exits keep running, nothing is closed.",
                "No ML result, model promotion, paper profit or page refresh can arm LIVE."]}


async def record_change(session: AsyncSession, *, user: str, to: str, previous: str, reason: str,
                        ev: dict | None, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    value = {"at": now.isoformat(), "by": user, "from": previous, "to": to, "reason": reason,
             "failed_checks": (ev or {}).get("failed")}
    row = await session.get(PlatformSetting, LAST_CHANGE_KEY)
    if row is None:
        session.add(PlatformSetting(key=LAST_CHANGE_KEY, value=value))
    else:
        row.value = value
    return value


async def publish_enforcement(session: AsyncSession, redis: Any, settings: Any, now: datetime | None = None) -> dict:
    """The worker's periodic run: the readiness result, stored with a TTL."""
    now = now or datetime.now(timezone.utc)
    ev = await evaluate(session, redis, settings, now)
    rec = {"ok": ev["readiness_ok"], "at": now.isoformat(),
           "failed": [c["code"] for c in ev["checks"] if c["status"] == FAIL and c["kind"] == "readiness"]}
    await redis.set(CONTROL_KEY, json.dumps(rec), ex=CONTROL_TTL)
    return rec


async def enforcement_ok(redis: Any, now: datetime | None = None) -> tuple[bool, str | None]:
    """For live_trading.live_readiness and the order worker."""
    now = now or datetime.now(timezone.utc)
    rec = await _json(redis, CONTROL_KEY)
    if not rec:
        return False, "live checks not run yet (the order worker runs them every 15 s)"
    age = _age(rec.get("at"), now)
    if age is None or age > CONTROL_TTL:
        return False, "live checks are stale"
    if not rec.get("ok"):
        return False, "a mandatory live check fails: " + ", ".join(rec.get("failed") or [])
    return True, None
