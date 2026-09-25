"""MT5 bridge — a small authenticated HTTP API in front of a logged-in
MetaTrader 5 terminal, so YonixAlpha (Linux) can trade FX through MT5
(Windows-only). Implements the contract documented in
packages/core-py/yonixalpha_core/execution/mt5_bridge.py.

Runs ON THE WINDOWS HOST, next to the terminal:
    set MT5_LOGIN=...  MT5_PASSWORD=...  MT5_SERVER=...  MT5_BRIDGE_TOKEN=<long random>
    python -m app.main            (listens on MT5_BRIDGE_HOST:MT5_BRIDGE_PORT)

Security:
- every endpoint requires `Authorization: Bearer <MT5_BRIDGE_TOKEN>`
  (constant-time compare); the bridge refuses to start without a token of
  at least 32 characters (fail closed);
- MT5 credentials are read from this host's environment only and never
  returned by any endpoint;
- it binds to 127.0.0.1 by default: expose it to the YonixAlpha server
  only through a private tunnel (WireGuard / SSH), never the internet;
- only positions opened by the bridge (its magic number) are ever
  modified or closed — manual trades on the same account are untouched.

Quantities are in BASE units (10000 = 0.1 lot of EURUSD at 100000
contract size) and converted with the symbol's contract size / volume
step. Symbols whose profit currency is not the account currency are
refused, because YonixAlpha books PnL in the account currency.

Idempotency: each order's client id is written to the state file before
the order is sent and mapped to an MT5 comment; a repeated client id
returns the stored result, and an order whose result was lost in a crash
is found again in the terminal's deal history by that comment.
"""

import hashlib
import hmac
import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

TIMEFRAMES = {"1m": "TIMEFRAME_M1", "5m": "TIMEFRAME_M5", "15m": "TIMEFRAME_M15", "30m": "TIMEFRAME_M30",
              "1h": "TIMEFRAME_H1", "4h": "TIMEFRAME_H4", "1d": "TIMEFRAME_D1"}
INTERVAL_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000, "1h": 3_600_000, "4h": 14_400_000,
               "1d": 86_400_000}
MIN_TOKEN_LENGTH = 32
HISTORY_DAYS = 7


class OrderIn(BaseModel):
    symbol: str
    side: str  # BUY | SELL
    qty: str  # base units
    reduce_only: bool = False
    client_id: str
    ref_price: str | None = None
    deviation_points: int = 20


class StopIn(BaseModel):
    symbol: str
    stop_price: str


def comment_for(client_id: str) -> str:
    # MT5 comments are at most 31 characters.
    return "yx" + hashlib.sha256(client_id.encode()).hexdigest()[:26]


