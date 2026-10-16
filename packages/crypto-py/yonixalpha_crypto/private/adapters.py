"""Spot account adapters, one per exchange. They share a small interface so
the executor is written once, but each follows its own exchange's
documented order types and responses; nothing is assumed to be identical.

Binance spot (developers.binance.com, spot REST):
- entry / exit: POST /api/v3/order type MARKET, `newClientOrderId`,
  newOrderRespType FULL (fills with commission and commissionAsset);
- protection: POST /api/v3/orderList/oco, SELL, above LIMIT_MAKER (take
  profit) and below STOP_LOSS_LIMIT (stop); without a target, a single
  STOP_LOSS_LIMIT order. Both are held by the exchange and lock the coins.
  An OCO cannot be amended: moving the stop is cancel + place (a short gap
  with no stop; the executor records it and verifies the new orders).
- lookup: GET /api/v3/order origClientOrderId (code -2013 = not found);
  cancel: DELETE /api/v3/order or /api/v3/orderList (code -2011 = unknown).

Bybit V5 spot (bybit-exchange.github.io/docs/v5):
- entry / exit: POST /v5/order/create category spot, orderType Market,
  marketUnit baseCoin (otherwise a market BUY quantity is read in USDT),
  `orderLinkId`; the answer has only ids, so the order is then looked up;
- protection: a spot TP/SL order (orderFilter tpslOrder, triggerPrice =
  stop, Limit price = stop-limit), which reserves the coins. A take-profit
  as a second exchange order would need the same coins, so on Bybit spot
  the TARGET IS ENGINE-MANAGED (market sell when the bid reaches it) and
  only the stop is exchange-side. The stop can be amended in place
  (POST /v5/order/amend), so moving it leaves no gap.
- lookup: GET /v5/order/realtime then /v5/order/history by orderLinkId.

Fees: Binance reports each fill's commission asset. Bybit spot charges a
BUY's fee in the coin received (assumed here, and checked against the
actual balance by the reconciler, which never trusts the arithmetic alone).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

from yonixalpha_crypto.exchanges.base import InstrumentSpec, TopOfBook, dec, split_symbol
from yonixalpha_crypto.exchanges.binance import instrument_from as binance_instrument
from yonixalpha_crypto.exchanges.bybit import instrument_from as bybit_instrument
from yonixalpha_crypto.private.signedhttp import ExchangeRequestError, SignedHttp

OPEN, PARTIAL, FILLED, CANCELED, REJECTED, EXPIRED = "OPEN", "PARTIAL", "FILLED", "CANCELED", "REJECTED", "EXPIRED"
TERMINAL = frozenset({FILLED, CANCELED, REJECTED, EXPIRED})


@dataclass
class OrderState:
    client_id: str
    status: str  # OPEN | PARTIAL | FILLED | CANCELED | REJECTED | EXPIRED
    filled_qty: Decimal = Decimal(0)
    avg_price: Decimal | None = None
    fee: Decimal = Decimal(0)
    fee_asset: str | None = None
    exchange_id: str | None = None
    raw_status: str = ""

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL


@dataclass(frozen=True)
class ProtectionIds:
    stop: str
    take_profit: str | None = None
    list_id: str | None = None  # Binance OCO list client id


@dataclass
class Balance:
    free: Decimal = Decimal(0)
    locked: Decimal = Decimal(0)

    @property
    def total(self) -> Decimal:
        return self.free + self.locked


@dataclass
class ProtectionResult:
    orders: list[OrderState] = field(default_factory=list)
    exchange_take_profit: bool = False


class SpotAccount(Protocol):
    exchange: str
    environment: str
    exchange_take_profit: bool  # can the exchange hold the target as well as the stop
    stop_amendable: bool

    async def instrument(self, symbol: str) -> InstrumentSpec: ...
    async def book(self, symbol: str) -> TopOfBook: ...
    async def balances(self) -> dict[str, Balance]: ...
    async def bars(self, symbol: str, interval: str, limit: int) -> list[tuple[int, Decimal, Decimal, Decimal]]: ...
    async def market(self, symbol: str, side: str, qty: Decimal, client_id: str) -> OrderState | None: ...
    async def get_order(self, symbol: str, client_id: str) -> OrderState | None: ...
    async def cancel(self, symbol: str, client_id: str) -> None: ...
    async def place_protection(self, symbol: str, qty: Decimal, stop: Decimal, stop_limit: Decimal,
                               take_profit: Decimal | None, ids: ProtectionIds) -> None: ...
    async def cancel_protection(self, symbol: str, ids: ProtectionIds) -> None: ...
    async def amend_stop(self, symbol: str, ids: ProtectionIds, stop: Decimal, stop_limit: Decimal) -> None: ...
    async def aclose(self) -> None: ...


def _venue(symbol: str) -> str:
    base, quote = split_symbol(symbol)
    return base + quote


# ---- Binance -------------------------------------------------------------

BINANCE_STATUS = {"NEW": OPEN, "PENDING_NEW": OPEN, "PARTIALLY_FILLED": PARTIAL, "FILLED": FILLED,
                  "CANCELED": CANCELED, "PENDING_CANCEL": OPEN, "REJECTED": REJECTED, "EXPIRED": EXPIRED,
                  "EXPIRED_IN_MATCH": EXPIRED}
BINANCE_NOT_FOUND = -2013
BINANCE_UNKNOWN_ORDER = -2011


def binance_order(o: dict[str, Any]) -> OrderState:
    filled = dec(o.get("executedQty") or 0)
    quote = dec(o.get("cummulativeQuoteQty") or 0)
    fee, asset = Decimal(0), None
    for f in o.get("fills") or []:
        fee += dec(f.get("commission") or 0)
        asset = f.get("commissionAsset") or asset
    raw = str(o.get("status", ""))
    return OrderState(str(o.get("clientOrderId") or o.get("origClientOrderId") or ""),
                      BINANCE_STATUS.get(raw, OPEN), filled, (quote / filled) if filled > 0 and quote > 0 else None,
                      fee, asset, str(o.get("orderId")) if o.get("orderId") is not None else None, raw)


class BinanceSpotAccount:
    exchange_take_profit = True
    stop_amendable = False

    def __init__(self, http: SignedHttp):
        assert http.exchange == "binance"
        self.http, self.exchange, self.environment = http, "binance", http.environment

    async def instrument(self, symbol: str) -> InstrumentSpec:
        body = await self.http.request("GET", "/api/v3/exchangeInfo", {"symbol": _venue(symbol)})
        syms = (body or {}).get("symbols") or []
        if not syms:
            raise ExchangeRequestError("binance", 200, None, f"{symbol} is not listed")
        return binance_instrument("binance", syms[0])

    async def book(self, symbol: str) -> TopOfBook:
        from datetime import datetime, timezone
        b = await self.http.request("GET", "/api/v3/ticker/bookTicker", {"symbol": _venue(symbol)})
        return TopOfBook("binance", symbol, dec(b["bidPrice"]), dec(b["askPrice"]), datetime.now(timezone.utc),
                         dec(b.get("bidQty") or 0), dec(b.get("askQty") or 0))

    async def bars(self, symbol: str, interval: str, limit: int) -> list[tuple[int, Decimal, Decimal, Decimal]]:
        """(open ms, high, low, close), oldest first, from this environment's own market."""
        rows = await self.http.request("GET", "/api/v3/klines", {"symbol": _venue(symbol), "interval": interval,
                                                                 "limit": min(int(limit), 1000)})
        return sorted((int(r[0]), dec(r[2]), dec(r[3]), dec(r[4])) for r in rows or [])

    async def balances(self) -> dict[str, Balance]:
        acct = await self.http.request("GET", "/api/v3/account", {"omitZeroBalances": "true"})
        return {b["asset"]: Balance(dec(b.get("free") or 0), dec(b.get("locked") or 0)) for b in acct.get("balances") or []}

    async def market(self, symbol: str, side: str, qty: Decimal, client_id: str) -> OrderState | None:
        o = await self.http.request("POST", "/api/v3/order", {
            "symbol": _venue(symbol), "side": side.upper(), "type": "MARKET", "quantity": format(qty, "f"),
            "newClientOrderId": client_id, "newOrderRespType": "FULL"})
        return binance_order(o)

    async def get_order(self, symbol: str, client_id: str) -> OrderState | None:
        try:
            o = await self.http.request("GET", "/api/v3/order", {"symbol": _venue(symbol), "origClientOrderId": client_id})
        except ExchangeRequestError as e:
            if e.code == BINANCE_NOT_FOUND:
                return None
            raise
        st = binance_order(o)
        if st.filled_qty > 0 and st.status == FILLED and st.fee_asset is None and o.get("orderId") is not None:
            trades = await self.http.request("GET", "/api/v3/myTrades", {"symbol": _venue(symbol), "orderId": o["orderId"]})
            for t in trades or []:
                st.fee += dec(t.get("commission") or 0)
                st.fee_asset = t.get("commissionAsset") or st.fee_asset
        return st

    async def cancel(self, symbol: str, client_id: str) -> None:
        try:
            await self.http.request("DELETE", "/api/v3/order", {"symbol": _venue(symbol), "origClientOrderId": client_id})
        except ExchangeRequestError as e:
            if e.code not in (BINANCE_UNKNOWN_ORDER, BINANCE_NOT_FOUND):
                raise

    async def place_protection(self, symbol: str, qty: Decimal, stop: Decimal, stop_limit: Decimal,
                               take_profit: Decimal | None, ids: ProtectionIds) -> None:
        q = format(qty, "f")
        if take_profit is not None:
            await self.http.request("POST", "/api/v3/orderList/oco", {
                "symbol": _venue(symbol), "side": "SELL", "quantity": q, "listClientOrderId": ids.list_id,
                "aboveType": "LIMIT_MAKER", "abovePrice": format(take_profit, "f"), "aboveClientOrderId": ids.take_profit,
                "belowType": "STOP_LOSS_LIMIT", "belowStopPrice": format(stop, "f"), "belowPrice": format(stop_limit, "f"),
                "belowTimeInForce": "GTC", "belowClientOrderId": ids.stop})
        else:
            await self.http.request("POST", "/api/v3/order", {
                "symbol": _venue(symbol), "side": "SELL", "type": "STOP_LOSS_LIMIT", "timeInForce": "GTC",
                "quantity": q, "price": format(stop_limit, "f"), "stopPrice": format(stop, "f"),
                "newClientOrderId": ids.stop})

    async def cancel_protection(self, symbol: str, ids: ProtectionIds) -> None:
        if ids.list_id:
            try:
                await self.http.request("DELETE", "/api/v3/orderList", {"symbol": _venue(symbol),
                                                                       "listClientOrderId": ids.list_id})
            except ExchangeRequestError as e:
                if e.code not in (BINANCE_UNKNOWN_ORDER, BINANCE_NOT_FOUND):
                    raise
        else:
            await self.cancel(symbol, ids.stop)

    async def amend_stop(self, symbol: str, ids: ProtectionIds, stop: Decimal, stop_limit: Decimal) -> None:
        raise NotImplementedError("Binance spot stops cannot be amended; the executor replaces them")

    async def aclose(self) -> None:
        await self.http.aclose()


