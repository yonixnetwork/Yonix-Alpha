"""In-memory spot exchanges for the executor tests, served through
httpx.MockTransport so every request goes through the real signing client
and the real adapters. Each fake follows its own exchange's documented
behaviour (and where that is only assumed, the test says so):

Binance: MARKET orders answer FULL with fills and the commission (in the
coin received); STOP_LOSS_LIMIT and OCO legs lock the coins; GET order of an
unknown client id -> code -2013; DELETE of an unknown order -> -2011; a
client id already used by an OPEN order -> -2010; when one OCO leg fills the
other becomes EXPIRED.
Bybit: /v5/order/create answers ids only; Market + marketUnit baseCoin
fills at once; a spot tpslOrder sell is Untriggered and reserves the coins;
amend changes triggerPrice / price in place; open orders are in
/v5/order/realtime, finished ones in /v5/order/history; a duplicate
orderLinkId -> retCode 10001.

Knobs (acting on the next order-changing request): `timeout_next` (the
request IS executed, then the answer is lost), `drop_next` (the request
never arrives, the client sees a timeout),
`fill_ratio` (the next market order fills only partly), `reject_next`
(retCode / code error), and `trigger(bid)` to move the market.
"""

from __future__ import annotations

import itertools
import json
from decimal import ROUND_DOWN, Decimal
from urllib.parse import parse_qsl

import httpx

FEE = Decimal("0.001")


def _d(x) -> Decimal:
    return Decimal(str(x))


class FakeSpot:
    def __init__(self, *, bid="100", ask="100.1", usdt="1000", base_asset="BTC", base="0"):
        self.bid, self.ask = _d(bid), _d(ask)
        self.base_asset = base_asset
        self.free = {"USDT": _d(usdt), base_asset: _d(base)}
        self.locked = {"USDT": Decimal(0), base_asset: Decimal(0)}
        self.orders: dict[str, dict] = {}
        self.ids = itertools.count(1000)
        self.timeout_next = False
        self.drop_next = False
        self.fill_ratio: Decimal | None = None
        self.reject_next: tuple[int, str] | None = None
        self.requests: list[httpx.Request] = []

    # --- shared mechanics -------------------------------------------------
    def _lock(self, qty: Decimal) -> bool:
        if self.free[self.base_asset] < qty:
            return False
        self.free[self.base_asset] -= qty
        self.locked[self.base_asset] += qty
        return True

    def _unlock(self, qty: Decimal) -> None:
        self.locked[self.base_asset] -= qty
        self.free[self.base_asset] += qty

    def _market(self, side: str, qty: Decimal) -> tuple[Decimal, Decimal, Decimal, str]:
        ratio = self.fill_ratio if self.fill_ratio is not None else Decimal(1)
        self.fill_ratio = None
        q = (qty * ratio).quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
        if side == "BUY":
            cost = q * self.ask
            if cost > self.free["USDT"]:
                raise ValueError("insufficient balance")
            fee = q * FEE
            self.free["USDT"] -= cost
            self.free[self.base_asset] += q - fee
            return q, self.ask, fee, self.base_asset
        if q > self.free[self.base_asset]:
            raise ValueError("insufficient balance")
        fee = q * self.bid * FEE
        self.free[self.base_asset] -= q
        self.free["USDT"] += q * self.bid - fee
        return q, self.bid, fee, "USDT"

    def _fill_stop(self, o: dict) -> None:
        q, px = o["qty"], o["price"]
        self.locked[self.base_asset] -= q
        fee = q * px * FEE
        self.free["USDT"] += q * px - fee
        o.update(filled=q, avg=px, fee=fee, fee_asset="USDT")

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        sends = request.method != "GET"  # the knobs act on the next order-changing request
        if self.drop_next and sends:
            self.drop_next = False
            raise httpx.ConnectTimeout("dropped", request=request)
        timeout = self.timeout_next and sends
        self.timeout_next = self.timeout_next and not sends
        resp = self.route(request)
        if timeout:
            raise httpx.ReadTimeout("answer lost", request=request)
        return resp

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def order_count(self, side: str, kind: str = "MARKET") -> int:
        return sum(1 for o in self.orders.values() if o["side"] == side and o["kind"] == kind)


