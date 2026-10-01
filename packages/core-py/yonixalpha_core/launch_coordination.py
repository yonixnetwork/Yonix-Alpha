"""Launch-window coordination (master upgrade §11), for every EVM launchpad
and with Pons V2 specifics.

A launch is not safe because it is active. This module looks at who bought
in a token's launch window (the first `window_seconds` after the launch
transaction; Pons V2's anti-snipe tax lasts at most 60 s) and at what those
wallets did afterwards, from the trades data-evm stored (all of them, the
launch itself included: a launch this system did not observe is reported
WINDOW_NOT_OBSERVED) plus a few read-only chain and explorer lookups:

  LAUNCH_BLOCK_BUNDLE          wallets other than the creator bought in the
                               launch transaction's own block
  NEAR_SIMULTANEOUS_BUYERS     many distinct wallets' first buys within
                               `simultaneous_seconds` of each other
  DECLARED_EXEMPTIONS          Pons V2: the launch transaction declared
                               snipe-tax exemptions (launchToken overload with
                               an exemption list, launchTokenFor, or the
                               official launchAndBuy router), i.e. special
                               treatment for listed wallets
  PRIVILEGED_BUYERS            Pons V2: wallets exempt from the snipe tax
                               (declared, or confirmed on chain: the curve's
                               currentSnipeTaxBps(wallet) is 0 at the block of
                               its buy while a reference wallet pays tax)
                               bought inside the window
  CREATOR_BOUGHT               the deployer, its fee recipient or the launch's
                               opening-buy recipient bought inside the window
  CREATOR_FUNDED_BUYERS        a window buyer's first funding came from the
                               creator (block explorer, see below)
  COMMON_FUNDER                several window buyers were first funded by
                               the same address
  FRESH_WALLET_CLUSTER         many window buyers had (almost) no transaction
                               history at the end of the window
  WINDOW_SUPPLY_CONCENTRATION  window buyers still hold a large share of the
                               supply (their curve buys minus curve sells)
  SINGLE_WALLET_CONCENTRATION  one window buyer holds a large share
  ABNORMAL_INITIAL_OWNERSHIP   Pons V2 mints the whole supply to the curve;
                               tokens outside the curve that recorded curve
                               trades do not explain
  COORDINATED_EXIT             several window buyers sold within
                               `exit_seconds` of each other
  COORDINATION_DATA_UNAVAILABLE  a core chain read (supply; Pons V2 launch
                               calldata and curve balance) failed

Each detection maps to an operator-configured action: NONE (report only),
REDUCE_SIZE, MANUAL_APPROVAL or NO_TRADE; the strictest action of the
findings applies. "NO_COORDINATION_DETECTED" means none of these checks
fired on the data available; it is not a safety verdict, and the ordinary
safety check (sellability, taxes, contract) still has to pass.

Holdings are followed through launchpad trades only (a transfer to another
wallet is not seen). Funding comes from a block explorer: Robinhood Chain's
public Blockscout, BSC through Etherscan API V2 (ETHERSCAN_API_KEY); without
one the funding checks are NOT_CONFIGURED, never "no common funder".
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from eth_abi import decode
from sqlalchemy import case, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core.chains.evm import dex
from yonixalpha_core.chains.evm.abi import selector
from yonixalpha_core.chains.evm.rpc import EvmRpcError, EvmRpcUnavailableError
from yonixalpha_core.db.models import EvmToken, EvmTrade, EvmWalletFunder, PlatformSetting
from yonixalpha_core.logging import get_logger

log = get_logger("core.launch_coordination")

SETTINGS_KEY = "launch_coordination"
NONE, REDUCE, MANUAL, NO_TRADE = "NONE", "REDUCE_SIZE", "MANUAL_APPROVAL", "NO_TRADE"
ACTIONS = (NONE, REDUCE, MANUAL, NO_TRADE)  # strictness order
FLAG, OK, UNKNOWN, NOT_APPLICABLE, NOT_CONFIGURED = "FLAG", "PASS", "UNKNOWN", "NOT_APPLICABLE", "NOT_CONFIGURED"
DETECTED, NOT_DETECTED, NOT_ASSESSED = "COORDINATION_DETECTED", "NO_COORDINATION_DETECTED", "NOT_ASSESSED"

DEFAULT_ACTIONS: dict[str, str] = {
    "LAUNCH_BLOCK_BUNDLE": NO_TRADE,
    "NEAR_SIMULTANEOUS_BUYERS": REDUCE,
    "DECLARED_EXEMPTIONS": REDUCE,
    "PRIVILEGED_BUYERS": NO_TRADE,
    "CREATOR_BOUGHT": REDUCE,
    "CREATOR_FUNDED_BUYERS": NO_TRADE,
    "COMMON_FUNDER": NO_TRADE,
    "FRESH_WALLET_CLUSTER": REDUCE,
    "WINDOW_SUPPLY_CONCENTRATION": NO_TRADE,
    "SINGLE_WALLET_CONCENTRATION": REDUCE,
    "ABNORMAL_INITIAL_OWNERSHIP": NO_TRADE,
    "COORDINATED_EXIT": NO_TRADE,
    "WINDOW_NOT_OBSERVED": NO_TRADE,
    "COORDINATION_DATA_UNAVAILABLE": NO_TRADE,
}

# Pons V2 launch entrypoints (github.com/ponsdotdev/pons-labs PonsV2LaunchFactory,
# and the launchAndBuy router ABI of github.com/slightlyuseless/pons-launch-engine).
PONS_V2_TOKEN_PARAMS = ("(string,string,string,string,(string,string,string,string,string),"
                        "address,uint16,bool,bytes32,bytes32)")
_P = PONS_V2_TOKEN_PARAMS
PONS_V2_LAUNCH_CALLS: dict[str, tuple[str, list[str], int | None, int | None]] = {
    # selector -> (entrypoint, argument types, exemption-list index, opening-buy recipient index)
    "0x" + selector(f"launchToken({_P},uint256,address)").hex():
        ("factory.launchToken", [_P, "uint256", "address"], None, None),
    "0x" + selector(f"launchToken({_P},uint256,address,address[])").hex():
        ("factory.launchToken (exemption list)", [_P, "uint256", "address", "address[]"], 3, None),
    "0x" + selector(f"launchTokenFor({_P},uint256,address,address,address[])").hex():
        ("factory.launchTokenFor (launch forwarder)", [_P, "uint256", "address", "address", "address[]"], 4, None),
    "0x" + selector(f"launchAndBuy({_P},uint256,address,uint256,uint256,address,address[])").hex():
        ("launchAndBuy router", [_P, "uint256", "address", "uint256", "uint256", "address", "address[]"], 6, 5),
}
SNIPE_REFERENCE = dex.SIM_ACCOUNT  # an address no launch exempts: the tax everyone else pays
BLOCKSCOUT = {"robinhood": "https://robinhoodchain.blockscout.com"}
ETHERSCAN_V2 = "https://api.etherscan.io/v2/api"
ETHERSCAN_CHAIN_ID = {"bsc": 56}


@dataclass(frozen=True)
class CoordinationConfig:
    enabled: bool = True
    window_seconds: int = 60
    launch_block_buyers_max: int = 1
    simultaneous_seconds: float = 2.0
    simultaneous_buyers_max: int = 4
    fresh_nonce_max: int = 3
    fresh_buyers_max: int = 3
    common_funder_min_wallets: int = 2
    window_supply_share_max: float = 0.25
    single_wallet_share_max: float = 0.08
    ownership_tolerance_share: float = 0.001
    exit_wallets_min: int = 3
    exit_seconds: int = 60
    reduce_size_factor: float = 0.5
    max_wallet_lookups: int = 16
    funding_lookups: bool = True
    approval_minutes: int = 60
    ignore_funders: tuple[str, ...] = ()
    actions: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_ACTIONS))

    def action(self, code: str) -> str:
        return self.actions.get(code, DEFAULT_ACTIONS.get(code, NO_TRADE))

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["ignore_funders"] = list(self.ignore_funders)
        return d


_SHARES = ("window_supply_share_max", "single_wallet_share_max", "ownership_tolerance_share", "reduce_size_factor")


def parse_config(data: dict | None) -> tuple[CoordinationConfig, list[str]]:
    base, errors, values = CoordinationConfig(), [], {}
    types = {f.name: f.type for f in fields(CoordinationConfig)}
    for k, v in (data or {}).items():
        if k not in types:
            errors.append(f"unknown setting {k}")
        elif k in ("enabled", "funding_lookups"):
            if not isinstance(v, bool):
                errors.append(f"{k}: must be true or false")
            else:
                values[k] = v
        elif k == "actions":
            if not isinstance(v, dict):
                errors.append("actions: an object of detection -> action")
                continue
            acts = dict(DEFAULT_ACTIONS)
            for code, a in v.items():
                if code not in DEFAULT_ACTIONS:
                    errors.append(f"actions: unknown detection {code}")
                elif a not in ACTIONS:
                    errors.append(f"actions.{code}: one of {', '.join(ACTIONS)}")
                else:
                    acts[code] = a
            values[k] = acts
        elif k == "ignore_funders":
            addrs = [str(a).strip() for a in (v or [])]
            bad = [a for a in addrs if not (a.startswith("0x") and len(a) == 42)]
            if bad:
                errors.append(f"ignore_funders: not addresses: {bad[:3]}")
            else:
                values[k] = tuple(a.lower() for a in addrs)
        else:
            try:
                num = int(v) if types[k] in ("int", int) else float(Decimal(str(v)))
            except (ValueError, TypeError, InvalidOperation):
                errors.append(f"{k}: not a number")
                continue
            if num < 0:
                errors.append(f"{k}: must be >= 0")
            elif k in _SHARES and num > 1:
                errors.append(f"{k}: a share between 0 and 1")
            elif k == "window_seconds" and not 1 <= num <= 3600:
                errors.append("window_seconds: between 1 and 3600")
            else:
                values[k] = num
    return CoordinationConfig(**{**asdict(base), **values}), errors


async def load_config(session: AsyncSession) -> CoordinationConfig:
    row = await session.get(PlatformSetting, SETTINGS_KEY)
    cfg, errors = parse_config(dict(row.value) if row else None)
    return CoordinationConfig() if errors else cfg


# --- pure analysis ---------------------------------------------------------------------------------

@dataclass(frozen=True)
class Trade:
    holder: str          # lower-case: the wallet that received the tokens (buy) / sold them
    is_buy: bool
    tokens: int
    quote: int
    block: int | None
    at: datetime


def _check(code: str, status: str, message: str, value: Any = None, limit: Any = None, source: str = "",
           wallets: list[str] | None = None) -> dict[str, Any]:
    return {"check": code, "status": status, "message": message, "value": value, "limit": limit, "source": source,
            "wallets": wallets or []}


def _max_in_span(times: list[tuple[datetime, str]], seconds: float) -> tuple[int, list[str]]:
    """Most distinct wallets with an event inside any `seconds` span."""
    times = sorted(times)
    best: tuple[int, list[str]] = (0, [])
    j = 0
    for i in range(len(times)):
        while times[i][0] - times[j][0] > timedelta(seconds=seconds):
            j += 1
        ws = sorted({w for _, w in times[j:i + 1]})
        if len(ws) > best[0]:
            best = (len(ws), ws)
    return best


def _share(n: int | Decimal, d: int | None) -> float | None:
    return round(float(Decimal(n) / Decimal(d)), 6) if d else None


def analyse(launch: dict[str, Any], trades: list[Trade], facts: dict[str, Any], cfg: CoordinationConfig) -> dict[str, Any]:
    """The checks of the module doc over one launch. `trades`: the window's
    buys and every later trade of the wallets that bought in the window."""
    checks: list[dict[str, Any]] = []
    base = {"window_seconds": cfg.window_seconds, "launchpad": launch.get("launchpad")}
    if not launch.get("launch_seen") or launch.get("created_block") is None:
        checks.append(_check("WINDOW_NOT_OBSERVED", FLAG, "the launch predates this system's discovery: who bought in "
                             "the launch window is not known", source="evm_tokens.launch_seen"))
        return _result(checks, cfg, wallets=[], window=base, facts=facts)
    start = launch["created_at"]
    end = start + timedelta(seconds=cfg.window_seconds)
    creator_set = {a.lower() for a in (launch.get("creator"), facts.get("fee_recipient"), facts.get("launch_sender"),
                                       (facts.get("exemptions") or {}).get("opening_recipient")) if a}
    w: dict[str, dict[str, Any]] = {}
    for t in sorted(trades, key=lambda t: t.at):
        if t.is_buy and t.at <= end:
            h = w.setdefault(t.holder, {"wallet": t.holder, "first_at": t.at, "first_block": t.block, "bought": 0,
                                        "window_quote": 0, "sold": 0, "sells": []})
            h["bought"] += t.tokens
            h["window_quote"] += t.quote
    for t in trades:
        h = w.get(t.holder)
        if h is None:
            continue
        if t.is_buy and t.at > end:
            h["bought"] += t.tokens
        elif not t.is_buy:
            h["sold"] += t.tokens
            h["sells"].append(t.at)
    supply = facts.get("supply")
    others = [h for k, h in w.items() if k not in creator_set]

    # Launch block and near-simultaneous buyers.
    lb = sorted(h["wallet"] for h in others if h["first_block"] == launch["created_block"])
    checks.append(_check("LAUNCH_BLOCK_BUNDLE", FLAG if len(lb) > cfg.launch_block_buyers_max else OK,
                         f"{len(lb)} wallet(s) other than the creator bought in the launch block", len(lb),
                         cfg.launch_block_buyers_max, "evm_trades", lb))
    n_sim, sim = _max_in_span([(h["first_at"], h["wallet"]) for h in others], cfg.simultaneous_seconds)
    checks.append(_check("NEAR_SIMULTANEOUS_BUYERS", FLAG if n_sim > cfg.simultaneous_buyers_max else OK,
                         f"{n_sim} wallet(s) made their first buy within {cfg.simultaneous_seconds:g}s of each other",
                         n_sim, cfg.simultaneous_buyers_max, "evm_trades", sim))

    # Snipe-tax exemptions (Pons V2).
    ex = facts.get("exemptions") or {"status": NOT_APPLICABLE}
    snipe = facts.get("snipe") or {}
    if ex["status"] == NOT_APPLICABLE:
        checks.append(_check("DECLARED_EXEMPTIONS", NOT_APPLICABLE, "this launchpad has no exemption list"))
    elif ex["status"] != "READ":
        checks.append(_check("DECLARED_EXEMPTIONS", UNKNOWN, ex.get("detail") or "launch calldata not readable",
                             source=ex.get("via") or "eth_getTransactionByHash"))
    else:
        declared = sorted({a.lower() for a in ex.get("declared", [])} - creator_set)
        checks.append(_check("DECLARED_EXEMPTIONS", FLAG if declared else OK,
                             f"{len(declared)} wallet(s) besides the creator declared exempt from the snipe tax "
                             f"({ex.get('via')})", len(declared), 0, ex.get("via", ""), declared))
    if ex["status"] == NOT_APPLICABLE:
        checks.append(_check("PRIVILEGED_BUYERS", NOT_APPLICABLE, "this launchpad has no snipe-tax exemptions"))
    else:
        declared_set = {a.lower() for a in ex.get("declared", [])}
        confirmed = {k for k, v in snipe.items() if v.get("exempt") is True}
        priv = sorted(h["wallet"] for h in others if h["wallet"] in declared_set | confirmed)
        unread = [k for k, v in snipe.items() if v.get("exempt") is None and k not in declared_set]
        status = FLAG if priv else (UNKNOWN if ex["status"] != "READ" and unread else OK)
        checks.append(_check("PRIVILEGED_BUYERS", status,
                             f"{len(priv)} snipe-tax-exempt wallet(s) bought in the window"
                             + (f"; exemption of {len(unread)} buyer(s) could not be read" if unread else ""),
                             len(priv), 0, "launch calldata + curve.currentSnipeTaxBps at the buy's block", priv))

    # Creator-linked buys.
    cb = sorted(k for k in w if k in creator_set)
    checks.append(_check("CREATOR_BOUGHT", FLAG if cb else OK,
                         f"creator-linked wallet(s) bought in the window: {len(cb)}", len(cb), 0, "evm_trades", cb))

    # Funding.
    funders = facts.get("funders") or {}
    fstat = facts.get("funding_status", NOT_CONFIGURED)
    known = {k: v["funder"].lower() for k, v in funders.items() if v.get("funder") and k in w}
    if fstat == NOT_CONFIGURED:
        for code in ("CREATOR_FUNDED_BUYERS", "COMMON_FUNDER"):
            checks.append(_check(code, NOT_CONFIGURED, facts.get("funding_detail") or "no block explorer configured "
                                 "for this chain: funding not checked", source="block explorer"))
    else:
        unread = [k for k in funders if not funders[k].get("funder") and funders[k].get("status") != "NOT_FOUND"]
        cf = sorted(k for k, f in known.items() if f in creator_set and k not in creator_set)
        checks.append(_check("CREATOR_FUNDED_BUYERS", FLAG if cf else (UNKNOWN if unread and not known else OK),
                             f"{len(cf)} window buyer(s) first funded by a creator-linked wallet "
                             f"({len(known)} funder(s) known, {len(unread)} unreadable)", len(cf), 0,
                             facts.get("funding_source", "block explorer"), cf))
        groups: dict[str, list[str]] = {}
        for k, f in known.items():
            if f not in cfg.ignore_funders:
                groups.setdefault(f, []).append(k)
        shared = {f: sorted(ws) for f, ws in groups.items() if len(ws) >= cfg.common_funder_min_wallets}
        top = max(shared.items(), key=lambda kv: len(kv[1]), default=(None, []))
        checks.append(_check("COMMON_FUNDER", FLAG if shared else (UNKNOWN if unread and not known else OK),
                             f"{sum(len(v) for v in shared.values())} window buyer(s) share a first funder"
                             + (f" (largest group: {len(top[1])} funded by {top[0]})" if shared else ""),
                             max((len(v) for v in shared.values()), default=0), cfg.common_funder_min_wallets,
                             facts.get("funding_source", "block explorer"), sorted({x for v in shared.values() for x in v})))

    # Fresh wallets.
    nonces = facts.get("nonces") or {}
    readable = {k: v["nonce"] for k, v in nonces.items() if v.get("nonce") is not None and k in w}
    fresh = sorted(k for k, n in readable.items() if n <= cfg.fresh_nonce_max and k not in creator_set)
    if not readable and nonces:
        checks.append(_check("FRESH_WALLET_CLUSTER", UNKNOWN, "transaction counts could not be read",
                             source="eth_getTransactionCount"))
    else:
        checks.append(_check("FRESH_WALLET_CLUSTER", FLAG if len(fresh) > cfg.fresh_buyers_max else OK,
                             f"{len(fresh)} of {len(readable)} checked window buyer(s) had at most "
                             f"{cfg.fresh_nonce_max} transactions at the end of the window", len(fresh),
                             cfg.fresh_buyers_max, "eth_getTransactionCount", fresh))

    # Supply concentration.
    held = {k: max(0, h["bought"] - h["sold"]) for k, h in w.items()}
    if not supply:
        for code in ("WINDOW_SUPPLY_CONCENTRATION", "SINGLE_WALLET_CONCENTRATION"):
            checks.append(_check(code, UNKNOWN, "total supply not readable", source="totalSupply()"))
    else:
        total = sum(held.values())
        share = _share(total, supply)
        checks.append(_check("WINDOW_SUPPLY_CONCENTRATION", FLAG if share > cfg.window_supply_share_max else OK,
                             f"window buyers still hold {share:.2%} of the supply (launchpad trades only)", share,
                             cfg.window_supply_share_max, "evm_trades + totalSupply()"))
        top_w, top_h = max(held.items(), key=lambda kv: kv[1], default=(None, 0))
        top_s = _share(top_h, supply) or 0.0
        checks.append(_check("SINGLE_WALLET_CONCENTRATION", FLAG if top_s > cfg.single_wallet_share_max else OK,
                             f"largest window buyer holds {top_s:.2%} of the supply", top_s,
                             cfg.single_wallet_share_max, "evm_trades + totalSupply()", [top_w] if top_w else []))

    # Initial ownership (Pons V2: the whole supply is minted to the curve).
    own = facts.get("ownership")
    if own is None:
        checks.append(_check("ABNORMAL_INITIAL_OWNERSHIP", NOT_APPLICABLE,
                             "checked for Pons V2 curves (the whole supply starts in the curve)"))
    elif own.get("status") != "READ":
        checks.append(_check("ABNORMAL_INITIAL_OWNERSHIP", UNKNOWN, own.get("detail") or "curve balance not readable",
                             source="balanceOf(curve)"))
    else:
        outside = own["supply"] - own["curve_balance"]
        unexplained = outside - own["net_curve_buys"]
        frac = _share(max(unexplained, 0), own["supply"]) or 0.0
        checks.append(_check("ABNORMAL_INITIAL_OWNERSHIP", FLAG if frac > cfg.ownership_tolerance_share else OK,
                             f"{frac:.4%} of the supply is outside the curve without a recorded curve buy "
                             f"(block {own['block']})", frac, cfg.ownership_tolerance_share,
                             "totalSupply() - balanceOf(curve) vs evm_trades"))

    # Coordinated exits.
    n_exit, exiting = _max_in_span([(s, k) for k, h in w.items() for s in h["sells"]], cfg.exit_seconds)
    sold = sum(min(h["sold"], h["bought"]) for h in w.values())
    bought = sum(h["bought"] for h in w.values())
    checks.append(_check("COORDINATED_EXIT", FLAG if n_exit >= cfg.exit_wallets_min else OK,
                         f"{n_exit} window buyer(s) sold within {cfg.exit_seconds}s of each other; window buyers "
                         f"sold {(_share(sold, bought) or 0):.0%} of what they bought", n_exit, cfg.exit_wallets_min,
                         "evm_trades", exiting))

    if facts.get("unavailable"):
        checks.append(_check("COORDINATION_DATA_UNAVAILABLE", FLAG, "; ".join(facts["unavailable"])[:300],
                             source="RPC"))
    roles = _roles(w, creator_set, ex, snipe, known, nonces, cfg)
    wallets = sorted(({**{k: v for k, v in h.items() if k != "sells"}, "held": max(0, h["bought"] - h["sold"]),
                       "held_share": _share(max(0, h["bought"] - h["sold"]), supply), "sells": len(h["sells"]),
                       "roles": roles.get(k, []), "funder": known.get(k),
                       "nonce": (nonces.get(k) or {}).get("nonce")} for k, h in w.items()),
                     key=lambda r: (-r["held"], r["first_at"]))[:32]
    window = {**base, "start": start, "end": end, "buyers": len(w), "launch_block": launch["created_block"],
              "bought_tokens": bought, "supply": supply,
              "basis": "launchpad trades stored by data-evm; transfers between wallets are not followed"}
    return _result(checks, cfg, wallets=wallets, window=window, facts=facts)


def _roles(w, creator_set, ex, snipe, known, nonces, cfg) -> dict[str, list[str]]:
    declared = {a.lower() for a in ex.get("declared", [])}
    out: dict[str, list[str]] = {}
    for k in w:
        r = []
        if k in creator_set:
            r.append("CREATOR_LINKED")
        if k in declared:
            r.append("DECLARED_EXEMPT")
        if (snipe.get(k) or {}).get("exempt") is True:
            r.append("CONFIRMED_EXEMPT")
        if known.get(k) in creator_set:
            r.append("CREATOR_FUNDED")
        n = (nonces.get(k) or {}).get("nonce")
        if n is not None and n <= cfg.fresh_nonce_max:
            r.append("FRESH_WALLET")
        out[k] = r
    return out


def _result(checks, cfg: CoordinationConfig, *, wallets, window, facts) -> dict[str, Any]:
    findings = [{"code": c["check"], "action": cfg.action(c["check"]), "message": c["message"]}
                for c in checks if c["status"] == FLAG]
    action = max((f["action"] for f in findings), key=ACTIONS.index, default=NONE)
    fp = hashlib.sha256(",".join(sorted(f"{f['code']}:{f['action']}" for f in findings)).encode()).hexdigest()[:16]
    return {"status": DETECTED if findings else NOT_DETECTED, "action": action, "findings": findings,
            "checks": checks, "wallets": wallets, "window": window, "facts": facts, "fingerprint": fp,
            "config": cfg.to_dict(),
            "note": "NO_COORDINATION_DETECTED means none of these checks fired on the data available; "
                    "it is not a safety verdict"}


# --- entry effect ----------------------------------------------------------------------------------

@dataclass(frozen=True)
class Effect:
    blocker: str | None
    message: str
    size_factor: Decimal = Decimal(1)


def entry_effect(result: dict | None, assessed_at: datetime | None, approval: dict | None, cfg: CoordinationConfig,
                 now: datetime, max_age: timedelta) -> Effect:
    """What an assessment means for an automatic (or copy) paper entry."""
    if not cfg.enabled:
        return Effect(None, "launch-coordination check switched off")
    if result is None or assessed_at is None or now - assessed_at > max_age:
        return Effect("COORDINATION_NOT_CHECKED", "launch-coordination check missing or older than "
                      f"{int(max_age.total_seconds() // 60)} minutes")
    action = result.get("action", NONE)
    codes = ", ".join(f["code"] for f in result.get("findings", []))
    reduce = any(f["action"] == REDUCE for f in result.get("findings", []))
    factor = Decimal(str(cfg.reduce_size_factor)) if reduce else Decimal(1)
    if action == NO_TRADE:
        return Effect("LAUNCH_COORDINATION", f"launch-window coordination: {codes}")
    if action == MANUAL:
        if not approval_valid(approval, result, now):
            return Effect("COORDINATION_MANUAL_APPROVAL", f"needs operator approval: {codes}")
        return Effect(None, f"approved by {approval.get('by')}: {codes}", factor)
    if reduce:
        return Effect(None, f"size x{cfg.reduce_size_factor:g}: {codes}", factor)
    return Effect(None, "no coordination action")


def approval_valid(approval: dict | None, result: dict, now: datetime) -> bool:
    """An approval covers exactly the findings it was given for, until it expires."""
    if not approval or approval.get("fingerprint") != result.get("fingerprint"):
        return False
    try:
        return datetime.fromisoformat(approval["expires_at"]) > now
    except (KeyError, TypeError, ValueError):
        return False


# --- chain and explorer facts ----------------------------------------------------------------------

def decode_pons_v2_launch(tx_input: str) -> dict[str, Any]:
    """Declared exemptions (and the router's opening-buy recipient) from a
    Pons V2 launch transaction's calldata."""
    sel = (tx_input or "")[:10].lower()
    spec = PONS_V2_LAUNCH_CALLS.get(sel)
    if spec is None:
        return {"status": UNKNOWN, "selector": sel, "detail": f"launch sent through an unrecognised entrypoint "
                f"(selector {sel}); exemption list not readable", "declared": []}
    name, types, ex_i, rcpt_i = spec
    try:
        args = decode(types, bytes.fromhex(tx_input[10:]))
    except Exception as exc:  # noqa: BLE001 - malformed calldata is reported, never trusted
        return {"status": UNKNOWN, "selector": sel, "via": name, "detail": f"calldata not decodable: {exc}"[:200],
                "declared": []}
    params = args[0]
    return {"status": "READ", "selector": sel, "via": name,
            "declared": [a.lower() for a in (args[ex_i] if ex_i is not None else ())],
            "opening_recipient": args[rcpt_i].lower() if rcpt_i is not None else None,
            "creator_fee_recipient_at_launch": params[5].lower(), "creator_tax_bps": params[6]}


async def _nonce(rpc, wallet: str, block: int | None) -> dict[str, Any]:
    for tag in ([hex(block)] if block else []) + ["latest"]:
        try:
            return {"nonce": int(await rpc.call("eth_getTransactionCount", [wallet, tag]), 16), "at": tag}
        except EvmRpcUnavailableError:
            raise
        except Exception:  # noqa: BLE001 - no historical state on this node: fall back to latest
            continue
    return {"nonce": None, "at": None}


async def _snipe(rpc, curve: str, wallet: str, block: int) -> dict[str, Any]:
    try:
        (own,) = await dex.call(rpc, curve, "currentSnipeTaxBps(address)", ["uint256"], wallet, block=hex(block))
        (ref,) = await dex.call(rpc, curve, "currentSnipeTaxBps(address)", ["uint256"], SNIPE_REFERENCE,
                                block=hex(block))
    except EvmRpcUnavailableError:
        raise
    except Exception as exc:  # noqa: BLE001 - no such view or no historical state: unknown, not exempt
        return {"exempt": None, "block": block, "detail": str(exc)[:120]}
    if ref == 0:
        return {"exempt": False, "block": block, "tax_bps": own, "reference_bps": ref,
                "detail": "no snipe tax for anyone at that block"}
    return {"exempt": own == 0, "block": block, "tax_bps": own, "reference_bps": ref}


async def first_funding(client: httpx.AsyncClient, chain: str, wallet: str, etherscan_key: str | None) -> dict[str, Any]:
    """{status: FOUND / NOT_FOUND / UNAVAILABLE / NOT_CONFIGURED, funder, funded_at, tx_hash, source}."""
    if chain in BLOCKSCOUT:
        return await _blockscout_funding(client, BLOCKSCOUT[chain], wallet)
    if chain in ETHERSCAN_CHAIN_ID:
        if not etherscan_key:
            return {"status": NOT_CONFIGURED, "source": "Etherscan API V2 (ETHERSCAN_API_KEY not set)"}
        return await _etherscan_funding(client, ETHERSCAN_CHAIN_ID[chain], wallet, etherscan_key)
    return {"status": NOT_CONFIGURED, "source": "no explorer for this chain"}


def _ts(v: Any) -> datetime | None:
    try:
        if isinstance(v, str) and not v.isdigit():
            return datetime.fromisoformat(v.replace("Z", "+00:00"))
        return datetime.fromtimestamp(int(v), timezone.utc)
    except (TypeError, ValueError):
        return None


async def _blockscout_funding(client, base: str, wallet: str) -> dict[str, Any]:
    src = f"Blockscout {base}"
    best: dict[str, Any] | None = None
    try:
        for kind in ("transactions", "internal-transactions"):
            r = await client.get(f"{base}/api/v2/addresses/{wallet}/{kind}", params={"filter": "to"})
            if r.status_code == 404:
                return {"status": "NOT_FOUND", "source": src}
            if r.status_code != 200:
                return {"status": "UNAVAILABLE", "source": src, "detail": f"HTTP {r.status_code}"}
            body = r.json()
            if body.get("next_page_params"):
                return {"status": "NOT_FOUND", "source": src, "detail": "more than one page of incoming transfers: "
                        "not a fresh wallet, first funder not looked up"}
            for it in body.get("items") or []:
                if int(it.get("value") or 0) <= 0:
                    continue
                at = _ts(it.get("timestamp"))
                funder = (it.get("from") or {}).get("hash")
                tx = it.get("hash") or it.get("transaction_hash")
                if kind == "internal-transactions" and tx:  # a disperse contract: the funder is who called it
                    tr = await client.get(f"{base}/api/v2/transactions/{tx}")
                    if tr.status_code == 200:
                        funder = ((tr.json() or {}).get("from") or {}).get("hash") or funder
                if funder and at and (best is None or at < best["funded_at"]):
                    best = {"status": "FOUND", "funder": funder.lower(), "funded_at": at, "tx_hash": tx,
                            "source": src, "via_contract": kind == "internal-transactions"}
    except (httpx.HTTPError, ValueError) as exc:
        return {"status": "UNAVAILABLE", "source": src, "detail": type(exc).__name__}
    return best or {"status": "NOT_FOUND", "source": src}


async def _etherscan_funding(client, chain_id: int, wallet: str, key: str) -> dict[str, Any]:
    src = "Etherscan API V2"
    best: dict[str, Any] | None = None
    try:
        for action in ("txlist", "txlistinternal"):
            r = await client.get(ETHERSCAN_V2, params={"chainid": chain_id, "module": "account", "action": action,
                                                       "address": wallet, "sort": "asc", "page": 1, "offset": 20,
                                                       "apikey": key})
            if r.status_code != 200:
                return {"status": "UNAVAILABLE", "source": src, "detail": f"HTTP {r.status_code}"}
            body = r.json()
            items = body.get("result")
            if not isinstance(items, list):  # an error message (bad key, plan, rate limit)
                if "no transactions" in str(body.get("message", "")).lower():
                    continue
                return {"status": "UNAVAILABLE", "source": src, "detail": str(items or body.get("message"))[:120]}
            for it in items:
                if (it.get("to") or "").lower() != wallet.lower() or int(it.get("value") or 0) <= 0:
                    continue
                at, funder = _ts(it.get("timeStamp")), (it.get("from") or "").lower()
                if action == "txlistinternal":
                    funder = None  # the caller of the disperse contract is resolved below
                    tr = await client.get(ETHERSCAN_V2, params={"chainid": chain_id, "module": "proxy",
                                                                "action": "eth_getTransactionByHash",
                                                                "txhash": it.get("hash"), "apikey": key})
                    if tr.status_code == 200 and isinstance((tr.json() or {}).get("result"), dict):
                        funder = (tr.json()["result"].get("from") or "").lower() or None
                if funder and at and (best is None or at < best["funded_at"]):
                    best = {"status": "FOUND", "funder": funder, "funded_at": at, "tx_hash": it.get("hash"),
                            "source": src, "via_contract": action == "txlistinternal"}
                break  # ascending: the first incoming transfer of this list is enough
    except (httpx.HTTPError, ValueError) as exc:
        return {"status": "UNAVAILABLE", "source": src, "detail": type(exc).__name__}
    return best or {"status": "NOT_FOUND", "source": src}


async def _funders(session: AsyncSession, chain: str, wallets: list[str], client, key: str | None,
                   now: datetime) -> tuple[str, dict[str, dict], str]:
    cached = {r.wallet: r for r in (await session.execute(select(EvmWalletFunder).where(
        EvmWalletFunder.chain == chain, EvmWalletFunder.wallet.in_(wallets)))).scalars()}
    out: dict[str, dict] = {}
    status, source = "CHECKED", ""
    for wlt in wallets:
        r = cached.get(wlt)
        if r is not None and (r.status != "UNAVAILABLE" or now - r.checked_at < timedelta(minutes=10)):
            out[wlt] = {"status": r.status, "funder": r.funder, "source": (r.detail or {}).get("source")}
            continue
        f = await first_funding(client, chain, wlt, key)
        if f["status"] == NOT_CONFIGURED:
            return NOT_CONFIGURED, {}, f.get("source", "")
        source = f.get("source", source)
        out[wlt] = {"status": f["status"], "funder": f.get("funder"), "source": f.get("source")}
        stmt = insert(EvmWalletFunder).values(
            chain=chain, wallet=wlt, status=f["status"], funder=f.get("funder"), funded_at=f.get("funded_at"),
            tx_hash=f.get("tx_hash"), checked_at=now,
            detail={k: v for k, v in f.items() if k in ("source", "detail", "via_contract")})
        await session.execute(stmt.on_conflict_do_update(index_elements=["chain", "wallet"], set_={
            k: stmt.excluded[k] for k in ("status", "funder", "funded_at", "tx_hash", "checked_at", "detail")}))
    return status, out, source


def _holder(t: EvmTrade) -> str:
    """Pons V2 curve events name the msg.sender (a router for router buys) and
    the recipient; the recipient is who holds the tokens."""
    return ((t.extra or {}).get("recipient") or t.trader).lower()


async def assess(session: AsyncSession, adapter, row: EvmToken, now: datetime, cfg: CoordinationConfig, *,
                 client: httpx.AsyncClient | None = None, etherscan_key: str | None = None) -> dict[str, Any]:
    """Collects the facts for `row` and runs `analyse`. Facts that never change
    (launch calldata, per-wallet exemption and nonce at the window's end) are
    reused from the previous assessment. Caller stores the result and commits."""
    rpc = adapter.rpc
    chain, key = row.chain, row.launchpad
    launch = {"launchpad": key, "launch_seen": bool((row.extra or {}).get("launch_seen")),
              "created_at": row.created_at, "created_block": row.created_block, "creator": row.creator}
    prev = (row.coordination or {}).get("facts") or {}
    facts: dict[str, Any] = {"unavailable": []}
    if not launch["launch_seen"] or row.created_block is None:
        return analyse(launch, [], facts, cfg)
    end = row.created_at + timedelta(seconds=cfg.window_seconds)
    tr = EvmTrade
    holder_col = func.lower(func.coalesce(tr.extra["recipient"].astext, tr.trader))
    window = (await session.execute(select(tr).where(
        tr.chain == chain, tr.token == row.token, tr.is_buy.is_(True), tr.at <= end).order_by(tr.at))).scalars().all()
    holders = sorted({_holder(t) for t in window})
    later = (await session.execute(select(tr).where(
        tr.chain == chain, tr.token == row.token, or_(tr.at > end, tr.is_buy.is_(False)),
        holder_col.in_(holders)))).scalars().all() if holders else []
    trades = [Trade(_holder(t), t.is_buy, int(t.token_amount), int(t.quote_amount), t.block, t.at)
              for t in [*window, *later]]
    last_window_block = max((t.block for t in window if t.block), default=row.created_block)
    by_size = sorted(holders, key=lambda h: -sum(t.tokens for t in trades if t.holder == h and t.is_buy))
    lookups = by_size[:cfg.max_wallet_lookups]

    try:
        (facts["supply"],) = await dex.call(rpc, row.token, "totalSupply()", ["uint256"])
    except EvmRpcUnavailableError:
        raise
    except EvmRpcError as exc:
        facts["supply"] = None
        facts["unavailable"].append(f"totalSupply(): {str(exc)[:80]}")

    if key == "pons_v2":
        curve = (row.venue or {}).get("curve")
        if prev.get("exemptions", {}).get("status") == "READ":
            facts["exemptions"] = prev["exemptions"]
            facts["launch_sender"] = prev.get("launch_sender")
        elif row.created_tx:
            try:
                tx = await rpc.call("eth_getTransactionByHash", [row.created_tx])
            except EvmRpcError:
                tx = None
            if not tx:
                facts["exemptions"] = {"status": UNKNOWN, "detail": "launch transaction not returned by the RPC"}
                facts["unavailable"].append("launch transaction not readable")
            else:
                facts["exemptions"] = decode_pons_v2_launch(tx.get("input") or "")
                facts["exemptions"]["to"] = (tx.get("to") or "").lower()
                facts["launch_sender"] = (tx.get("from") or "").lower() or None
        else:
            facts["exemptions"] = {"status": UNKNOWN, "detail": "launch transaction hash not recorded"}
        try:
            lt = await adapter.launched(row.token)
            facts["fee_recipient"] = (lt.get("creator_fee_recipient") or "").lower() or None
        except EvmRpcError:
            facts["fee_recipient"] = None
        snipe = dict(prev.get("snipe") or {})
        if curve:
            first_block = {}
            for t in sorted(window, key=lambda t: t.at):
                first_block.setdefault(_holder(t), t.block)
            try:
                for h in lookups:
                    if h not in snipe or snipe[h].get("exempt") is None and snipe[h].get("retry", 0) < 2:
                        if first_block.get(h):
                            r = await _snipe(rpc, curve, h, first_block[h])
                            r["retry"] = (snipe.get(h) or {}).get("retry", 0) + (1 if r["exempt"] is None else 0)
                            snipe[h] = r
            except EvmRpcUnavailableError as exc:  # enrichment: what is missing stays unknown
                facts["enrichment_stopped"] = f"snipe-tax lookups: {str(exc)[:120]}"
            facts["snipe"] = snipe
            if facts.get("supply") and row.stage == "CURVE":
                facts["ownership"] = await _ownership(session, rpc, row, curve, facts["supply"])
                if facts["ownership"]["status"] != "READ":
                    facts["unavailable"].append("curve balance not readable")
        else:
            facts["unavailable"].append("Pons V2 curve address unknown")
    nonces = dict(prev.get("nonces") or {})
    try:
        for h in lookups:
            if (nonces.get(h) or {}).get("nonce") is None:
                nonces[h] = await _nonce(rpc, h, last_window_block)
    except EvmRpcUnavailableError as exc:
        facts["enrichment_stopped"] = f"transaction counts: {str(exc)[:120]}"
    facts["nonces"] = nonces
    if cfg.funding_lookups and lookups:
        own = client is None
        client = client or httpx.AsyncClient(timeout=10.0)
        try:
            status, funders, source = await _funders(session, chain, lookups, client, etherscan_key, now)
        finally:
            if own:
                await client.aclose()
        facts["funding_status"], facts["funders"], facts["funding_source"] = status, funders, source
        if status == NOT_CONFIGURED:
            facts["funding_detail"] = f"funding not checked: {source}"
    else:
        facts["funding_status"] = NOT_CONFIGURED
        facts["funding_detail"] = "funding lookups switched off" if not cfg.funding_lookups else "no window buyers"
    if not facts["unavailable"]:
        facts.pop("unavailable")
    return analyse(launch, trades, facts, cfg)


async def _ownership(session: AsyncSession, rpc, row: EvmToken, curve: str, supply: int) -> dict[str, Any]:
    """Tokens outside the curve vs net curve buys, both at the last block whose
    trades are stored (the discovery cursor covers it)."""
    tr = EvmTrade
    block = (await session.execute(select(func.max(tr.block)).where(tr.chain == row.chain, tr.token == row.token))).scalar()
    if block is None:
        return {"status": "READ", "supply": supply, "curve_balance": supply, "net_curve_buys": 0, "block": None}
    net = (await session.execute(select(func.coalesce(func.sum(case((tr.is_buy, tr.token_amount),
                                                                    else_=-tr.token_amount)), 0)).where(
        tr.chain == row.chain, tr.token == row.token, tr.block <= block))).scalar()
    try:
        (bal,) = await dex.call(rpc, row.token, "balanceOf(address)", ["uint256"], curve, block=hex(block))
    except EvmRpcUnavailableError:
        raise
    except Exception as exc:  # noqa: BLE001 - no historical state: unknown
        return {"status": UNKNOWN, "detail": f"balanceOf(curve) at block {block}: {str(exc)[:100]}"}
    return {"status": "READ", "supply": supply, "curve_balance": bal, "net_curve_buys": int(net), "block": block}
