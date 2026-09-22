from datetime import datetime, timezone

from redis.asyncio import Redis

# A single, shared Redis key rather than a per-service flag: the kill
# switch's whole point (spec section 37) is that it stops EVERYTHING —
# every engine and the decision system all check the same key, so
# engaging it from any one place (dashboard button, a future emergency
# CLI, a risk-engine self-trip) takes effect everywhere immediately with
# no coordination needed between processes.
_KILL_SWITCH_KEY = "yonixalpha:kill_switch:engaged"
_KILL_SWITCH_REASON_KEY = "yonixalpha:kill_switch:reason"


async def engage(redis: Redis, reason: str) -> None:
    await redis.set(_KILL_SWITCH_KEY, "1")
    await redis.set(_KILL_SWITCH_REASON_KEY, f"{datetime.now(timezone.utc).isoformat()} {reason}")


async def disengage(redis: Redis) -> None:
    await redis.delete(_KILL_SWITCH_KEY, _KILL_SWITCH_REASON_KEY)


async def is_engaged(redis: Redis) -> bool:
    return bool(await redis.get(_KILL_SWITCH_KEY))


async def get_reason(redis: Redis) -> str | None:
    return await redis.get(_KILL_SWITCH_REASON_KEY)
