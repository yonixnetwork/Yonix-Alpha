"""BinanceProvider — USDⓈ-M perpetual futures, signed REST.

Endpoints and parameters follow Binance's official connector
(binance/binance-connector-python, derivatives_trading_usds_futures):
- orders: POST/GET /fapi/v1/order (MARKET, newClientOrderId, reduceOnly,
  newOrderRespType=RESULT), fills: GET /fapi/v1/userTrades;
- conditional (stop) orders: since 2025-12-09 Binance only accepts
  STOP_MARKET / TAKE_PROFIT_MARKET through the Algo service —
  POST /fapi/v1/algoOrder (algoType=CONDITIONAL, triggerPrice,
  closePosition=true, clientAlgoId), GET /fapi/v1/openAlgoOrders,
  DELETE /fapi/v1/algoOpenOrders. /fapi/v1/order rejects them (-4120);
- account: /fapi/v2/positionRisk, /fapi/v2/balance, /fapi/v1/leverage,
  /fapi/v1/positionSide/dual (one-way mode is required);
- rules: /fapi/v1/exchangeInfo (LOT_SIZE / MARKET_LOT_SIZE, PRICE_FILTER,
  MIN_NOTIONAL).
Signature: HMAC-SHA256 of the query string with the API secret, key in
the X-MBX-APIKEY header. Credentials are never logged or returned.
"""

import hashlib
import hmac
import time
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

import httpx

from yonixalpha_core.execution.base import (
    CANCELED, EXPIRED, FILLED, OPEN, PARTIAL, REJECTED, UNKNOWN, ExecutionError, Fill, InstrumentRules, NotConfigured,
    OrderState, PositionInfo, close_side, dec,
)
from yonixalpha_core.venues.common import tracked

MAINNET = "https://fapi.binance.com"
TESTNET = "https://testnet.binancefuture.com"
RECV_WINDOW = 5000
ORDER_NOT_FOUND = -2013
STATUS = {"NEW": OPEN, "PARTIALLY_FILLED": PARTIAL, "FILLED": FILLED, "CANCELED": CANCELED, "REJECTED": REJECTED,
          "EXPIRED": EXPIRED, "EXPIRED_IN_MATCH": EXPIRED}


def sign(secret: str, query: str) -> str:
    return hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()


