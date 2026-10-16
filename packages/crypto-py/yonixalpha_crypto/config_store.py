"""Engine switch and per-strategy settings, persisted in the crypto schema.

- Defaults: engine OFF, every strategy DISABLED. A restart or deploy never
  turns anything on (ensure_defaults only inserts missing rows).
- A strategy save writes only that strategy's row, and only when the caller
  sends the version it read (optimistic concurrency): one strategy's save
  cannot overwrite another's, and two tabs cannot silently overwrite each
  other.
- Unknown keys, unknown exchanges, bad symbols, out-of-range numbers and any
  mode other than DISABLED / DEMO (paper) / EXCHANGE_DEMO (a practice
  connection) are rejected, never coerced. There is no LIVE mode.
- A strategy can be enabled only when its registry status is IMPLEMENTED
  and the chosen exchange is one it supports.
- Every change is written to crypto.config_audit (before / after)."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_crypto import registry
import uuid

from yonixalpha_crypto.db import ApiConnection, ConfigAudit, EngineState, StrategyConfig
from yonixalpha_crypto.exchanges.base import split_symbol
from yonixalpha_crypto.guard import ACTIVE_MODES, MODES
from yonixalpha_crypto.private import management
from yonixalpha_crypto.risk import RISK_RULES, validate_risk

ALLOWED_KEYS = {"enabled", "mode", "exchange", "symbols", "interval", "params", "risk", "exits", "connection_id"}
PRACTICE_ENVIRONMENTS = ("DEMO", "TESTNET")


class ConfigError(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


class ConfigConflict(RuntimeError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def config_dict(c: StrategyConfig) -> dict[str, Any]:
    return {"strategy_id": c.strategy_id, "enabled": c.enabled, "mode": c.mode, "exchange": c.exchange,
            "symbols": list(c.symbols or []), "interval": c.interval, "params": dict(c.params or {}),
            "risk": dict(c.risk or {}), "exits": dict(c.exits or {}),
            "connection_id": str(c.connection_id) if c.connection_id else None, "version": c.version,
            "updated_at": c.updated_at.isoformat() if c.updated_at else None, "updated_by": c.updated_by}


def engine_dict(e: EngineState) -> dict[str, Any]:
    return {"enabled": e.enabled, "emergency_stop": e.emergency_stop, "note": e.note,
            "updated_at": e.updated_at.isoformat() if e.updated_at else None, "updated_by": e.updated_by}


async def ensure_defaults(session: AsyncSession) -> None:
    if await session.get(EngineState, 1) is None:
        session.add(EngineState(id=1, enabled=False, emergency_stop=False))
    have = set((await session.execute(select(StrategyConfig.strategy_id))).scalars())
    for spec in registry.all_specs():
        if spec.id not in have:
            session.add(StrategyConfig(strategy_id=spec.id, enabled=False, mode="DISABLED",
                                       exchange=spec.default_exchange, symbols=list(spec.default_symbols),
                                       interval=spec.default_interval, params=spec.default_params(),
                                       risk={k: str(v[0]) for k, v in RISK_RULES.items()},
                                       exits=management.validate_plan(spec.default_exits), version=1))
    await session.flush()


def _validate_params(spec: registry.StrategySpec, params: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    out, errors = {}, []
    for k, v in params.items():
        rule = spec.params.get(k)
        if rule is None:
            errors.append(f"params.{k}: unknown for {spec.id}")
            continue
        _default, kind, lo, hi = rule
        try:
            if kind == "int":
                x = int(v)
                if str(v).strip() != str(x) or not lo <= x <= hi:
                    raise ValueError
                out[k] = x
            elif kind == "dec":
                x = Decimal(str(v))
                if not x.is_finite() or not Decimal(str(lo)) <= x <= Decimal(str(hi)):
                    raise ValueError
                out[k] = str(x)
            elif kind == "bool":
                if not isinstance(v, bool):
                    raise ValueError
                out[k] = v
            elif kind.startswith("choice:"):
                if v not in kind[7:].split("|"):
                    raise ValueError
                out[k] = v
        except (ValueError, InvalidOperation, TypeError):
            errors.append(f"params.{k}: invalid value {v!r} ({kind} {lo}..{hi})")
    return out, errors


def validate(spec: registry.StrategySpec, current: dict[str, Any], change: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    unknown = set(change) - ALLOWED_KEYS
    if unknown:
        errors.append(f"unknown keys: {sorted(unknown)}")
    new = {**current, **{k: v for k, v in change.items() if k in ALLOWED_KEYS}}
    if new["mode"] not in MODES:
        errors.append(f"mode must be one of {list(MODES)} (there is no live mode: live orders are locked in code)")
    if not isinstance(new["enabled"], bool):
        errors.append("enabled must be true or false")
    if new["enabled"]:
        if spec.status != registry.IMPLEMENTED:
            errors.append(f"{spec.id} cannot be enabled: {spec.status} ({spec.status_reason})")
        if new["mode"] not in ACTIVE_MODES:
            errors.append(f"an enabled strategy must be in {' or '.join(ACTIVE_MODES)} mode")
    if new["mode"] == "EXCHANGE_DEMO":
        if not new.get("connection_id"):
            errors.append("EXCHANGE_DEMO needs a practice (DEMO / TESTNET) connection")
        else:
            try:
                new["connection_id"] = str(uuid.UUID(str(new["connection_id"])))
            except ValueError:
                errors.append("connection_id is not a valid id")
    elif "connection_id" in change and change["connection_id"] not in (None, ""):
        errors.append("connection_id is only used in EXCHANGE_DEMO mode")
    if "exits" in change:
        try:
            new["exits"] = management.validate_plan(change["exits"] if isinstance(change["exits"], dict) else None)
            if not isinstance(change["exits"], dict):
                raise management.PlanError("exits must be an object")
        except (management.PlanError, ValueError, TypeError) as e:
            errors.append(f"exits: {e}")
    if spec.exchanges and new["exchange"] not in spec.exchanges:
        errors.append(f"exchange must be one of {list(spec.exchanges)}")
    syms = new.get("symbols")
    if not isinstance(syms, list) or len(syms) > spec.max_symbols:
        errors.append(f"symbols must be a list of at most {spec.max_symbols}")
    else:
        for s in syms:
            try:
                split_symbol(str(s))
            except ValueError as e:
                errors.append(str(e))
        new["symbols"] = [str(s).upper() for s in syms]
    if spec.intervals and new["interval"] not in spec.intervals:
        errors.append(f"interval must be one of {list(spec.intervals)}")
    if "params" in change:
        if not isinstance(change["params"], dict):
            errors.append("params must be an object")
        else:
            p, perr = _validate_params(spec, change["params"])
            errors += perr
            new["params"] = {**current.get("params", {}), **p}
    if "risk" in change:
        if not isinstance(change["risk"], dict):
            errors.append("risk must be an object")
        else:
            r, rerr = validate_risk(change["risk"])
            errors += rerr
            new["risk"] = {**current.get("risk", {}), **r}
    if not errors and spec.check is not None:
        errors += spec.check(new)
    if errors:
        raise ConfigError(errors)
    return new


async def save_strategy(session: AsyncSession, strategy_id: str, change: dict[str, Any], expected_version: int,
                        actor: str | None) -> dict[str, Any]:
    spec = registry.get(strategy_id)
    if spec is None:
        raise ConfigError([f"unknown strategy {strategy_id!r}"])
    row = (await session.execute(select(StrategyConfig).where(StrategyConfig.strategy_id == strategy_id)
                                 .with_for_update())).scalar_one_or_none()
    if row is None:
        raise ConfigError([f"{strategy_id} has no stored settings yet"])
    if row.version != expected_version:
        raise ConfigConflict(f"{strategy_id} was changed elsewhere (version {row.version}, you sent {expected_version}); "
                             "reload and try again")
    before = config_dict(row)
    new = validate(spec, before, change)
    if new["mode"] == "EXCHANGE_DEMO":
        conn = await session.get(ApiConnection, uuid.UUID(new["connection_id"]))
        problems = []
        if conn is None:
            problems.append("the connection does not exist")
        else:
            if conn.environment not in PRACTICE_ENVIRONMENTS:
                problems.append(f"the connection is {conn.environment}: EXCHANGE_DEMO uses DEMO / TESTNET only")
            if conn.exchange != new["exchange"]:
                problems.append(f"the connection is for {conn.exchange}, the strategy trades on {new['exchange']}")
            if new["enabled"] and not conn.enabled:
                problems.append("the connection is disabled")
        if problems:
            raise ConfigError(problems)
    for k in ("enabled", "mode", "exchange", "symbols", "interval", "params", "risk", "exits"):
        setattr(row, k, new[k])
    row.connection_id = uuid.UUID(new["connection_id"]) if new.get("mode") == "EXCHANGE_DEMO" else None
    row.version += 1
    row.updated_at, row.updated_by = _now(), actor
    await session.flush()
    after = config_dict(row)
    session.add(ConfigAudit(actor=actor, scope=strategy_id, before=before, after=after))
    return after


async def set_engine(session: AsyncSession, *, enabled: bool | None, emergency_stop: bool | None, note: str | None,
                     actor: str | None) -> dict[str, Any]:
    row = (await session.execute(select(EngineState).where(EngineState.id == 1).with_for_update())).scalar_one_or_none()
    if row is None:
        row = EngineState(id=1, enabled=False, emergency_stop=False)
        session.add(row)
        await session.flush()
    before = engine_dict(row)
    if enabled is not None:
        row.enabled = enabled
    if emergency_stop is not None:
        row.emergency_stop = emergency_stop
    row.note, row.updated_at, row.updated_by = note, _now(), actor
    await session.flush()
    after = engine_dict(row)
    session.add(ConfigAudit(actor=actor, scope="engine", before=before, after=after))
    return after
