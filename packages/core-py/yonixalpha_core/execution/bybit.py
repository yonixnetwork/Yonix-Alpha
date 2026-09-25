"""BybitProvider — V5 linear (USDT) perpetuals, signed REST.

Paths from the official pybit SDK (pybit/trade.py, position.py,
account.py): /v5/order/create, /v5/order/realtime, /v5/order/history,
/v5/position/list, /v5/position/set-leverage, /v5/position/trading-stop,
/v5/execution/list, /v5/account/wallet-balance,
/v5/market/instruments-info.
Signature (pybit _http_manager): HMAC-SHA256(secret, timestamp + api_key
+ recv_window + payload) where payload is the query string for GET and
the JSON body for POST; headers X-BAPI-API-KEY / -TIMESTAMP /
-RECV-WINDOW / -SIGN. retCode 0 is success.

One-way mode (positionIdx 0) is required. The exchange-side stop is the
position's own stopLoss (tpslMode Full), so it always covers the whole
position; clearing it is stopLoss "0".
"""

import hashlib
import hmac
import json
import time
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

import httpx

from yonixalpha_core.execution.base import (
    CANCELED, FILLED, OPEN, PARTIAL, REJECTED, UNKNOWN, ExecutionError, Fill, InstrumentRules, NotConfigured,
    OrderState, PositionInfo, dec,
)
from yonixalpha_core.venues.common import tracked

MAINNET = "https://api.bybit.com"
TESTNET = "https://api-testnet.bybit.com"
RECV_WINDOW = 5000
LEVERAGE_NOT_MODIFIED = 110043
STATUS = {"New": OPEN, "PartiallyFilled": PARTIAL, "Filled": FILLED, "Cancelled": CANCELED, "Rejected": REJECTED,
          "Deactivated": CANCELED, "PartiallyFilledCanceled": CANCELED, "Untriggered": OPEN, "Triggered": OPEN}


def sign(secret: str, timestamp: str, api_key: str, recv_window: int, payload: str) -> str:
    return hmac.new(secret.encode(), f"{timestamp}{api_key}{recv_window}{payload}".encode(), hashlib.sha256).hexdigest()


class BybitError(ExecutionError):
    def __init__(self, code: int | None, message: str):
        super().__init__(message)
        self.code = code


