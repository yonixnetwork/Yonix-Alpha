"""Hyperliquid info API (POST /info), read-only. Request types and response
shapes follow the official hyperliquid-python-sdk (hyperliquid/info.py).
Account views need only a public address (HYPERLIQUID_ACCOUNT_ADDRESS) —
no private key is read or used anywhere."""

import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import httpx

from yonixalpha_core.safety.liquidity import OrderBookModel, book_from_levels
from yonixalpha_core.solana.market_data import RateBudget
from yonixalpha_core.venues.common import (
    DEFAULT_TAKER_FEE_BPS,
    Candle,
    NotConfigured,
    Ticker,
    VenueError,
    budgeted,
    dec,
    ms_to_dt,
    raise_for,
)

MAINNET = "https://api.hyperliquid.xyz"
TESTNET = "https://api.hyperliquid-testnet.xyz"
INTERVAL_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}


class HyperliquidInfo:
    venue = "hyperliquid"

    def __init__(self, client: httpx.AsyncClient, budget: RateBudget, account_address: str | None = None,
                 testnet: bool = False, taker_fee_bps: Decimal = DEFAULT_TAKER_FEE_BPS["hyperliquid"]):
        self.client, self.budget = client, budget
        self.address = account_address or None
        self.base = TESTNET if testnet else MAINNET
        self.taker_fee_bps = taker_fee_bps

    async def _info(self, body: dict[str, Any]) -> Any:
        await budgeted(self.budget, "hyperliquid")
        try:
            resp = await self.client.post(f"{self.base}/info", json=body, timeout=10.0)
        except httpx.HTTPError as exc:
            raise VenueError(f"hyperliquid {body.get('type')}: transport {exc!r}") from exc
        return raise_for(resp, f"hyperliquid {body.get('type')}")

    async def all_mids(self) -> dict[str, Decimal]:
        body = await self._info({"type": "allMids"})
        if not isinstance(body, dict):
            raise VenueError("hyperliquid allMids: unexpected shape")
        return {k: Decimal(v) for k, v in body.items() if dec(v) is not None}

    async def mid(self, coin: str) -> Decimal:
        mids = await self.all_mids()
        if coin not in mids:
            raise VenueError(f"hyperliquid: no mid for {coin}")
        return mids[coin]

    async def book(self, coin: str) -> OrderBookModel:
        body = await self._info({"type": "l2Book", "coin": coin})
        try:
            bids, asks = body["levels"]
            return book_from_levels([(lv["px"], lv["sz"]) for lv in bids], [(lv["px"], lv["sz"]) for lv in asks],
                                    self.taker_fee_bps)
        except (KeyError, TypeError, ValueError) as exc:
            raise VenueError(f"hyperliquid l2Book: {exc}") from exc

    async def ticker(self, coin: str) -> Ticker:
        body = await self._info({"type": "metaAndAssetCtxs"})
        try:
            meta, ctxs = body
            for asset, ctx in zip(meta["universe"], ctxs):
                if asset.get("name") == coin:
                    return Ticker(coin, datetime.now(timezone.utc), dec(ctx.get("midPx")), dec(ctx.get("markPx")),
                                  dec(ctx.get("funding")), dec(ctx.get("openInterest")), volume_24h=dec(ctx.get("dayNtlVlm")))
        except (KeyError, TypeError, ValueError) as exc:
            raise VenueError(f"hyperliquid metaAndAssetCtxs: {exc}") from exc
        raise VenueError(f"hyperliquid: unknown coin {coin}")

    async def candles(self, coin: str, interval: str, count: int = 200, now: datetime | None = None) -> list[Candle]:
        step = INTERVAL_MS.get(interval)
        if step is None:
            raise VenueError(f"unsupported interval {interval}")
        end = int((now.timestamp() if now else time.time()) * 1000)
        rows = await self._info({"type": "candleSnapshot",
                                 "req": {"coin": coin, "interval": interval, "startTime": end - step * count, "endTime": end}})
        out = [Candle(ms_to_dt(r["t"]), Decimal(r["o"]), Decimal(r["h"]), Decimal(r["l"]), Decimal(r["c"]), Decimal(r["v"]),
                      closed=int(r["T"]) < end) for r in (rows or [])]
        if not out:
            raise VenueError("hyperliquid candles: empty")
        return out

    def _require_address(self) -> str:
        if not self.address:
            raise NotConfigured("HYPERLIQUID_ACCOUNT_ADDRESS not set")
        return self.address

    async def account(self) -> dict:
        return await self._info({"type": "clearinghouseState", "user": self._require_address()})

    async def open_orders(self) -> list[dict]:
        return list(await self._info({"type": "openOrders", "user": self._require_address()}) or [])

    async def fills(self) -> list[dict]:
        return list(await self._info({"type": "userFills", "user": self._require_address()}) or [])