class Bridge:
    """All terminal access, serialized: the MetaTrader5 package is not
    thread-safe."""

    def __init__(self, mt5, magic: int, state_path: Path):
        self.mt5, self.magic, self.state_path = mt5, magic, state_path
        self.lock = threading.Lock()
        self.state: dict[str, Any] = json.loads(state_path.read_text()) if state_path.exists() else {}

    def _save(self) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state))
        os.replace(tmp, self.state_path)

    # -- helpers ---------------------------------------------------------------

    def account(self):
        acc = self.mt5.account_info()
        if acc is None:
            raise HTTPException(503, f"terminal not logged in: {self.mt5.last_error()}")
        return acc

    def symbol(self, symbol: str):
        info = self.mt5.symbol_info(symbol)
        if info is None:
            raise HTTPException(404, f"unknown symbol {symbol}")
        if not info.visible and not self.mt5.symbol_select(symbol, True):
            raise HTTPException(409, f"symbol {symbol} cannot be selected in Market Watch")
        return info

    def check_currency(self, info) -> None:
        cur = self.account().currency
        if info.currency_profit != cur:
            raise HTTPException(409, f"{info.name} profits in {info.currency_profit}, account is {cur}: refused "
                                     "(PnL is booked in the account currency)")

    def own_positions(self, symbol: str) -> list:
        return [p for p in (self.mt5.positions_get(symbol=symbol) or ()) if p.magic == self.magic]

    def filling(self, info) -> int:
        mode = int(getattr(info, "filling_mode", 0) or 0)
        if mode & 2:
            return self.mt5.ORDER_FILLING_IOC
        if mode & 1:
            return self.mt5.ORDER_FILLING_FOK
        return self.mt5.ORDER_FILLING_RETURN

    def deals_for(self, comment: str) -> list:
        now = datetime.now(timezone.utc)
        deals = self.mt5.history_deals_get(now - timedelta(days=HISTORY_DAYS), now + timedelta(days=1)) or ()
        return [d for d in deals if d.comment == comment and d.magic == self.magic]

    def result_from_deals(self, client_id: str, deals: list, contract: Decimal) -> dict[str, Any]:
        vol = sum((Decimal(str(d.volume)) for d in deals), Decimal(0))
        if vol <= 0:
            return {"client_id": client_id, "status": "REJECTED", "filled_qty": "0", "error": "no deal for this order"}
        avg = sum((Decimal(str(d.volume)) * Decimal(str(d.price)) for d in deals), Decimal(0)) / vol
        fee = -sum((Decimal(str(d.commission)) + Decimal(str(getattr(d, "fee", 0) or 0)) for d in deals), Decimal(0))
        return {"client_id": client_id, "status": "FILLED", "filled_qty": str(vol * contract), "avg_price": str(avg),
                "fee": str(fee), "exchange_id": str(deals[0].order)}

    # -- orders ------------------------------------------------------------------

    def order(self, o: OrderIn) -> dict[str, Any]:
        with self.lock:
            prior = self.state.get(o.client_id)
            if prior and prior.get("status") != "SENDING":
                return prior
            info = self.symbol(o.symbol)
            self.check_currency(info)
            contract = Decimal(str(info.trade_contract_size))
            comment = comment_for(o.client_id)
            if prior:  # crashed after sending: the terminal's history is the truth
                result = self.result_from_deals(o.client_id, self.deals_for(comment), contract)
                self.state[o.client_id] = result
                self._save()
                return result
            step = Decimal(str(info.volume_step))
            lots = (Decimal(o.qty) / contract / step).to_integral_value(ROUND_DOWN) * step
            if lots < Decimal(str(info.volume_min)):
                return {"client_id": o.client_id, "status": "REJECTED", "filled_qty": "0",
                        "error": f"{o.qty} is below the minimum volume {info.volume_min} lots"}
            self.state[o.client_id] = {"status": "SENDING", "comment": comment}
            self._save()
            buy = o.side == "BUY"
            tick = self.mt5.symbol_info_tick(o.symbol)
            base = {"action": self.mt5.TRADE_ACTION_DEAL, "symbol": o.symbol,
                    "type": self.mt5.ORDER_TYPE_BUY if buy else self.mt5.ORDER_TYPE_SELL,
                    "price": tick.ask if buy else tick.bid, "deviation": int(o.deviation_points), "magic": self.magic,
                    "comment": comment, "type_time": self.mt5.ORDER_TIME_GTC, "type_filling": self.filling(info)}
            requests = []
            if o.reduce_only:
                want = lots
                close_type = self.mt5.POSITION_TYPE_SELL if buy else self.mt5.POSITION_TYPE_BUY
                for p in self.own_positions(o.symbol):
                    if want <= 0 or p.type != close_type:
                        continue
                    v = min(want, Decimal(str(p.volume)))
                    requests.append({**base, "volume": float(v), "position": p.ticket})
                    want -= v
                if not requests:
                    result = {"client_id": o.client_id, "status": "REJECTED", "filled_qty": "0",
                              "error": "no bridge-owned position to reduce"}
                    self.state[o.client_id] = result
                    self._save()
                    return result
            else:
                requests.append({**base, "volume": float(lots)})
            errors = []
            for req in requests:
                res = self.mt5.order_send(req)
                if res is None or res.retcode not in (self.mt5.TRADE_RETCODE_DONE, self.mt5.TRADE_RETCODE_DONE_PARTIAL):
                    errors.append(f"retcode {getattr(res, 'retcode', None)} {getattr(res, 'comment', self.mt5.last_error())}")
            result = self.result_from_deals(o.client_id, self.deals_for(comment), contract)
            if errors:
                result["error"] = "; ".join(errors)[:300]
                if result["status"] == "FILLED" and len(errors) < len(requests):
                    result["status"] = "CANCELED"  # part of a multi-position close filled: terminal, filled_qty > 0
            self.state[o.client_id] = result
            self._save()
            return result

    def order_status(self, client_id: str, symbol: str) -> dict[str, Any] | None:
        with self.lock:
            prior = self.state.get(client_id)
            if prior and prior.get("status") != "SENDING":
                return prior
            info = self.symbol(symbol)
            deals = self.deals_for(comment_for(client_id))
            if not deals and not prior:
                return None
            return self.result_from_deals(client_id, deals, Decimal(str(info.trade_contract_size)))

    # -- protection ---------------------------------------------------------------

    def set_stop(self, symbol: str, stop: float) -> dict[str, Any]:
        with self.lock:
            positions = self.own_positions(symbol)
            if not positions:
                raise HTTPException(404, f"no bridge-owned position on {symbol}")
            for p in positions:
                res = self.mt5.order_send({"action": self.mt5.TRADE_ACTION_SLTP, "symbol": symbol, "position": p.ticket,
                                           "sl": stop, "tp": p.tp, "magic": self.magic})
                if res is None or res.retcode != self.mt5.TRADE_RETCODE_DONE:
                    raise HTTPException(502, f"stop not set on ticket {p.ticket}: retcode {getattr(res, 'retcode', None)} "
                                             f"{getattr(res, 'comment', self.mt5.last_error())}")
            return {"id": f"position-sl:{symbol}", "positions": len(positions)}

    def protection(self, symbol: str) -> list[dict[str, Any]]:
        with self.lock:
            return [{"id": str(p.ticket), "type": "STOP_MARKET", "trigger_price": str(p.sl),
                     "side": "SELL" if p.type == self.mt5.POSITION_TYPE_BUY else "BUY"}
                    for p in self.own_positions(symbol) if p.sl]

    # -- account --------------------------------------------------------------------

    def position(self, symbol: str) -> dict[str, Any] | None:
        with self.lock:
            positions = self.own_positions(symbol)
            if not positions:
                return None
            contract = Decimal(str(self.symbol(symbol).trade_contract_size))
            signed = [(Decimal(str(p.volume)) * (1 if p.type == self.mt5.POSITION_TYPE_BUY else -1), p) for p in positions]
            size = sum((v for v, _ in signed), Decimal(0))
            gross = sum((abs(v) for v, _ in signed), Decimal(0))
            entry = sum((abs(v) * Decimal(str(p.price_open)) for v, p in signed), Decimal(0)) / gross
            return {"size": str(size * contract), "entry_price": str(entry), "mark_price": str(positions[0].price_current),
                    "unrealized_pnl": str(sum((Decimal(str(p.profit)) for p in positions), Decimal(0)))}

    def fills(self, symbol: str, start_ms: int) -> list[dict[str, Any]]:
        with self.lock:
            contract = Decimal(str(self.symbol(symbol).trade_contract_size))
            start = datetime.fromtimestamp(start_ms / 1000, timezone.utc)
            deals = self.mt5.history_deals_get(start, datetime.now(timezone.utc) + timedelta(days=1)) or ()
            return [{"order_id": str(d.order), "client_id": None, "side": "BUY" if d.type == self.mt5.DEAL_TYPE_BUY else "SELL",
                     "qty": str(Decimal(str(d.volume)) * contract), "price": str(d.price),
                     "fee": str(-(Decimal(str(d.commission)) + Decimal(str(getattr(d, "fee", 0) or 0)))),
                     "realized_pnl": str(d.profit), "time_ms": int(d.time_msc)}
                    for d in deals if d.symbol == symbol and d.magic == self.magic]


