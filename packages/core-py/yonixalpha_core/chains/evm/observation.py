"""EVM token observation (master upgrade §14-17).

Every BSC / Robinhood token enters OBSERVATION before it can be traded,
waited on, rejected or expired, once per category it reaches (FRESH from its
launch, MIGRATED from its migration, MOMENTUM when it qualifies): one
evm_observations row per (chain, token, category), never deleted.

  DISCOVERED -> OBSERVING -> ANALYZING -> QUALIFIED -> WAITING_FOR_ENTRY
             -> ENTRY_PENDING -> ENTERED
  OBSERVING  -> NO_ENTRY -> EXPIRED            (deadline, no entry)
  OBSERVING  -> SAFETY_FAILURE -> REJECTED     (safety FAIL on
                                                 `reject_after_safety_failures`
                                                 consecutive checks; one failed
                                                 check is SAFETY_FAILURE only,
                                                 UNKNOWN never rejects)

  ANALYZING          the entry pass evaluated it and its trade signal is not
                     met (yet)
  QUALIFIED          safety PASS and the signal met
  WAITING_FOR_ENTRY  qualified, but held by something other than the token
                     (account limits, launchpad evidence, a coordination
                     approval, ...); the blocker is recorded
  ENTRY_PENDING / ENTERED  the paper entry is being / was opened

Snapshots at T0, T+5, T+10, T+20, T+30 and T+60 minutes after the
observation started (configurable; also after an entry, until the window
ends: the path after entry is outcome data), each computed as of that
moment from stored trades, with the §16 fields this system can
measure from stored trades and the token row; a field it cannot measure is
None with the reason, never 0. MIGRATED and MOMENTUM windows are adaptive:
while the token keeps trading (>= `active_min_trades` in the last 5
minutes) the deadline moves out by `extend_minutes`, up to
`max_window_minutes`. A deadline without an entry is EXPIRED with
expiry_reason EXPIRED_NO_ENTRY (and the last blocker), kept for ML.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core.db.models import EvmObservation, EvmToken, EvmTrade, PlatformSetting, WalletProfile

SETTINGS_KEY = "evm_observation"
CATEGORIES = ("FRESH", "MIGRATED", "MOMENTUM")
DISCOVERED, OBSERVING, ANALYZING, QUALIFIED, WAITING, PENDING, ENTERED = (
    "DISCOVERED", "OBSERVING", "ANALYZING", "QUALIFIED", "WAITING_FOR_ENTRY", "ENTRY_PENDING", "ENTERED")
NO_ENTRY, EXPIRED, SAFETY_FAILURE, REJECTED = "NO_ENTRY", "EXPIRED", "SAFETY_FAILURE", "REJECTED"
STATES = (DISCOVERED, OBSERVING, ANALYZING, QUALIFIED, WAITING, PENDING, ENTERED, NO_ENTRY, EXPIRED,
          SAFETY_FAILURE, REJECTED)
TERMINAL = (ENTERED, EXPIRED, REJECTED)
# Entry blockers that mean "the token's own signal is not there yet".
SIGNAL_BLOCKERS = {"TOO_FEW_BUYS", "TOO_FEW_BUYERS", "SELLING_PRESSURE", "CATEGORY_NOT_TRADED"}
SAFETY_BLOCKERS = {"SAFETY_NOT_PASSED", "SAFETY_STALE", "LAUNCH_COORDINATION", "COORDINATION_NOT_CHECKED",
                   "NO_EXECUTABLE_ROUND_TRIP", "ROUND_TRIP_EXCEEDS_STOP_BUDGET", "LIQUIDITY_TOO_LOW"}
HISTORY_MAX = 40
SMART_STAGES = ("VALIDATED", "PAPER_FOLLOWED")


@dataclass(frozen=True)
class ObservationConfig:
    snapshots_min: tuple[int, ...] = (0, 5, 10, 20, 30, 60)
    window_min: dict[str, int] = field(default_factory=lambda: {"FRESH": 60, "MIGRATED": 60, "MOMENTUM": 60})
    adaptive_categories: tuple[str, ...] = ("MIGRATED", "MOMENTUM")
    active_min_trades: int = 5
    extend_minutes: int = 10
    max_window_minutes: int = 180
    reject_after_safety_failures: int = 3

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["snapshots_min"] = list(self.snapshots_min)
        d["adaptive_categories"] = list(self.adaptive_categories)
        return d


def parse_config(data: dict | None) -> tuple[ObservationConfig, list[str]]:
    base, errors, values = ObservationConfig(), [], {}
    for k, v in (data or {}).items():
        try:
            if k == "snapshots_min":
                mins = sorted({int(x) for x in v})
                if not mins or mins[0] != 0 or mins[-1] > 1440:
                    raise ValueError("starts at 0 (T0), at most 1440")
                values[k] = tuple(mins)
            elif k == "window_min":
                w = {**base.window_min, **{str(c).upper(): int(m) for c, m in dict(v).items()}}
                if set(w) - set(CATEGORIES) or any(not 1 <= m <= 1440 for m in w.values()):
                    raise ValueError(f"categories {CATEGORIES}, 1 to 1440 minutes")
                values[k] = w
            elif k == "adaptive_categories":
                cats = tuple(str(c).upper() for c in v)
                if set(cats) - set(CATEGORIES):
                    raise ValueError(f"categories {CATEGORIES}")
                values[k] = cats
            elif k in ("active_min_trades", "extend_minutes", "max_window_minutes", "reject_after_safety_failures"):
                n = int(v)
                if n < (1 if k == "reject_after_safety_failures" else 0):
                    raise ValueError("too small")
                values[k] = n
            else:
                errors.append(f"unknown setting {k}")
        except (ValueError, TypeError, InvalidOperation) as exc:
            errors.append(f"{k}: {exc}")
    cfg = ObservationConfig(**{**asdict(base), **values})
    if cfg.max_window_minutes < max(cfg.window_min.values()):
        errors.append("max_window_minutes must be at least every category window")
    return cfg, errors


async def load_config(session: AsyncSession) -> ObservationConfig:
    row = await session.get(PlatformSetting, SETTINGS_KEY)
    cfg, errors = parse_config(dict(row.value) if row else None)
    return ObservationConfig() if errors else cfg


def label(minutes: int) -> str:
    return "T0" if minutes == 0 else f"T+{minutes}"


def _hist(obs: EvmObservation, state: str, at: datetime, reason: str) -> None:
    h = list(obs.history or [])
    if h and h[-1]["state"] == state and h[-1].get("reason") == reason:
        return
    h.append({"state": state, "at": at.isoformat(), "reason": reason[:200]})
    obs.history = h[-HISTORY_MAX:]


def transition(obs: EvmObservation, state: str, at: datetime, reason: str) -> bool:
    """Moves obs to `state` (recorded in history). Terminal states never change."""
    if obs.state in TERMINAL or state not in STATES:
        return False
    if obs.state != state:
        obs.state, obs.state_at = state, at
    obs.reason = reason[:500]
    _hist(obs, state, at, reason)
    if state in TERMINAL:
        obs.decided_at = at
    return True


async def ensure(session: AsyncSession, chain: str, token: str, category: str, started: datetime, reason: str,
                 cfg: ObservationConfig) -> None:
    """Opens the observation of (chain, token, category) once. Caller commits."""
    if category not in CATEGORIES:
        return
    deadline = started + timedelta(minutes=cfg.window_min[category])
    stmt = insert(EvmObservation).values(
        chain=chain, token=token, category=category, state=OBSERVING, state_at=started, started_at=started,
        deadline=deadline, observation_reason=reason[:200], reason="observation started", snapshots={},
        history=[{"state": DISCOVERED, "at": started.isoformat(), "reason": reason[:200]},
                 {"state": OBSERVING, "at": started.isoformat(), "reason": "observation started"}])
    await session.execute(stmt.on_conflict_do_nothing(index_elements=["chain", "token", "category"]))


async def open_for(session: AsyncSession, chain: str, token: str, category: str) -> EvmObservation | None:
    obs = (await session.execute(select(EvmObservation).where(
        EvmObservation.chain == chain, EvmObservation.token == token, EvmObservation.category == category)))\
        .scalar_one_or_none()
    return obs if obs is not None and obs.state not in TERMINAL else None


def record_entry_decision(obs: EvmObservation, decision: dict[str, Any], opened: bool, now: datetime) -> None:
    """The entry pass's verdict on an open observation."""
    codes = [b["code"] for b in decision.get("blockers", [])]
    obs.last_decision = {"at": now.isoformat(), "decision": decision.get("decision"), "layer": decision.get("layer"),
                         "reason": decision.get("reason"), "blockers": codes[:8]}
    if opened:
        transition(obs, PENDING, now, "paper entry planned")
        transition(obs, ENTERED, now, "paper position opened")
        return
    signal = [c for c in codes if c in SIGNAL_BLOCKERS]
    unsafe = [c for c in codes if c in SAFETY_BLOCKERS]
    if signal:
        transition(obs, ANALYZING, now, "signal not met: " + ", ".join(signal))
    elif unsafe:
        transition(obs, SAFETY_FAILURE if "SAFETY_NOT_PASSED" in unsafe else ANALYZING, now,
                   "held by safety: " + ", ".join(unsafe))
    elif codes:
        if obs.state not in (QUALIFIED, WAITING):
            transition(obs, QUALIFIED, now, "safety passed and the signal is met")
        transition(obs, WAITING, now, "qualified, held by: " + ", ".join(codes))
    else:
        transition(obs, QUALIFIED, now, "safety passed and the signal is met")


