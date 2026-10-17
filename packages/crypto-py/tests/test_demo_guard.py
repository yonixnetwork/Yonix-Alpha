"""Demo-only enforcement of the strategy engine: no request other than
public market data can be built by it, no strategy mode other than DEMO
executes, and credentials / signing exist only in yonixalpha_crypto.private
(tested in test_private_accounts.py)."""

import re
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select

from yonixalpha_crypto import config_store, demo, registry
from yonixalpha_crypto.db import DemoOrder, DemoPosition
from yonixalpha_crypto.guard import LiveTradingNotAvailable, require_demo
from yonixalpha_crypto.publichttp import PublicHttp, RealOrderBlocked, check_request

from tests.conftest import NOW, book, spec

PKG = Path(__file__).resolve().parents[1] / "yonixalpha_crypto"


@pytest.mark.parametrize("exchange,method,path,params,headers", [
    ("bybit", "POST", "/v5/order/create", None, None),
    ("bybit", "GET", "/v5/order/realtime", None, None),
    ("bybit", "GET", "/v5/account/wallet-balance", None, None),
    ("binance", "POST", "/api/v3/order", None, None),
    ("binance", "GET", "/api/v3/order", None, None),
    ("binance", "GET", "/api/v3/account", None, None),
    ("binance", "DELETE", "/api/v3/openOrders", None, None),
    ("binance_us", "GET", "/api/v3/klines", {"symbol": "BTCUSDT", "signature": "x"}, None),
    ("binance", "GET", "/api/v3/klines", {"symbol": "BTCUSDT"}, {"X-MBX-APIKEY": "k"}),
    ("bybit", "GET", "/v5/market/kline", None, {"X-BAPI-API-KEY": "k"}),
    ("bybit", "GET", "/v5/market/kline", None, {"Authorization": "Bearer k"}),
    ("hyperliquid", "GET", "/info", None, None),
])
def test_anything_but_public_market_data_is_refused(exchange, method, path, params, headers):
    with pytest.raises(RealOrderBlocked):
        check_request(exchange, method, path, params, headers)


def test_public_market_data_is_allowed():
    check_request("bybit", "GET", "/v5/market/kline", {"category": "spot"}, None)
    check_request("binance", "GET", "/api/v3/ticker/bookTicker", {"symbol": "BTCUSDT"}, None)


async def test_a_blocked_request_never_reaches_the_network():
    sent = []

    def handler(request):
        sent.append(request)
        return httpx.Response(200, json={})

    http = PublicHttp("binance", "https://api.binance.com", transport=httpx.MockTransport(handler), min_interval_s=0)
    with pytest.raises(RealOrderBlocked):
        await http.get("/api/v3/order", {"symbol": "BTCUSDT", "side": "BUY"})
    await http.get("/api/v3/ping")
    await http.aclose()
    assert [r.url.path for r in sent] == ["/api/v3/ping"]
    assert all(r.method == "GET" for r in sent)


@pytest.mark.parametrize("mode", ["LIVE", "live", "REAL", "", "DISABLED"])
def test_only_demo_executes(mode):
    with pytest.raises(LiveTradingNotAvailable):
        require_demo(mode)
    require_demo("DEMO")


async def test_the_executor_refuses_a_non_demo_mode_before_writing_anything(sf):
    async with sf() as s:
        with pytest.raises(LiveTradingNotAvailable):
            await demo.open_long(s, mode="LIVE", strategy_id="x", client_id="c1", book=book("100", "100.1"),
                                 spec=spec(), qty=Decimal("0.1"), fee_bps=Decimal(10), slippage_bps=Decimal(5),
                                 stop_price=None, target_price=None, top_size=None, now=NOW)
        await s.rollback()
        assert await s.scalar(select(func.count()).select_from(DemoOrder)) == 0
        assert await s.scalar(select(func.count()).select_from(DemoPosition)) == 0


def test_configuration_cannot_select_a_live_mode():
    spec_ = registry.get("meta_muse")
    current = {"enabled": False, "mode": "DISABLED", "exchange": "bybit", "symbols": [], "interval": "5m",
               "params": {}, "risk": {}}
    for mode in ("LIVE", "REAL", "PAPER_LIVE"):
        with pytest.raises(config_store.ConfigError, match="no live mode"):
            config_store.validate(spec_, current, {"mode": mode})
    with pytest.raises(config_store.ConfigError, match="unknown keys"):
        config_store.validate(spec_, current, {"api_key": "x"})


def test_only_the_private_subpackage_holds_credentials_or_signs():
    """Outside yonixalpha_crypto/private nothing may sign, hold a key or
    build an HTTP client other than PublicHttp; nothing anywhere imports
    the Memecoin package or a wallet / exchange SDK."""
    src = {p: p.read_text() for p in PKG.rglob("*.py")}
    private = PKG / "private"
    public = {p: t for p, t in src.items() if private not in p.parents}
    imports = r"^\s*(?:from|import)\s+(?:hmac|hashlib|cryptography|eth_account|solders|ccxt|nacl|yonixalpha_core)\b"
    secrets = r"API_SECRET|API_KEY|PRIVATE_KEY|SECRET_KEY|api_secret|secretstore|signedhttp"
    hits = [(p.name, m.group(0)) for p, t in public.items() for m in re.finditer(imports, t, re.M)]
    hits += [(p.name, m.group(0)) for p, t in public.items() for m in re.finditer(secrets, t)]
    assert hits == []
    everywhere = r"^\s*(?:from|import)\s+(?:eth_account|solders|ccxt|nacl|yonixalpha_core|pybit|binance)\b"
    assert [p.name for p, t in src.items() if re.search(everywhere, t, re.M)] == []
    # HTTP clients: PublicHttp (public data) and SignedHttp (accounts), nothing else
    assert sorted(p.name for p, t in src.items() if "httpx.AsyncClient(" in t) == ["publichttp.py", "signedhttp.py"]
    # inside private, only signedhttp signs and only secretstore fingerprints
    signers = sorted(p.name for p, t in src.items() if private in p.parents and re.search(r"^import hmac", t, re.M))
    assert signers == ["secretstore.py", "signedhttp.py"]