class BinanceProvider:
    venue = "binance"
    quote_currency = "USDT"

    def __init__(self, client: httpx.AsyncClient, api_key: str | None, api_secret: str | None, testnet: bool = False,
                 clock=time.time):
        self.client = client
        self._key = api_key or ""
        self._secret = api_secret or ""
        self.base = TESTNET if testnet else MAINNET
        self._clock = clock
        self._rules: dict[str, InstrumentRules] = {}

    @property
    def configured(self) -> bool:
        return bool(self._key and self._secret)

    async def _call(self, method: str, path: str, params: dict[str, Any] | None = None, signed: bool = True) -> Any:
        if signed and not self.configured:
            raise NotConfigured("BINANCE_API_KEY / BINANCE_API_SECRET not set")
        p = {k: v for k, v in (params or {}).items() if v is not None}
        headers = {}
        if signed:
            p["timestamp"] = int(self._clock() * 1000)
            p["recvWindow"] = RECV_WINDOW
            query = urlencode(p)
            query = f"{query}&signature={sign(self._secret, query)}"
            headers["X-MBX-APIKEY"] = self._key
        else:
            query = urlencode(p)
        url = f"{self.base}{path}" + (f"?{query}" if query else "")

        async def go():
            try:
                resp = await self.client.request(method, url, headers=headers, timeout=10.0)
            except httpx.HTTPError as exc:
                raise ExecutionError(f"binance {path}: transport {type(exc).__name__}") from exc
            try:
                body = resp.json()
            except ValueError as exc:
                raise ExecutionError(f"binance {path}: HTTP {resp.status_code} non-JSON") from exc
            if resp.status_code >= 400 or (isinstance(body, dict) and isinstance(body.get("code"), int) and body["code"] < 0):
                code = body.get("code") if isinstance(body, dict) else None
                raise BinanceError(code, f"binance {path}: HTTP {resp.status_code} {str(body)[:200]}")
            return body
        return await tracked("binance", go())

    # -- rules / setup ---------------------------------------------------------

    async def instrument(self, symbol: str) -> InstrumentRules:
        if symbol in self._rules:
            return self._rules[symbol]
        info = await self._call("GET", "/fapi/v1/exchangeInfo", signed=False)
        for s in info.get("symbols", []):
            if s.get("symbol") != symbol:
                continue
            f = {x["filterType"]: x for x in s.get("filters", [])}
            lot = f.get("MARKET_LOT_SIZE") or f.get("LOT_SIZE") or {}
            rules = InstrumentRules(
                symbol=symbol, qty_step=dec(lot.get("stepSize"), Decimal("0.001")),
                tick_size=dec((f.get("PRICE_FILTER") or {}).get("tickSize"), Decimal("0.01")),
                min_qty=dec(lot.get("minQty"), Decimal(0)),
                min_notional=dec((f.get("MIN_NOTIONAL") or {}).get("notional"), Decimal(0)))
            self._rules[symbol] = rules
            return rules
        raise ExecutionError(f"binance: symbol {symbol} not listed")

    async def prepare(self, symbol: str, leverage: int) -> None:
        mode = await self._call("GET", "/fapi/v1/positionSide/dual")
        if mode.get("dualSidePosition"):
            raise ExecutionError("binance account is in Hedge Mode; YonixAlpha requires One-way Mode")
        await self._call("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": int(leverage)})

    # -- orders ----------------------------------------------------------------

    @staticmethod
    def _state(body: dict, client_id: str) -> OrderState:
        filled = dec(body.get("executedQty"), Decimal(0))
        avg = dec(body.get("avgPrice"))
        return OrderState(client_id=client_id, status=STATUS.get(body.get("status"), UNKNOWN), filled_qty=filled,
                          avg_price=avg if avg and avg > 0 else None, exchange_id=str(body.get("orderId") or "") or None,
                          raw={k: body.get(k) for k in ("status", "executedQty", "avgPrice", "orderId", "updateTime")})

    async def market_order(self, symbol: str, side: str, qty: Decimal, reduce_only: bool, client_id: str,
                           ref_price: Decimal | None = None) -> OrderState:
        body = await self._call("POST", "/fapi/v1/order", {
            "symbol": symbol, "side": side, "type": "MARKET", "quantity": format(qty, "f"),
            "reduceOnly": "true" if reduce_only else None, "newClientOrderId": client_id, "newOrderRespType": "RESULT"})
        state = self._state(body, client_id)
        if state.status == FILLED:
            state.fee = await self._fees(symbol, state.exchange_id)
        return state

    async def order_status(self, symbol: str, client_id: str) -> OrderState:
        try:
            body = await self._call("GET", "/fapi/v1/order", {"symbol": symbol, "origClientOrderId": client_id})
        except BinanceError as exc:
            if exc.code == ORDER_NOT_FOUND:
                return OrderState(client_id=client_id, status=REJECTED, error="order does not exist (never reached the book)")
            raise
        state = self._state(body, client_id)
        if state.filled_qty > 0:
            state.fee = await self._fees(symbol, state.exchange_id)
        return state

    async def _fees(self, symbol: str, order_id: str | None) -> Decimal:
        if not order_id:
            return Decimal(0)
        trades = await self._call("GET", "/fapi/v1/userTrades", {"symbol": symbol, "orderId": order_id})
        return sum((dec(t.get("commission"), Decimal(0)) for t in trades if t.get("commissionAsset") == "USDT"), Decimal(0))

    # -- protection (Algo service) --------------------------------------------

    async def set_stop(self, symbol: str, position_side: str, stop_price: Decimal, qty: Decimal, client_id: str) -> str:
        rules = await self.instrument(symbol)
        body = await self._call("POST", "/fapi/v1/algoOrder", {
            "algoType": "CONDITIONAL", "symbol": symbol, "side": close_side(position_side), "type": "STOP_MARKET",
            "triggerPrice": format(rules.round_price(stop_price), "f"), "closePosition": "true",
            "workingType": "MARK_PRICE", "priceProtect": "true", "clientAlgoId": client_id[:36]})
        return str(body.get("algoId") or body.get("clientAlgoId") or client_id)

    async def cancel_stop(self, symbol: str, stop_id: str) -> None:
        if stop_id.isdigit():
            await self._call("DELETE", "/fapi/v1/algoOrder", {"symbol": symbol, "algoId": stop_id})
        else:
            await self._call("DELETE", "/fapi/v1/algoOrder", {"symbol": symbol, "clientAlgoId": stop_id})

    async def cancel_protection(self, symbol: str) -> None:
        await self._call("DELETE", "/fapi/v1/algoOpenOrders", {"symbol": symbol})

    async def open_protection(self, symbol: str) -> list[dict[str, Any]]:
        rows = await self._call("GET", "/fapi/v1/openAlgoOrders", {"symbol": symbol})
        rows = rows.get("orders", rows) if isinstance(rows, dict) else rows
        return [{"id": str(r.get("algoId")), "type": r.get("orderType") or r.get("type"),
                 "trigger_price": r.get("triggerPrice"), "side": r.get("side")} for r in rows or []]

    # -- account ---------------------------------------------------------------

    async def position(self, symbol: str) -> PositionInfo | None:
        rows = await self._call("GET", "/fapi/v2/positionRisk", {"symbol": symbol})
        for r in rows:
            if r.get("symbol") == symbol and dec(r.get("positionAmt"), Decimal(0)) != 0:
                return PositionInfo(symbol, dec(r["positionAmt"]), dec(r.get("entryPrice")), dec(r.get("markPrice")),
                                    dec(r.get("unRealizedProfit")))
        return None

    async def balance(self) -> Decimal:
        rows = await self._call("GET", "/fapi/v2/balance")
        for r in rows:
            if r.get("asset") == "USDT":
                return dec(r.get("availableBalance"), Decimal(0))
        return Decimal(0)

    async def fills_since(self, symbol: str, start_ms: int) -> list[Fill]:
        rows = await self._call("GET", "/fapi/v1/userTrades", {"symbol": symbol, "startTime": start_ms, "limit": 1000})
        return [Fill(str(r.get("orderId")), None, r.get("side"), dec(r.get("qty"), Decimal(0)), dec(r.get("price"), Decimal(0)),
                     dec(r.get("commission"), Decimal(0)) if r.get("commissionAsset") == "USDT" else Decimal(0),
                     dec(r.get("realizedPnl")), int(r.get("time", 0))) for r in rows]


class BinanceError(ExecutionError):
    def __init__(self, code: int | None, message: str):
        super().__init__(message)
        self.code = code
