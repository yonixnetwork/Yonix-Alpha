"""Configuration health: the database configuration revision against the
revision each running service acknowledged, plus each module's runtime
status (READY / BLOCKED with the reason), so the dashboard can show whether
what it displays is what the backend is running."""

from fastapi import APIRouter, Depends
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_username, get_db, get_redis, get_settings
from app.api.util import jsonable
from yonixalpha_core import config_validation, runtime_config
from yonixalpha_core.config import Settings

router = APIRouter(prefix="/config", tags=["config"])


def _module_runtime(validation: dict, acks: dict) -> list[dict]:
    """ON/OFF (the stored mode) and what the runtime can actually do with it."""
    rpc = _rpc_state(acks)
    out = []
    for name, r in validation.items():
        mode = r.get("mode")
        if r["status"] == config_validation.DISABLED or mode in (None, "OFF"):
            runtime, reason = "OFF", None
        elif r["status"] == config_validation.CONFIG_ERROR:
            runtime, reason = "BLOCKED", "; ".join(r["errors"]) or "configuration error"
        elif name.startswith("solana") and rpc is not None and not rpc["healthy"]:
            runtime, reason = "BLOCKED", f"no healthy RPC provider ({rpc['detail']})"
        else:
            runtime, reason = "RUNNING", None
        out.append({"module": name, "label": r.get("label"), "mode": mode, "runtime": runtime, "reason": reason,
                    "live_missing": r.get("live_missing") or []})
    return out


def _rpc_state(acks: dict) -> dict | None:
    """From the decision engine's acknowledged RPC health (the service that
    evaluates tokens): healthy when at least one endpoint is usable."""
    ack = acks.get("decision-engine") or {}
    rpc = (ack.get("status") or {}).get("rpc")
    endpoints = rpc.get("endpoints") if isinstance(rpc, dict) else None
    if not endpoints:
        return None
    usable = [e for e in endpoints if not e.get("disabled") and not e.get("rate_limited")]
    return {"healthy": bool(usable), "detail": f"{len(usable)}/{len(endpoints)} endpoints usable"}


@router.get("/health")
async def config_health(db: AsyncSession = Depends(get_db), redis: Redis = Depends(get_redis),
                        settings: Settings = Depends(get_settings), _: str = Depends(get_current_username)) -> dict:
    db_rev = await runtime_config.current(db)
    acks = await runtime_config.read_acks(redis)
    sync = runtime_config.sync_status(int(db_rev.get("revision", 0)), acks)
    validation = await config_validation.load_and_validate(db, settings)
    return jsonable({
        "database": {"revision": int(db_rev.get("revision", 0)), "changed_at": db_rev.get("changed_at"),
                     "change": db_rev.get("change"), "actor": db_rev.get("actor")},
        "status": sync["status"],
        "services": sync["services"],
        "effective_in_database": await runtime_config.effective_snapshot(db),
        "modules": _module_runtime(validation, acks),
        "restart_required_for": [
            "secrets and infrastructure in .env (wallet private key, exchange API keys/secrets, database/Redis, "
            "JWT secret, admin password, the TRADING_ENABLED / LIVE_TRADING_ENABLED / PAPER_TRADING locks): changed on "
            "the server; the dashboard key updater applies provider keys and restarts only the affected services",
        ],
    })
