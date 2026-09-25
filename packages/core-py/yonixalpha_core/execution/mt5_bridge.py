"""MT5BridgeProvider — forex/CFD execution through services/mt5-bridge.

The MetaTrader5 Python package only runs on Windows next to a logged-in
MT5 terminal, so YonixAlpha never talks to MT5 directly. The bridge runs
on that Windows host, holds MT5_LOGIN / MT5_PASSWORD / MT5_SERVER there,
and exposes a small bearer-token HTTP API that this provider calls. The
YonixAlpha server only knows MT5_BRIDGE_URL and MT5_BRIDGE_TOKEN; the
frontend never sees any of it.

Quantities cross the bridge in BASE units (e.g. 10000 EURUSD), not lots:
the bridge converts with the symbol's contract size and volume step, so
this provider behaves like every other one. The bridge refuses symbols
whose profit currency differs from the account currency, because the
shared PnL accounting books PnL in the account currency.

Contract (services/mt5-bridge/app/main.py implements it):
  GET  /health                      -> {"connected", "account_currency", ...}
  GET  /symbols/{symbol}            -> {"qty_step", "min_qty", "tick_size", "digits", "profit_currency"}
  POST /orders                      {symbol, side, qty, reduce_only, client_id, ref_price, deviation_points}
  GET  /orders/{client_id}?symbol=  -> order state
  POST /protection                  {symbol, stop_price}      (stop on every bridge-owned position)
  DELETE /protection/{symbol}
  GET  /protection/{symbol}
  GET  /positions/{symbol}          -> {"size" (signed base units), "entry_price", "unrealized_pnl"} | 404
  GET  /balance                     -> {"currency", "free_margin", "balance", "equity"}
  GET  /fills?symbol=&start_ms=     -> [fill]
"""

from decimal import Decimal
from typing import Any

import httpx

from yonixalpha_core.execution.base import (
    UNKNOWN, ExecutionError, Fill, InstrumentRules, NotConfigured, OrderState, PositionInfo, dec,
)
from yonixalpha_core.venues.common import tracked