# --- snapshots ----------------------------------------------------------------------------------------

def _d(v: Any) -> Decimal | None:
    try:
        return Decimal(str(v)) if v is not None else None
    except InvalidOperation:
        return None


async def snapshot(session: AsyncSession, row: EvmToken, obs: EvmObservation, minutes: int, now: datetime,
                   smart: set[str]) -> dict[str, Any]:
    """§16 fields as of T+minutes, from trades stored up to that moment
    (exact even when the pass runs late); the token's live state (liquidity,
    progress, safety, coordination) is as read at `now` and labelled so."""
    taken_at, now = now, obs.started_at + timedelta(minutes=minutes)
    e = EvmTrade
    holder = func.lower(func.coalesce(e.extra["recipient"].astext, e.trader))
    snaps = obs.snapshots or {}
    prev_label = max((k for k in snaps), key=lambda k: snaps[k].get("minutes", -1), default=None)
    prev = snaps.get(prev_label) if prev_label else None
    since = datetime.fromisoformat(prev["at"]) if prev else obs.started_at

    async def agg(start: datetime) -> Any:
        return (await session.execute(select(
            func.count(), func.coalesce(func.sum(case((e.is_buy, e.quote_amount), else_=0)), 0),
            func.coalesce(func.sum(case((e.is_buy, 0), else_=e.quote_amount)), 0),
            func.count(func.distinct(case((e.is_buy, holder)))), func.count(func.distinct(case((e.is_buy, None), else_=holder))),
        ).where(e.chain == row.chain, e.token == row.token, e.at >= start, e.at <= now))).one()

    cum, part = await agg(obs.started_at), await agg(since)
    last = (await session.execute(select(e.quote_amount, e.token_amount).where(
        e.chain == row.chain, e.token == row.token, e.at <= now, e.token_amount > 0).order_by(e.at.desc()).limit(1))).first()
    price = (Decimal(last[0]) / Decimal(last[1])) if last else None
    coord = row.coordination or {}
    flagged = {w["wallet"] for w in coord.get("wallets", []) if w.get("roles")}
    # holders from launchpad trades: wallets whose curve buys exceed their sells
    net = func.sum(case((e.is_buy, e.token_amount), else_=-e.token_amount))
    holders = (await session.execute(select(func.count()).select_from(
        select(holder.label("h")).where(e.chain == row.chain, e.token == row.token, e.at <= now)
        .group_by(holder).having(net > 0).subquery()))).scalar()
    buyers_part = set((await session.execute(select(holder).where(
        e.chain == row.chain, e.token == row.token, e.is_buy.is_(True), e.at >= since, e.at <= now).distinct()))
        .scalars())
    creator = (row.creator or "").lower()
    creator_trades = (await session.execute(select(func.count()).where(
        e.chain == row.chain, e.token == row.token, e.at >= since, e.at <= now, holder == creator))).scalar() if creator else 0
    organic = (await session.execute(select(
        func.coalesce(func.sum(case((e.is_buy, e.quote_amount), else_=0)), 0),
        func.coalesce(func.sum(case((e.is_buy, 0), else_=e.quote_amount)), 0)).where(
        e.chain == row.chain, e.token == row.token, e.at >= since, e.at <= now,
        *([holder.not_in(flagged)] if flagged else [])))).one()
    top = (await session.execute(select(func.max(func.coalesce(func.sum(e.quote_amount), 0)).over()).where(
        e.chain == row.chain, e.token == row.token, e.is_buy.is_(True), e.at >= since, e.at <= now)
        .group_by(holder).limit(1))).scalar()
    bv, sv = Decimal(part[1]), Decimal(part[2])
    obv, osv = Decimal(organic[0]), Decimal(organic[1])
    st = row.state or {}
    supply = _d((coord.get("facts") or {}).get("supply"))
    liq = _d(st.get("liquidity_quote"))
    prev_liq = _d(prev.get("liquidity")) if prev else None
    prev_holders = prev.get("holders") if prev else None
    e18 = Decimal(10) ** 18
    return {
        "minutes": minutes, "at": now.isoformat(), "taken_at": taken_at.isoformat(),
        "state_read_at": taken_at.isoformat(), "price": str(price) if price is not None else None,
        "market_cap": str(price * supply / e18) if price is not None and supply else None,
        "market_cap_note": None if supply else "total supply not read yet (launch-coordination check)",
        "volume": str((bv + sv) / e18), "buy_volume": str(bv / e18), "sell_volume": str(sv / e18),
        "volume_total": str((Decimal(cum[1]) + Decimal(cum[2])) / e18), "trades": int(part[0]),
        "buyers": int(part[3]), "sellers": int(part[4]),
        "effective_buyers": len(buyers_part - flagged) if coord else None,
        "effective_buyers_note": None if coord else "no launch-coordination assessment yet",
        "holders": int(holders or 0), "holder_growth": (int(holders or 0) - prev_holders) if prev_holders is not None else None,
        "liquidity": str(liq) if liq is not None else None,
        "liquidity_change": str(liq - prev_liq) if liq is not None and prev_liq is not None else None,
        "curve_progress": st.get("progress"), "stage": row.stage,
        "creator_trades": int(creator_trades or 0),
        "top_buyer_share": str((Decimal(top) / bv).quantize(Decimal("0.0001"))) if top and bv > 0 else None,
        "smart_money_buyers": len(buyers_part & smart),
        "net_flow": str(((bv - sv) / (bv + sv)).quantize(Decimal("0.0001"))) if bv + sv > 0 else None,
        "organic_net_flow": str(((obv - osv) / (obv + osv)).quantize(Decimal("0.0001"))) if obv + osv > 0 else None,
        "manipulation": {"coordination": coord.get("status"), "action": coord.get("action")} if coord else None,
        "safety": row.safety_verdict,
        "ml": None, "ml_note": "no EVM model yet: ML is Solana only",
        "decision": (obs.last_decision or {}).get("decision"),
        "basis": "launchpad trades stored by data-evm since the observation started; holders follow trades only",
    }