# ---- Bybit ---------------------------------------------------------------

BYBIT_STATUS = {"New": OPEN, "Created": OPEN, "Untriggered": OPEN, "Triggered": OPEN, "Active": OPEN,
                "PartiallyFilled": PARTIAL, "Filled": FILLED, "Cancelled": CANCELED, "Deactivated": CANCELED,
                "PartiallyFilledCanceled": CANCELED, "Rejected": REJECTED}


def bybit_order(o: dict[str, Any], base: str, quote: str) -> OrderState:
    filled = dec(o.get("cumExecQty") or 0)
    avg = dec(o.get("avgPrice") or 0)
    raw = str(o.get("orderStatus", ""))
    side = str(o.get("side", ""))
    return OrderState(str(o.get("orderLinkId") or ""), BYBIT_STATUS.get(raw, OPEN), filled,
                      avg if avg > 0 else None, dec(o.get("cumExecFee") or 0),
                      base if side == "Buy" else quote, str(o.get("orderId") or "") or None, raw)


class BybitSpotAccount:
    exchange_take_profit = False
    stop_amendable = True

    def __init__(self, http: SignedHttp):
        assert http.exchange == "bybit"
        self.http, self.exchange, self.environment = http, "bybit", http.environment

    async def instrument(self, symbol: str) -> InstrumentSpec:
        res = await self.http.request("GET", "/v5/market/instruments-info", {"category": "spot", "symbol": _venue(symbol)})
        items = (res or {}).get("list") or []
        if not items:
            raise ExchangeRequestError("bybit", 200, None, f"{symbol} is not listed")
        return bybit_instrument(items[0])

    async def book(self, symbol: str) -> TopOfBook:
        from datetime import datetime, timezone
        res = await self.http.request("GET", "/v5/market/tickers", {"category": "spot", "symbol": _venue(symbol)})
        it = ((res or {}).get("list") or [{}])[0]
        return TopOfBook("bybit", symbol, dec(it["bid1Price"]), dec(it["ask1Price"]), datetime.now(timezone.utc),
                         dec(it.get("bid1Size") or 0), dec(it.get("ask1Size") or 0))

    async def bars(self, symbol: str, interval: str, limit: int) -> list[tuple[int, Decimal, Decimal, Decimal]]:
        from yonixalpha_crypto.exchanges.bybit import INTERVALS
        res = await self.http.request("GET", "/v5/market/kline", {"category": "spot", "symbol": _venue(symbol),
                                                                  "interval": INTERVALS[interval], "limit": min(int(limit), 1000)})
        return sorted((int(r[0]), dec(r[2]), dec(r[3]), dec(r[4])) for r in (res or {}).get("list") or [])

    async def balances(self) -> dict[str, Balance]:
        res = await self.http.request("GET", "/v5/account/wallet-balance", {"accountType": "UNIFIED"})
        out: dict[str, Balance] = {}
        for acct in (res or {}).get("list") or []:
            for c in acct.get("coin") or []:
                total, locked = dec(c.get("walletBalance") or 0), dec(c.get("locked") or 0)
                out[c["coin"]] = Balance(total - locked, locked)
        return out

    async def market(self, symbol: str, side: str, qty: Decimal, client_id: str) -> OrderState | None:
        await self.http.request("POST", "/v5/order/create", {
            "category": "spot", "symbol": _venue(symbol), "side": "Buy" if side.upper() == "BUY" else "Sell",
            "orderType": "Market", "qty": format(qty, "f"), "marketUnit": "baseCoin", "orderLinkId": client_id})
        return None  # the create answer has ids only: the executor looks the order up

    async def get_order(self, symbol: str, client_id: str) -> OrderState | None:
        base, quote = split_symbol(symbol)
        for path in ("/v5/order/realtime", "/v5/order/history"):
            res = await self.http.request("GET", path, {"category": "spot", "symbol": _venue(symbol), "orderLinkId": client_id})
            items = (res or {}).get("list") or []
            if items:
                return bybit_order(items[0], base, quote)
        return None

    async def cancel(self, symbol: str, client_id: str, order_filter: str | None = None) -> None:
        try:
            await self.http.request("POST", "/v5/order/cancel", {"category": "spot", "symbol": _venue(symbol),
                                                                 "orderLinkId": client_id, "orderFilter": order_filter})
        except ExchangeRequestError as e:
            if e.code not in (110001, 170213):  # order does not exist / already finished
                raise

    async def place_protection(self, symbol: str, qty: Decimal, stop: Decimal, stop_limit: Decimal,
                               take_profit: Decimal | None, ids: ProtectionIds) -> None:
        await self.http.request("POST", "/v5/order/create", {
            "category": "spot", "symbol": _venue(symbol), "side": "Sell", "orderType": "Limit", "qty": format(qty, "f"),
            "price": format(stop_limit, "f"), "triggerPrice": format(stop, "f"), "orderFilter": "tpslOrder",
            "orderLinkId": ids.stop})

    async def cancel_protection(self, symbol: str, ids: ProtectionIds) -> None:
        await self.cancel(symbol, ids.stop, "tpslOrder")

    async def amend_stop(self, symbol: str, ids: ProtectionIds, stop: Decimal, stop_limit: Decimal) -> None:
        await self.http.request("POST", "/v5/order/amend", {
            "category": "spot", "symbol": _venue(symbol), "orderLinkId": ids.stop,
            "triggerPrice": format(stop, "f"), "price": format(stop_limit, "f")})

    async def aclose(self) -> None:
        await self.http.aclose()


def account_for(http: SignedHttp) -> SpotAccount:
    return BinanceSpotAccount(http) if http.exchange == "binance" else BybitSpotAccount(http)