def create_app(mt5=None, env: dict[str, str] | None = None) -> FastAPI:
    env = dict(os.environ if env is None else env)
    token = env.get("MT5_BRIDGE_TOKEN", "")
    if len(token) < MIN_TOKEN_LENGTH:
        raise RuntimeError(f"MT5_BRIDGE_TOKEN must be set to at least {MIN_TOKEN_LENGTH} characters")
    if mt5 is None:  # pragma: no cover - Windows only
        import MetaTrader5 as mt5
    kwargs = {k: v for k, v in {"path": env.get("MT5_TERMINAL_PATH")}.items() if v}
    login = env.get("MT5_LOGIN")
    if not mt5.initialize(**kwargs):
        raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")
    if login and not mt5.login(int(login), password=env.get("MT5_PASSWORD", ""), server=env.get("MT5_SERVER", "")):
        err = mt5.last_error()
        mt5.shutdown()
        raise RuntimeError(f"MT5 login failed: {err}")
    bridge = Bridge(mt5, int(env.get("MT5_BRIDGE_MAGIC", "20260925")), Path(env.get("MT5_BRIDGE_STATE_PATH", "mt5_bridge_state.json")))
    app = FastAPI(title="YonixAlpha MT5 bridge", docs_url=None, redoc_url=None, openapi_url=None)

    def auth(authorization: str = Header(default="")) -> None:
        given = authorization.removeprefix("Bearer ").strip()
        if not hmac.compare_digest(given.encode(), token.encode()):
            raise HTTPException(401, "unauthorized")

    guarded = [Depends(auth)]

    @app.get("/health", dependencies=guarded)
    def health():
        term = mt5.terminal_info()
        acc = mt5.account_info()
        return {"connected": bool(term and term.connected and acc), "trade_allowed": bool(term and term.trade_allowed),
                "account_currency": acc.currency if acc else None, "server": acc.server if acc else None,
                "time": datetime.now(timezone.utc).isoformat()}

    @app.get("/symbols/{symbol}", dependencies=guarded)
    def symbol(symbol: str):
        with bridge.lock:
            info = bridge.symbol(symbol)
            bridge.check_currency(info)
            contract = Decimal(str(info.trade_contract_size))
            return {"qty_step": str(Decimal(str(info.volume_step)) * contract),
                    "min_qty": str(Decimal(str(info.volume_min)) * contract),
                    "tick_size": str(info.trade_tick_size or info.point), "digits": info.digits,
                    "contract_size": str(contract), "profit_currency": info.currency_profit}

    @app.get("/tick/{symbol}", dependencies=guarded)
    def tick(symbol: str):
        with bridge.lock:
            bridge.symbol(symbol)
            t = mt5.symbol_info_tick(symbol)
            if t is None:
                raise HTTPException(503, f"no tick for {symbol}")
            return {"bid": str(t.bid), "ask": str(t.ask), "time_ms": int(t.time_msc)}

    @app.get("/rates/{symbol}", dependencies=guarded)
    def rates(symbol: str, interval: str, limit: int = 200):
        if interval not in TIMEFRAMES or not 1 <= limit <= 5000:
            raise HTTPException(400, "unsupported interval or limit")
        with bridge.lock:
            bridge.symbol(symbol)
            rows = mt5.copy_rates_from_pos(symbol, getattr(mt5, TIMEFRAMES[interval]), 0, limit)
        if rows is None or len(rows) == 0:
            raise HTTPException(503, f"no rates: {mt5.last_error()}")
        ms = INTERVAL_MS[interval]
        return [{"open_time_ms": int(r["time"]) * 1000, "close_time_ms": int(r["time"]) * 1000 + ms, "open": float(r["open"]),
                 "high": float(r["high"]), "low": float(r["low"]), "close": float(r["close"]),
                 "volume": float(r["tick_volume"])} for r in rows]

    @app.get("/book/{symbol}", dependencies=guarded)
    def book(symbol: str):
        with bridge.lock:
            info = bridge.symbol(symbol)
            mt5.market_book_add(symbol)
            levels = mt5.market_book_get(symbol) or ()
        contract = Decimal(str(info.trade_contract_size))
        bids = [(str(lv.price), str(Decimal(str(lv.volume_dbl or lv.volume)) * contract)) for lv in levels
                if lv.type in (mt5.BOOK_TYPE_BUY, mt5.BOOK_TYPE_BUY_MARKET)]
        asks = [(str(lv.price), str(Decimal(str(lv.volume_dbl or lv.volume)) * contract)) for lv in levels
                if lv.type in (mt5.BOOK_TYPE_SELL, mt5.BOOK_TYPE_SELL_MARKET)]
        if not bids or not asks:
            raise HTTPException(404, f"broker publishes no depth of market for {symbol}")
        return {"bids": sorted(bids, key=lambda x: -Decimal(x[0])), "asks": sorted(asks, key=lambda x: Decimal(x[0]))}

    @app.post("/orders", dependencies=guarded)
    def orders(o: OrderIn):
        if o.side not in ("BUY", "SELL") or not o.client_id or len(o.client_id) > 64:
            raise HTTPException(400, "bad order")
        return bridge.order(o)

    @app.get("/orders/{client_id}", dependencies=guarded)
    def order_status(client_id: str, symbol: str):
        res = bridge.order_status(client_id, symbol)
        if res is None:
            raise HTTPException(404, "unknown order")
        return res

    @app.post("/protection", dependencies=guarded)
    def set_protection(s: StopIn):
        return bridge.set_stop(s.symbol, float(s.stop_price))

    @app.delete("/protection/{symbol}", dependencies=guarded)
    def clear_protection(symbol: str):
        with bridge.lock:
            for p in bridge.own_positions(symbol):
                mt5.order_send({"action": mt5.TRADE_ACTION_SLTP, "symbol": symbol, "position": p.ticket, "sl": 0.0,
                                "tp": p.tp, "magic": bridge.magic})
        return {"cleared": True}

    @app.get("/protection/{symbol}", dependencies=guarded)
    def get_protection(symbol: str):
        return bridge.protection(symbol)

    @app.get("/positions/{symbol}", dependencies=guarded)
    def position(symbol: str):
        res = bridge.position(symbol)
        if res is None:
            raise HTTPException(404, "no position")
        return res

    @app.get("/balance", dependencies=guarded)
    def balance():
        with bridge.lock:
            acc = bridge.account()
        return {"currency": acc.currency, "free_margin": str(acc.margin_free), "balance": str(acc.balance),
                "equity": str(acc.equity)}

    @app.get("/fills", dependencies=guarded)
    def fills(symbol: str, start_ms: int):
        return bridge.fills(symbol, start_ms)

    app.state.bridge = bridge
    app.state.started = time.time()
    return app


if __name__ == "__main__":  # pragma: no cover - Windows only
    import uvicorn

    uvicorn.run(create_app(), host=os.environ.get("MT5_BRIDGE_HOST", "127.0.0.1"),
                port=int(os.environ.get("MT5_BRIDGE_PORT", "9100")))