async def smart_wallets(session: AsyncSession, chain: str) -> set[str]:
    """Wallets the profile rebuild VALIDATED (§25-26): the smart-money set for snapshots."""
    rows = (await session.execute(select(WalletProfile.wallet).where(
        WalletProfile.chain == chain, WalletProfile.metrics["discovery"]["stage"].astext.in_(SMART_STAGES)))).scalars()
    return {w.lower() for w in rows}


async def step(session: AsyncSession, chain: str, now: datetime, cfg: ObservationConfig, limit: int = 300) -> dict[str, int]:
    """Due snapshots, safety outcomes, adaptive deadlines and expiry for the
    open observations of `chain`. Caller commits."""
    counts = {"snapshots": 0, "expired": 0, "rejected": 0, "extended": 0}
    rows = (await session.execute(select(EvmObservation, EvmToken).join(EvmToken, and_(
        EvmToken.chain == EvmObservation.chain, EvmToken.token == EvmObservation.token)).where(
        EvmObservation.chain == chain, or_(
            EvmObservation.state.not_in(TERMINAL),
            # an entered token keeps its snapshots through the window: its path after entry is outcome data
            and_(EvmObservation.state == ENTERED, EvmObservation.deadline >= now - timedelta(hours=2))))
        .order_by(EvmObservation.started_at).limit(limit))).all()
    smart = await smart_wallets(session, chain) if rows else set()
    for obs, row in rows:
        snaps = dict(obs.snapshots or {})
        for m in cfg.snapshots_min:
            lab = label(m)
            if lab not in snaps and now >= obs.started_at + timedelta(minutes=m) and \
                    obs.started_at + timedelta(minutes=m) <= obs.deadline:
                obs.snapshots = dict(snaps)  # snapshot() reads the previous one from here
                snaps[lab] = await snapshot(session, row, obs, m, now, smart)
                counts["snapshots"] += 1
        obs.snapshots = dict(snaps)
        if obs.state == ENTERED:
            continue
        # Safety outcome.
        if row.safety_at is not None and (obs.safety_checked_at is None or row.safety_at > obs.safety_checked_at):
            obs.safety_checked_at = row.safety_at
            if row.safety_verdict == "FAIL":
                obs.safety_failures = (obs.safety_failures or 0) + 1
                codes = [f["code"] for f in (row.safety or {}).get("findings", []) if f.get("level") == "FAIL"]
                transition(obs, SAFETY_FAILURE, now, "safety FAIL: " + ", ".join(codes[:4]))
                if obs.safety_failures >= cfg.reject_after_safety_failures:
                    transition(obs, REJECTED, now, f"safety FAIL on {obs.safety_failures} consecutive checks: "
                                                   + ", ".join(codes[:4]))
                    counts["rejected"] += 1
                    continue
            elif row.safety_verdict in ("PASS", "WARN"):
                obs.safety_failures = 0
                if obs.state == SAFETY_FAILURE:
                    transition(obs, OBSERVING, now, f"safety {row.safety_verdict} again")
        # Deadline.
        if now >= obs.deadline:
            recent = (await session.execute(select(func.count()).where(
                EvmTrade.chain == chain, EvmTrade.token == obs.token, EvmTrade.at >= now - timedelta(minutes=5)))).scalar()
            cap = obs.started_at + timedelta(minutes=cfg.max_window_minutes)
            if obs.category in cfg.adaptive_categories and recent >= cfg.active_min_trades and obs.deadline < cap:
                obs.deadline = min(cap, obs.deadline + timedelta(minutes=cfg.extend_minutes))
                obs.extensions = (obs.extensions or 0) + 1
                _hist(obs, obs.state, now, f"window extended to {obs.deadline.isoformat()}: {recent} trades in 5 min")
                counts["extended"] += 1
                continue
            last = obs.last_decision or {}
            was_qualified = any(h["state"] in (QUALIFIED, WAITING) for h in obs.history or [])
            obs.expiry_reason = "EXPIRED_NO_ENTRY"
            detail = (", ".join(last.get("blockers") or []) or "never evaluated for entry")
            transition(obs, NO_ENTRY, now, ("qualified but not entered: " if was_qualified else "no qualifying entry: ")
                       + detail)
            transition(obs, EXPIRED, now, f"EXPIRED_NO_ENTRY ({detail})")
            counts["expired"] += 1
    return counts