class MT5BridgeProvider:
    venue = "mt5"

    def __init__(self, client: httpx.AsyncClient, base_url: str | None, token: Any = None, deviation_points: int = 20):
        self.client = client
        self.base = (base_url or "").rstrip("/")
        self._token = token
        self.deviation_points = deviation_points
        self.quote_currency = "USD"  # replaced by the account currency on the first balance()
        self._rules: dict[str, InstrumentRules] = {}

    def _secret(self) -> str:
        t = self._token
        if t is None:
            return ""
        return t.get_secret_value() if hasattr(t, "get_secret_value") else str(t)

    @property
    def configured(self) -> bool:
        return bool(self.base and self._secret())

    async def _call(self, method: str, path: str, json: dict | None = None, params: dict | None = None,
                    allow_404: bool = False) -> Any:
        if not self.configured:
            raise NotConfigured("MT5_BRIDGE_URL / MT5_BRIDGE_TOKEN not set")

        async def go():
            try:
                resp = await self.client.request(method, f"{self.base}{path}", json=json, params=params,
                                                 headers={"Authorization": f"Bearer {self._secret()}"}, timeout=15.0)
            except httpx.HTTPError as exc:
                raise ExecutionError(f"mt5-bridge {path}: transport {type(exc).__name__}") from exc
            if allow_404 and resp.status_code == 404:
                return None
            try:
                body = resp.json()
            except ValueError as exc:
                raise ExecutionError(f"mt5-bridge {path}: HTTP {resp.status_code} non-JSON") from exc
            if resp.status_code >= 400:
                detail = body.get("detail") if isinstance(body, dict) else body
                raise ExecutionError(f"mt5-bridge {path}: HTTP {resp.status_code} {str(detail)[:200]}")
            return body
        return await tracked("mt5", go())

    async def health(self) -> dict[str, Any]:
        return await self._call("GET", "/health")

    async def instrument(self, symbol: str) -> InstrumentRules:
        if symbol in self._rules:
            return self._rules[symbol]
        s = await self._call("GET", f"/symbols/{symbol}")
        rules = InstrumentRules(symbol, dec(s.get("qty_step"), Decimal(1)), dec(s.get("tick_size"), Decimal("0.00001")),
                                dec(s.get("min_qty"), Decimal(0)))
        self._rules[symbol] = rules
        return rules

    async def prepare(self, symbol: str, leverage: int) -> None:
        # MT5 leverage is an account property set by the broker; the bridge
        # only checks that the symbol is tradable and the terminal connected.
        await self._call("GET", f"/symbols/{symbol}")

    @staticmethod
    def _state(body: dict, client_id: str) -> OrderState:
        return OrderState(client_id=client_id, status=body.get("status") or UNKNOWN,
                          filled_qty=dec(body.get("filled_qty"), Decimal(0)), avg_price=dec(body.get("avg_price")),
                          fee=dec(body.get("fee"), Decimal(0)), exchange_id=body.get("exchange_id"), error=body.get("error"),
                          raw={k: body.get(k) for k in ("retcode", "comment", "deal", "order")})

    async def market_order(self, symbol: str, side: str, qty: Decimal, reduce_only: bool, client_id: str,
                           ref_price: Decimal | None = None) -> OrderState:
        body = await self._call("POST", "/orders", json={
            "symbol": symbol, "side": side, "qty": format(qty, "f"), "reduce_only": reduce_only, "client_id": client_id,
            "ref_price": format(ref_price, "f") if ref_price is not None else None,
            "deviation_points": self.deviation_points})
        return self._state(body, client_id)

    async def order_status(self, symbol: str, client_id: str) -> OrderState:
        body = await self._call("GET", f"/orders/{client_id}", params={"symbol": symbol}, allow_404=True)
        if body is None:
            return OrderState(client_id=client_id, status="REJECTED", error="order unknown to the bridge and the terminal")
        return self._state(body, client_id)

    async def set_stop(self, symbol: str, position_side: str, stop_price: Decimal, qty: Decimal, client_id: str) -> str:
        rules = await self.instrument(symbol)
        body = await self._call("POST", "/protection", json={"symbol": symbol, "stop_price": format(rules.round_price(stop_price), "f")})
        return str(body.get("id") or f"position-sl:{symbol}")

    async def cancel_stop(self, symbol: str, stop_id: str) -> None:
        return None  # the bridge modifies the positions' SL in place; set_stop replaced it

    async def cancel_protection(self, symbol: str) -> None:
        await self._call("DELETE", f"/protection/{symbol}")

    async def open_protection(self, symbol: str) -> list[dict[str, Any]]:
        return await self._call("GET", f"/protection/{symbol}") or []

    async def position(self, symbol: str) -> PositionInfo | None:
        body = await self._call("GET", f"/positions/{symbol}", allow_404=True)
        if not body or dec(body.get("size"), Decimal(0)) == 0:
            return None
        return PositionInfo(symbol, dec(body["size"]), dec(body.get("entry_price")), dec(body.get("mark_price")),
                            dec(body.get("unrealized_pnl")))

    async def balance(self) -> Decimal:
        body = await self._call("GET", "/balance")
        if body.get("currency"):
            self.quote_currency = body["currency"]
        return dec(body.get("free_margin"), Decimal(0))

    async def fills_since(self, symbol: str, start_ms: int) -> list[Fill]:
        rows = await self._call("GET", "/fills", params={"symbol": symbol, "start_ms": start_ms})
        return [Fill(str(r.get("order_id")), r.get("client_id"), r.get("side"), dec(r.get("qty"), Decimal(0)),
                     dec(r.get("price"), Decimal(0)), dec(r.get("fee"), Decimal(0)), dec(r.get("realized_pnl")),
                     int(r.get("time_ms", 0))) for r in rows or []]
