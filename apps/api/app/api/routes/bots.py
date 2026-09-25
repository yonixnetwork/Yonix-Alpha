"""The user's standalone bots, through their control APIs
(yonixalpha_core.external_bots). Every route requires a dashboard login;
bot URLs and tokens stay on the server and are never returned. Closing a
bot's position is audited and needs the bot's name repeated in the body."""

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_username, get_db, get_redis, get_settings
from app.api.util import audit
from yonixalpha_core import external_bots
from yonixalpha_core.config import Settings

router = APIRouter(prefix="/external-bots", tags=["external-bots"])


class CloseIn(BaseModel):
    confirm: str


def _bot(name: str, settings: Settings) -> external_bots.BotSpec:
    bot = external_bots.BOTS.get(name)
    if bot is None:
        raise HTTPException(404, f"unknown bot; one of {sorted(external_bots.BOTS)}")
    if not external_bots.configured(settings, bot):
        raise HTTPException(409, f"NOT CONFIGURED: set {bot.url_setting} and {bot.token_setting} in .env")
    return bot


@router.get("")
async def list_bots(request: Request, redis: Redis = Depends(get_redis), settings: Settings = Depends(get_settings),
                    _: str = Depends(get_current_username)) -> list[dict]:
    """Live status of every configured bot (read now; also cached for the
    decision engine's LIVE conflict check)."""
    status = await external_bots.poll_all(request.app.state.http, settings, redis)
    return [{"name": name, "label": bot.label, "strategies": list(bot.strategies), **status[name]}
            for name, bot in external_bots.BOTS.items()]


@router.get("/{name}/config")
async def bot_config(name: str, request: Request, settings: Settings = Depends(get_settings),
                     _: str = Depends(get_current_username)) -> dict:
    bot = _bot(name, settings)
    try:
        return await external_bots.ExternalBotClient(request.app.state.http, settings, bot).get_config()
    except external_bots.BotError as exc:
        raise HTTPException(502, str(exc)) from exc


@router.post("/{name}/close")
async def bot_close(name: str, body: CloseIn, request: Request, db: AsyncSession = Depends(get_db),
                    settings: Settings = Depends(get_settings), username: str = Depends(get_current_username)) -> dict:
    bot = _bot(name, settings)
    if body.confirm != name:
        raise HTTPException(422, "type the bot's name in `confirm` to close its position")
    try:
        result = await external_bots.ExternalBotClient(request.app.state.http, settings, bot).close()
    except external_bots.BotError as exc:
        await audit(db, username, request, "external_bot.close_failed", {"bot": name, "error": str(exc)[:200]})
        await db.commit()
        raise HTTPException(502, str(exc)) from exc
    await audit(db, username, request, "external_bot.close", {"bot": name, "result": result})
    await db.commit()
    return result
