"""An in-memory stand-in for the MetaTrader5 package (same function names,
constants and result fields as the official API), for testing the bridge
on Linux. It fills market deals at the current tick and keeps positions."""

from datetime import datetime, timezone
from types import SimpleNamespace as NS

TRADE_ACTION_DEAL, TRADE_ACTION_SLTP = 1, 6
ORDER_TYPE_BUY, ORDER_TYPE_SELL = 0, 1
POSITION_TYPE_BUY, POSITION_TYPE_SELL = 0, 1
DEAL_TYPE_BUY, DEAL_TYPE_SELL = 0, 1
ORDER_FILLING_FOK, ORDER_FILLING_IOC, ORDER_FILLING_RETURN = 0, 1, 2
ORDER_TIME_GTC = 0
TRADE_RETCODE_DONE, TRADE_RETCODE_DONE_PARTIAL, TRADE_RETCODE_INVALID_STOPS = 10009, 10010, 10016
BOOK_TYPE_SELL, BOOK_TYPE_BUY, BOOK_TYPE_SELL_MARKET, BOOK_TYPE_BUY_MARKET = 1, 2, 3, 4
TIMEFRAME_M1, TIMEFRAME_M5, TIMEFRAME_M15, TIMEFRAME_M30 = 1, 5, 15, 30
TIMEFRAME_H1, TIMEFRAME_H4, TIMEFRAME_D1 = 16385, 16388, 16408

state = {}


def reset(currency="USD", book=True):
    state.clear()
    state.update(positions=[], deals=[], sent=[], ticket=1000, book=book, currency=currency, reject_sl=False,
                 symbols={"EURUSD": NS(name="EURUSD", visible=True, volume_min=0.01, volume_step=0.01,
                                       trade_contract_size=100000.0, trade_tick_size=0.00001, point=0.00001, digits=5,
                                       currency_profit="USD", filling_mode=3),
                          "USDJPY": NS(name="USDJPY", visible=True, volume_min=0.01, volume_step=0.01,
                                       trade_contract_size=100000.0, trade_tick_size=0.001, point=0.001, digits=3,
                                       currency_profit="JPY", filling_mode=2)},
                 tick=NS(bid=1.08000, ask=1.08002, time_msc=1_700_000_000_000))


def initialize(**kw):
    return True


def login(login, password="", server=""):
    state["login"] = (login, server)
    return True


def shutdown():
    return True


def last_error():
    return (1, "fake")


def account_info():
    return NS(currency=state["currency"], balance=10000.0, equity=10050.0, margin_free=9500.0, server="Demo-Server")


def terminal_info():
    return NS(connected=True, trade_allowed=True)


def symbol_info(symbol):
    return state["symbols"].get(symbol)


def symbol_select(symbol, enable):
    return True


def symbol_info_tick(symbol):
    return state["tick"]


def positions_get(symbol=None):
    return tuple(p for p in state["positions"] if symbol is None or p.symbol == symbol)


def _ticket():
    state["ticket"] += 1
    return state["ticket"]


def order_send(req):
    state["sent"].append(req)
    if req["action"] == TRADE_ACTION_SLTP:
        if state["reject_sl"]:
            return NS(retcode=TRADE_RETCODE_INVALID_STOPS, comment="Invalid stops")
        for p in state["positions"]:
            if p.ticket == req["position"]:
                p.sl = req["sl"]
        return NS(retcode=TRADE_RETCODE_DONE, comment="done")
    order, deal = _ticket(), _ticket()
    buy = req["type"] == ORDER_TYPE_BUY
    price = state["tick"].ask if buy else state["tick"].bid
    vol = req["volume"]
    if "position" in req:
        pos = next(p for p in state["positions"] if p.ticket == req["position"])
        profit = (price - pos.price_open) * vol * 100000 * (1 if pos.type == POSITION_TYPE_BUY else -1)
        pos.volume = round(pos.volume - vol, 2)
        if pos.volume <= 0:
            state["positions"].remove(pos)
    else:
        profit = 0.0
        state["positions"].append(NS(ticket=order, symbol=req["symbol"], type=POSITION_TYPE_BUY if buy else POSITION_TYPE_SELL,
                                     magic=req["magic"], volume=vol, price_open=price, sl=0.0, tp=0.0,
                                     price_current=price, profit=0.0, comment=req["comment"]))
    state["deals"].append(NS(ticket=deal, order=order, time_msc=1_700_000_000_000 + deal, type=DEAL_TYPE_BUY if buy else DEAL_TYPE_SELL,
                             magic=req["magic"], volume=vol, price=price, commission=round(-3.5 * vol, 2), fee=0.0, swap=0.0,
                             profit=profit, symbol=req["symbol"], comment=req["comment"]))
    return NS(retcode=TRADE_RETCODE_DONE, deal=deal, order=order, volume=vol, price=price, comment="done")


def history_deals_get(date_from, date_to, **kw):
    return tuple(state["deals"])


def copy_rates_from_pos(symbol, timeframe, start, count):
    t0 = int(datetime(2026, 9, 25, tzinfo=timezone.utc).timestamp())
    return [{"time": t0 + i * 900, "open": 1.08, "high": 1.081, "low": 1.079, "close": 1.0805, "tick_volume": 100}
            for i in range(count)]


def market_book_add(symbol):
    return True


def market_book_get(symbol):
    if not state["book"]:
        return ()
    return (NS(type=BOOK_TYPE_SELL, price=1.08003, volume=5, volume_dbl=5.0),
            NS(type=BOOK_TYPE_SELL, price=1.08002, volume=2, volume_dbl=2.0),
            NS(type=BOOK_TYPE_BUY, price=1.08000, volume=3, volume_dbl=3.0))