class BybitProvider:
    venue = "bybit"
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
            raise NotConfigured("BYBIT_API_KEY / BYBIT_API_SECRET not set")
        p = {k: v for k, v in (params or {}).items() if v is not None}
        headers: dict[str, str] = {}
        if method == "GET":
            payload = urlencode(p)
            url, body = f"{self.base}{path}" + (f"?{payload}" if payload else ""), None
        else:
            payload = json.dumps(p, separators=(",", ":"))
            url, body = f"{self.base}{path}", payload
            headers["Content-Type"] = "application/json"
        if signed:
            ts = str(int(self._clock() * 1000))
            headers.update({"X-BAPI-API-KEY": self._key, "X-BAPI-TIMESTAMP": ts, "X-BAPI-RECV-WINDOW": str(RECV_WINDOW),
                            "X-BAPI-SIGN": sign(self._secret, ts, self._key, RECV_WINDOW, payload)})

        async def go():
            try:
                resp = await self.client.request(method, url, content=body, headers=headers, timeout=10.0)
            except httpx.HTTPError as exc:
                raise ExecutionError(f"bybit {path}: transport {type(exc).__name__}") from exc
            try:
                data = resp.json()
            except ValueError as exc:
                raise ExecutionError(f"bybit {path}: HTTP {resp.status_code} non-JSON") from exc
            if resp.status_code >= 400 or data.get("retCode") != 0:
                raise BybitError(data.get("retCode"), f"bybit {path}: {data.get('retCode')} {data.get('retMsg')}")
            return data.get("result") or {}
        return await tracked("bybit", go())

    async def instrument(self, symbol: str) -> InstrumentRules:
        if symbol in self._rules:
            return self._rules[symbol]
        res = await self._call("GET", "/v5/market/instruments-info", {"category": "linear", "symbol": symbol}, signed=False)
        rows = res.get("list") or []
        if not rows:
            raise ExecutionError(f"bybit: symbol {symbol} not listed")
        lot, price = rows[0].get("lotSizeFilter") or {}, rows[0].get("priceFilter") or {}
        rules = InstrumentRules(symbol, dec(lot.get("qtyStep"), Decimal("0.001")), dec(price.get("tickSize"), Decimal("0.01")),
                                dec(lot.get("minOrderQty"), Decimal(0)), dec(lot.get("minNotionalValue"), Decimal(0)))
        self._rules[symbol] = rules
        return rules

    async def prepare(self, symbol: str, leverage: int) -> None:
        pos = await self._call("GET", "/v5/position/list", {"category": "linear", "symbol": symbol})
        if any(int(r.get("positionIdx", 0)) != 0 for r in pos.get("list") or []):
            raise ExecutionError("bybit position is in Hedge Mode; YonixAlpha requires One-way Mode (positionIdx 0)")
        try:
            await self._call("POST", "/v5/position/set-leverage", {"category": "linear", "symbol": symbol,
                                                                   "buyLeverage": str(leverage), "sellLeverage": str(leverage)})
        except BybitError as exc:
            if exc.code != LEVERAGE_NOT_MODIFIED:
                raise

    @staticmethod
    def _state(row: dict, client_id: str) -> OrderState:
        avg = dec(row.get("avgPrice"))
        return OrderState(client_id=client_id, status=STATUS.get(row.get("orderStatus"), UNKNOWN),
                          filled_qty=dec(row.get("cumExecQty"), Decimal(0)), avg_price=avg if avg and avg > 0 else None,
                          fee=dec(row.get("cumExecFee"), Decimal(0)), exchange_id=row.get("orderId"),
                          raw={k: row.get(k) for k in ("orderStatus", "cumExecQty", "avgPrice", "cumExecFee", "rejectReason")})

    async def market_order(self, symbol: str, side: str, qty: Decimal, reduce_only: bool, client_id: str,
                           ref_price: Decimal | None = None) -> OrderState:
        res = await self._call("POST", "/v5/order/create", {
            "category": "linear", "symbol": symbol, "side": "Buy" if side == "BUY" else "Sell", "orderType": "Market",
            "qty": format(qty, "f"), "reduceOnly": reduce_only, "orderLinkId": client_id, "positionIdx": 0})
        # Create only acknowledges; the fill is read back like any status query.
        state = await self.order_status(symbol, client_id)
        if state.exchange_id is None:
            state.exchange_id = res.get("orderId")
        return state

    async def order_status(self, symbol: str, client_id: str) -> OrderState:
        for path in ("/v5/order/realtime", "/v5/order/history"):
            res = await self._call("GET", path, {"category": "linear", "symbol": symbol, "orderLinkId": client_id})
            rows = res.get("list") or []
            if rows:
                return self._state(rows[0], client_id)
        return OrderState(client_id=client_id, status=UNKNOWN, error="order not found yet")

    async def set_stop(self, symbol: str, position_side: str, stop_price: Decimal, qty: Decimal, client_id: str) -> str:
        rules = await self.instrument(symbol)
        await self._call("POST", "/v5/position/trading-stop", {
            "category": "linear", "symbol": symbol, "tpslMode": "Full", "positionIdx": 0,
            "stopLoss": format(rules.round_price(stop_price), "f"), "slTriggerBy": "MarkPrice"})
        return f"position-stop:{symbol}"

    async def cancel_stop(self, symbol: str, stop_id: str) -> None:
        return None  # the position has one stopLoss; set_stop already replaced it

    async def cancel_protection(self, symbol: str) -> None:
        await self._call("POST", "/v5/position/trading-stop", {"category": "linear", "symbol": symbol, "tpslMode": "Full",
                                                               "positionIdx": 0, "stopLoss": "0"})

    async def open_protection(self, symbol: str) -> list[dict[str, Any]]:
        res = await self._call("GET", "/v5/position/list", {"category": "linear", "symbol": symbol})
        out = []
        for r in res.get("list") or []:
            if dec(r.get("stopLoss"), Decimal(0)) > 0:
                out.append({"id": f"position-stop:{symbol}", "type": "STOP_MARKET", "trigger_price": r.get("stopLoss"),
                            "side": r.get("side")})
        return out

    async def position(self, symbol: str) -> PositionInfo | None:
        res = await self._call("GET", "/v5/position/list", {"category": "linear", "symbol": symbol})
        for r in res.get("list") or []:
            size = dec(r.get("size"), Decimal(0))
            if size > 0 and r.get("side") in ("Buy", "Sell"):
                signed = size if r["side"] == "Buy" else -size
                return PositionInfo(symbol, signed, dec(r.get("avgPrice")), dec(r.get("markPrice")), dec(r.get("unrealisedPnl")))
        return None

    async def balance(self) -> Decimal:
        res = await self._call("GET", "/v5/account/wallet-balance", {"accountType": "UNIFIED", "coin": "USDT"})
        for acct in res.get("list") or []:
            total = dec(acct.get("totalAvailableBalance"))
            if total is not None:
                return total
            for c in acct.get("coin") or []:
                if c.get("coin") == "USDT":
                    return dec(c.get("walletBalance"), Decimal(0))
        return Decimal(0)

    async def fills_since(self, symbol: str, start_ms: int) -> list[Fill]:
        res = await self._call("GET", "/v5/execution/list", {"category": "linear", "symbol": symbol, "startTime": start_ms,
                                                             "limit": 100})
        return [Fill(r.get("orderId"), r.get("orderLinkId") or None, "BUY" if r.get("side") == "Buy" else "SELL",
                     dec(r.get("execQty"), Decimal(0)), dec(r.get("execPrice"), Decimal(0)), dec(r.get("execFee"), Decimal(0)),
                     None, int(r.get("execTime", 0))) for r in res.get("list") or []]