class FakeBinance(FakeSpot):
    def route(self, r: httpx.Request) -> httpx.Response:
        q = dict(parse_qsl(r.url.query.decode()))
        p, m = r.url.path, r.method
        if self.reject_next and m in ("POST",):
            code, msg = self.reject_next
            self.reject_next = None
            return httpx.Response(400, json={"code": code, "msg": msg})
        if p == "/api/v3/exchangeInfo":
            return httpx.Response(200, json={"symbols": [{"symbol": "BTCUSDT", "status": "TRADING", "baseAsset": "BTC",
                                  "quoteAsset": "USDT", "filters": [
                                      {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                                      {"filterType": "LOT_SIZE", "stepSize": "0.00001", "minQty": "0.00001"},
                                      {"filterType": "NOTIONAL", "minNotional": "5"}]}]})
        if p == "/api/v3/ticker/bookTicker":
            return httpx.Response(200, json={"bidPrice": str(self.bid), "askPrice": str(self.ask), "bidQty": "5", "askQty": "5"})
        if p == "/api/v3/account":
            return httpx.Response(200, json={"canTrade": True, "balances": [
                {"asset": a, "free": str(self.free[a]), "locked": str(self.locked[a])} for a in self.free]})
        if p == "/api/v3/order" and m == "GET":
            o = self.orders.get(q.get("origClientOrderId"))
            if o is None:
                return httpx.Response(400, json={"code": -2013, "msg": "Order does not exist."})
            return httpx.Response(200, json=self._json(o, fills=False))
        if p == "/api/v3/myTrades":
            o = next((x for x in self.orders.values() if str(x["id"]) == q.get("orderId")), None)
            return httpx.Response(200, json=[] if not o or not o["filled"] else
                                  [{"commission": str(o["fee"]), "commissionAsset": o["fee_asset"]}])
        if p == "/api/v3/order" and m == "POST":
            cid = q["newClientOrderId"]
            if cid in self.orders and self.orders[cid]["status"] in ("NEW", "PARTIALLY_FILLED"):
                return httpx.Response(400, json={"code": -2010, "msg": "Duplicate order sent."})
            qty = _d(q["quantity"])
            if qty * self.ask < 5:
                return httpx.Response(400, json={"code": -1013, "msg": "Filter failure: NOTIONAL"})
            if q["type"] == "MARKET":
                try:
                    f, px, fee, fa = self._market(q["side"], qty)
                except ValueError:
                    return httpx.Response(400, json={"code": -2010, "msg": "Account has insufficient balance for requested action."})
                o = self._new(cid, q["side"], "MARKET", qty, status="FILLED" if f == qty else "EXPIRED",
                              filled=f, avg=px, fee=fee, fee_asset=fa)
                return httpx.Response(200, json=self._json(o, fills=True))
            if q["type"] == "STOP_LOSS_LIMIT":
                if not self._lock(qty):
                    return httpx.Response(400, json={"code": -2010, "msg": "Account has insufficient balance for requested action."})
                o = self._new(cid, "SELL", "STOP_LOSS_LIMIT", qty, price=_d(q["price"]), stop=_d(q["stopPrice"]))
                return httpx.Response(200, json=self._json(o, fills=False))
        if p == "/api/v3/order" and m == "DELETE":
            o = self.orders.get(q.get("origClientOrderId"))
            if o is None or o["status"] not in ("NEW", "PARTIALLY_FILLED"):
                return httpx.Response(400, json={"code": -2011, "msg": "Unknown order sent."})
            o["status"] = "CANCELED"
            self._unlock(o["qty"])
            return httpx.Response(200, json=self._json(o, fills=False))
        if p == "/api/v3/orderList/oco":
            qty = _d(q["quantity"])
            if _d(q["abovePrice"]) <= self.ask:
                return httpx.Response(400, json={"code": -2010, "msg": "Order would immediately match and take."})
            if not self._lock(qty):
                return httpx.Response(400, json={"code": -2010, "msg": "Account has insufficient balance for requested action."})
            lst = q["listClientOrderId"]
            self._new(q["aboveClientOrderId"], "SELL", "LIMIT_MAKER", qty, price=_d(q["abovePrice"]), oco=lst)
            self._new(q["belowClientOrderId"], "SELL", "STOP_LOSS_LIMIT", qty, price=_d(q["belowPrice"]),
                      stop=_d(q["belowStopPrice"]), oco=lst)
            return httpx.Response(200, json={"listClientOrderId": lst, "listStatusType": "EXEC_STARTED"})
        if p == "/api/v3/orderList" and m == "DELETE":
            legs = [o for o in self.orders.values() if o.get("oco") == q["listClientOrderId"] and o["status"] == "NEW"]
            if not legs:
                return httpx.Response(400, json={"code": -2011, "msg": "Unknown order list sent."})
            for o in legs:
                o["status"] = "CANCELED"
            self._unlock(legs[0]["qty"])
            return httpx.Response(200, json={"listStatusType": "ALL_DONE"})
        return httpx.Response(404, json={"code": -1, "msg": f"fake: {m} {p} not implemented"})

    def _new(self, cid, side, kind, qty, *, status="NEW", price=None, stop=None, filled=Decimal(0), avg=None,
             fee=Decimal(0), fee_asset=None, oco=None) -> dict:
        o = {"cid": cid, "id": next(self.ids), "side": side, "kind": kind, "qty": qty, "status": status, "price": price,
             "stop": stop, "filled": filled, "avg": avg, "fee": fee, "fee_asset": fee_asset, "oco": oco}
        self.orders[cid] = o
        return o

    def _json(self, o: dict, fills: bool) -> dict:
        out = {"clientOrderId": o["cid"], "orderId": o["id"], "status": o["status"], "executedQty": str(o["filled"]),
               "cummulativeQuoteQty": str(o["filled"] * (o["avg"] or 0)), "type": o["kind"], "side": o["side"]}
        if fills and o["filled"]:
            out["fills"] = [{"price": str(o["avg"]), "qty": str(o["filled"]), "commission": str(o["fee"]),
                             "commissionAsset": o["fee_asset"]}]
        return out

    def trigger(self, bid) -> None:
        """Move the market; stops at or above the bid fill at their limit, a
        target at or below the bid fills at its price; OCO siblings expire."""
        self.bid, self.ask = _d(bid), _d(bid) + Decimal("0.1")
        for o in list(self.orders.values()):
            if o["status"] != "NEW":
                continue
            hit = (o["kind"] == "STOP_LOSS_LIMIT" and self.bid <= o["stop"]) or \
                  (o["kind"] == "LIMIT_MAKER" and self.bid >= o["price"])
            if hit:
                self._fill_stop(o)
                o["status"] = "FILLED"
                for sib in self.orders.values():
                    if sib is not o and o.get("oco") and sib.get("oco") == o["oco"] and sib["status"] == "NEW":
                        sib["status"] = "EXPIRED"

    def exchange_cancels_protection(self) -> None:
        for o in self.orders.values():
            if o["status"] == "NEW" and o["kind"] in ("STOP_LOSS_LIMIT", "LIMIT_MAKER"):
                o["status"] = "CANCELED"
        self.free[self.base_asset] += self.locked[self.base_asset]
        self.locked[self.base_asset] = Decimal(0)


class FakeBybit(FakeSpot):
    STATUS_OPEN = ("New", "Untriggered", "PartiallyFilled")

    def ok(self, result) -> httpx.Response:
        return httpx.Response(200, json={"retCode": 0, "retMsg": "OK", "result": result, "time": 1})

    def err(self, code, msg) -> httpx.Response:
        return httpx.Response(200, json={"retCode": code, "retMsg": msg, "result": {}})

    def route(self, r: httpx.Request) -> httpx.Response:
        p, m = r.url.path, r.method
        q = dict(parse_qsl(r.url.query.decode())) if m == "GET" else json.loads(r.content or b"{}")
        if self.reject_next and m == "POST":
            code, msg = self.reject_next
            self.reject_next = None
            return self.err(code, msg)
        if p == "/v5/market/instruments-info":
            return self.ok({"list": [{"symbol": "BTCUSDT", "baseCoin": "BTC", "quoteCoin": "USDT", "status": "Trading",
                                      "lotSizeFilter": {"basePrecision": "0.000001", "minOrderQty": "0.000048",
                                                        "minOrderAmt": "5"}, "priceFilter": {"tickSize": "0.01"}}]})
        if p == "/v5/market/tickers":
            return self.ok({"list": [{"bid1Price": str(self.bid), "ask1Price": str(self.ask), "bid1Size": "5", "ask1Size": "5"}]})
        if p == "/v5/account/wallet-balance":
            return self.ok({"list": [{"coin": [{"coin": a, "walletBalance": str(self.free[a] + self.locked[a]),
                                                "locked": str(self.locked[a])} for a in self.free]}]})
        if p in ("/v5/order/realtime", "/v5/order/history"):
            o = self.orders.get(q.get("orderLinkId"))
            want_open = p == "/v5/order/realtime"
            if o is None or (o["status"] in self.STATUS_OPEN) != want_open:
                return self.ok({"list": []})
            return self.ok({"list": [self._json(o)]})
        if p == "/v5/order/create":
            cid = q["orderLinkId"]
            if cid in self.orders:
                return self.err(10001, "Duplicate clientOrderId.")
            qty = _d(q["qty"])
            if qty * self.ask < 5:
                return self.err(170140, "Order value exceeded lower limit.")
            if q["orderType"] == "Market":
                assert q.get("marketUnit") == "baseCoin"
                side = "BUY" if q["side"] == "Buy" else "SELL"
                try:
                    f, px, fee, fa = self._market(side, qty)
                except ValueError:
                    return self.err(170131, "Insufficient balance.")
                self._new(cid, side, "MARKET", qty, "Filled" if f == qty else "PartiallyFilledCanceled",
                          filled=f, avg=px, fee=fee)
            elif q.get("orderFilter") == "tpslOrder":
                if not self._lock(qty):
                    return self.err(170131, "Insufficient balance.")
                self._new(cid, "SELL", "TPSL", qty, "Untriggered", price=_d(q["price"]), stop=_d(q["triggerPrice"]))
            else:
                return self.err(10001, "fake: unsupported order")
            return self.ok({"orderId": str(self.orders[cid]["id"]), "orderLinkId": cid})
        if p == "/v5/order/amend":
            o = self.orders.get(q["orderLinkId"])
            if o is None or o["status"] not in self.STATUS_OPEN:
                return self.err(110001, "Order does not exist.")
            o["stop"], o["price"] = _d(q["triggerPrice"]), _d(q["price"])
            return self.ok({"orderId": str(o["id"]), "orderLinkId": o["cid"]})
        if p == "/v5/order/cancel":
            o = self.orders.get(q["orderLinkId"])
            if o is None or o["status"] not in self.STATUS_OPEN:
                return self.err(170213, "Order does not exist.")
            o["status"] = "Cancelled"
            self._unlock(o["qty"])
            return self.ok({"orderId": str(o["id"]), "orderLinkId": o["cid"]})
        return self.err(10001, f"fake: {m} {p} not implemented")

    def _new(self, cid, side, kind, qty, status, *, price=None, stop=None, filled=Decimal(0), avg=None,
             fee=Decimal(0)) -> dict:
        o = {"cid": cid, "id": next(self.ids), "side": side, "kind": kind, "qty": qty, "status": status, "price": price,
             "stop": stop, "filled": filled, "avg": avg, "fee": fee}
        self.orders[cid] = o
        return o

    def _json(self, o: dict) -> dict:
        return {"orderLinkId": o["cid"], "orderId": str(o["id"]), "orderStatus": o["status"],
                "side": "Buy" if o["side"] == "BUY" else "Sell", "cumExecQty": str(o["filled"]),
                "avgPrice": str(o["avg"] or ""), "cumExecFee": str(o["fee"]),
                "triggerPrice": str(o["stop"] or ""), "price": str(o["price"] or "")}

    def trigger(self, bid) -> None:
        self.bid, self.ask = _d(bid), _d(bid) + Decimal("0.1")
        for o in list(self.orders.values()):
            if o["status"] == "Untriggered" and self.bid <= o["stop"]:
                self._fill_stop(o)
                o["status"] = "Filled"

    def exchange_cancels_protection(self) -> None:
        for o in self.orders.values():
            if o["status"] == "Untriggered":
                o["status"] = "Deactivated"
        self.free[self.base_asset] += self.locked[self.base_asset]
        self.locked[self.base_asset] = Decimal(0)
