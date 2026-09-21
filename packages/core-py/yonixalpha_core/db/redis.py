from redis.asyncio import Redis, from_url

from yonixalpha_core.config import Settings


def make_redis(settings: Settings) -> Redis:
    return from_url(settings.redis_url, decode_responses=True)
