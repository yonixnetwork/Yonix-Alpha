"""Active, read-only connection tests for every configured provider, for the
dashboard's TEST CONNECTION buttons. Each test makes one real request with
the server's current configuration and classifies the outcome:

  CONNECTED               the provider answered correctly (with auth where used)
  AUTHENTICATION FAILED   the provider rejected the key/token/signature
  TIMEOUT                 no answer within the time limit
  RATE LIMITED            HTTP 429 / the provider's rate-limit error
  INVALID CONFIGURATION   required settings missing or malformed
  UNAVAILABLE             network error, provider error, unexpected answer

Nothing is marked CONNECTED without a successful answer. Tests only read:
balances, account state, a quote, getSlot, a WebSocket subscription,
Telegram getMe. No order, transaction or message is ever sent. Secrets never
appear in results: messages are passed through redaction.
"""

import asyncio
import json
import time
from decimal import Decimal
from typing import Any, Awaitable, Callable

import httpx

from yonixalpha_core.redact import redact_text, redact_url

CONNECTED = "CONNECTED"
AUTH_FAILED = "AUTHENTICATION FAILED"
TIMEOUT = "TIMEOUT"
RATE_LIMITED = "RATE LIMITED"
INVALID = "INVALID CONFIGURATION"
UNAVAILABLE = "UNAVAILABLE"
RESULTS = [CONNECTED, AUTH_FAILED, TIMEOUT, RATE_LIMITED, INVALID, UNAVAILABLE]
TEST_TIMEOUT_SECONDS = 10.0

# Exchange error codes meaning "your credentials were refused".


class _Result(Exception):
    def __init__(self, status: str, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


def _secrets(settings: Any) -> list[str | None]:
    names = ["HELIUS_API_KEY", "SOLANA_RPC_URL", "SOLANA_WS_URL", "SOLANA_RPC_BACKUP_URL", "SOLANA_RPC_BACKUP_URL_2",
             "SOLANA_RPC_BACKUP_URL_3", "SOLANA_WS_BACKUP_URL", "JUPITER_API_KEY", "PUMPPORTAL_API_KEY", "TELEGRAM_BOT_TOKEN",
             "WALLET_PRIVATE_KEY", "EVM_WALLET_PRIVATE_KEY"]
    out = []
    for n in names:
        v = getattr(settings, n, None)
        out.append(v.get_secret_value() if hasattr(v, "get_secret_value") else v)
    for n in ("BSC_RPC_URLS", "ROBINHOOD_RPC_URLS"):  # comma-separated URL lists, each may embed a key
        out += [u.strip() for u in (getattr(settings, n, None) or "").split(",") if u.strip()]
    return out


def _redact(text: str, settings: Any) -> str:
    """Every configured secret value is removed from `text`: URLs down to
    scheme://host, anything else replaced outright."""
    for value in sorted((v for v in _secrets(settings) if v and len(str(v)) >= 6), key=lambda v: len(str(v)), reverse=True):
        v = str(value)
        text = text.replace(v, redact_url(v) if "://" in v else "[redacted]")
    return text


def classify(exc: BaseException) -> tuple[str, str]:
    """Maps a failure to a result. Uses the HTTP status / exchange code the
    provider clients put on their errors; never guesses CONNECTED."""
    if isinstance(exc, _Result):
        return exc.status, exc.detail
    name = type(exc).__name__
    text = str(exc)
    if isinstance(exc, (asyncio.TimeoutError, httpx.TimeoutException)) or "Timeout" in text or "Timeout" in name:
        return TIMEOUT, f"no answer within {TEST_TIMEOUT_SECONDS:.0f}s"
    if "NotConfigured" in name or "not set" in text or "not configured" in text:
        return INVALID, text
    status = getattr(getattr(exc, "response", None), "status_code", None)
    for marker, s in (("HTTP 401", 401), ("HTTP 403", 403), ("HTTP 429", 429), ("status 401", 401), ("status 403", 403),
                      ("status 429", 429), ("HTTP 418", 429)):
        if marker in text:
            status = s
    if status == 429:
        return RATE_LIMITED, text
    if status in (401, 403):
        return AUTH_FAILED, text
    return UNAVAILABLE, f"{name}: {text}" if text else name


async def _run(name: str, settings: Any, fn: Callable[[], Awaitable[str]]) -> dict[str, Any]:
    started = time.monotonic()
    try:
        detail = await asyncio.wait_for(fn(), TEST_TIMEOUT_SECONDS)
        status = CONNECTED
    except BaseException as exc:  # noqa: BLE001 - every failure becomes a classified result
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        status, detail = classify(exc)
    return {"provider": name, "status": status, "detail": _redact(str(detail), settings)[:300],
            "latency_ms": round((time.monotonic() - started) * 1000), "tested_at": time.time()}


def _need(settings: Any, *names: str) -> None:
    missing = [n for n in names if not getattr(settings, n, None)]
    if missing:
        raise _Result(INVALID, f"not configured: {', '.join(missing)}")


def _secret(v):
    return v.get_secret_value() if hasattr(v, "get_secret_value") else v


async def _json_rpc(client: httpx.AsyncClient, url: str, method: str, params: list | None = None) -> Any:
    resp = await client.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []},
                             timeout=TEST_TIMEOUT_SECONDS)
    if resp.status_code == 429:
        raise _Result(RATE_LIMITED, "HTTP 429 Too Many Requests")
    if resp.status_code in (401, 403):
        raise _Result(AUTH_FAILED, f"HTTP {resp.status_code}: key refused")
    if resp.status_code >= 400:
        raise _Result(UNAVAILABLE, f"HTTP {resp.status_code}")
    body = resp.json()
    if "error" in body:
        err = body["error"]
        msg = str(err.get("message", err))[:200] if isinstance(err, dict) else str(err)[:200]
        if "rate" in msg.lower() or "429" in msg:
            raise _Result(RATE_LIMITED, msg)
        if "api key" in msg.lower() or "unauthor" in msg.lower():
            raise _Result(AUTH_FAILED, msg)
        raise _Result(UNAVAILABLE, msg)
    return body.get("result")


async def _ws_probe(url: str, message: dict | None, what: str) -> str:
    import websockets

    try:
        async with websockets.connect(url, open_timeout=TEST_TIMEOUT_SECONDS, close_timeout=2) as ws:
            if message is None:
                return f"{what}: connection accepted"
            await ws.send(json.dumps(message))
            try:
                raw = await asyncio.wait_for(ws.recv(), 5)
            except asyncio.TimeoutError:
                return f"{what}: connection accepted, subscription sent (no message within 5s)"
            try:
                body = json.loads(raw)
            except ValueError:
                return f"{what}: connected, first message received"
            if isinstance(body, dict) and "error" in body:
                raise _Result(UNAVAILABLE, f"{what}: {str(body['error'])[:200]}")
            return f"{what}: connected, subscription answered"
    except _Result:
        raise
    except Exception as exc:  # noqa: BLE001
        text = str(exc)
        if "429" in text:
            raise _Result(RATE_LIMITED, f"{what}: HTTP 429") from exc
        if "401" in text or "403" in text:
            raise _Result(AUTH_FAILED, f"{what}: key refused ({text[:80]})") from exc
        raise


async def test_provider(name: str, settings: Any, client: httpx.AsyncClient) -> dict[str, Any]:
    """Runs the named test. Unknown names are INVALID CONFIGURATION."""

    async def solana_rpc() -> str:
        _need(settings, "SOLANA_RPC_URL")
        slot = await _json_rpc(client, settings.SOLANA_RPC_URL, "getSlot")
        return f"getSlot={slot}"

    async def solana_rpc_backup() -> str:
        _need(settings, "SOLANA_RPC_BACKUP_URL")
        return f"getSlot={await _json_rpc(client, settings.SOLANA_RPC_BACKUP_URL, 'getSlot')}"

    def backup_n(key: str) -> Callable[[], Awaitable[str]]:
        async def test() -> str:
            _need(settings, key)
            return f"getSlot={await _json_rpc(client, getattr(settings, key), 'getSlot')}"
        return test

    async def solana_ws() -> str:
        _need(settings, "SOLANA_WS_URL")
        return await _ws_probe(settings.SOLANA_WS_URL, {"jsonrpc": "2.0", "id": 1, "method": "slotSubscribe"}, "slotSubscribe")

    async def helius() -> str:
        _need(settings, "HELIUS_API_KEY")
        url = f"https://mainnet.helius-rpc.com/?api-key={_secret(settings.HELIUS_API_KEY)}"
        return f"HELIUS_API_KEY accepted, getSlot={await _json_rpc(client, url, 'getSlot')}"

    async def jupiter() -> str:
        from yonixalpha_core.solana.market_data import JupiterClient, RateBudget

        jc = JupiterClient(client, _secret(getattr(settings, "JUPITER_API_KEY", None)), RateBudget(5))
        q = await jc.quote("So11111111111111111111111111111111111111112", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                           10_000_000, 100)
        if q.out_amount is None:
            err = q.error or q.status
            if "429" in err:
                raise _Result(RATE_LIMITED, err)
            if "401" in err or "403" in err:
                raise _Result(AUTH_FAILED, err)
            if "timeout" in err.lower():
                raise _Result(TIMEOUT, err)
            raise _Result(UNAVAILABLE, err)
        keyed = "keyed" if getattr(settings, "JUPITER_API_KEY", None) else "keyless (deprecated lite-api)"
        return f"{keyed}: 0.01 SOL -> {Decimal(q.out_amount) / Decimal(10**6)} USDC"

    async def pumpportal() -> str:
        from yonixalpha_core.solana.pumpportal_ws import URL

        key = _secret(getattr(settings, "PUMPPORTAL_API_KEY", None))
        url = f"{URL}?api-key={key}" if key else URL
        what = "data WebSocket (" + ("with key" if key else "free data, no key") + ")"
        return await _ws_probe(url, {"method": "subscribeNewToken"}, what)

    async def telegram() -> str:
        _need(settings, "TELEGRAM_BOT_TOKEN")
        resp = await client.get(f"https://api.telegram.org/bot{_secret(settings.TELEGRAM_BOT_TOKEN)}/getMe",
                                timeout=TEST_TIMEOUT_SECONDS)
        if resp.status_code in (401, 404):
            raise _Result(AUTH_FAILED, "bot token refused")
        if resp.status_code == 429:
            raise _Result(RATE_LIMITED, "HTTP 429")
        body = resp.json()
        if not body.get("ok"):
            raise _Result(UNAVAILABLE, str(body.get("description", body))[:200])
        chat = "chat id set" if getattr(settings, "TELEGRAM_CHAT_ID", None) else "TELEGRAM_CHAT_ID not set"
        return f"bot @{body['result'].get('username')} ({chat}; no message sent)"

    def evm_rpc(chain: str) -> Callable[[], Awaitable[str]]:
        async def test() -> str:
            from yonixalpha_core.chains.base import Chain
            from yonixalpha_core.chains.registry import CHAINS

            from yonixalpha_core.chains.registry import LAUNCHPADS

            spec = CHAINS[Chain(chain)]
            configured = [u.strip() for u in (getattr(settings, f"{chain.upper()}_RPC_URLS", None) or "").split(",") if u.strip()]
            urls = configured or list(spec.public_rpc)
            # Discovery reads launchpad events with eth_getLogs, which many public
            # nodes refuse while answering everything else (bsc-dataseed:
            # "limit exceeded"; publicnode: HTTP 403). An endpoint is only useful
            # for discovery if it serves logs, so that is tested too, over the
            # last 100 blocks of one launchpad contract (a small answer).
            probe = next((a for lp in LAUNCHPADS.values() if lp.chain == spec.chain for a in lp.contracts.values()), None)
            results, ok, logs_ok = [], 0, 0
            for url in urls:
                try:
                    cid = int(await _json_rpc(client, url, "eth_chainId"), 16)
                    if cid != spec.evm_chain_id:
                        raise _Result(INVALID, f"{redact_url(url)} answers for chain {cid}, expected {spec.evm_chain_id}")
                    head = int(await _json_rpc(client, url, "eth_blockNumber"), 16)
                    ok += 1
                    logs = "logs not tested"
                    if probe:
                        try:
                            await _json_rpc(client, url, "eth_getLogs", [{"address": probe, "fromBlock": hex(max(0, head - 99)),
                                                                          "toBlock": hex(head)}])
                            logs, logs_ok = "logs OK", logs_ok + 1
                        except _Result as exc:
                            logs = f"logs REFUSED ({exc.status}: {redact_text(str(exc.detail), [url])[:80]})"
                    results.append(f"{redact_url(url)} block {head}, {logs}")
                except _Result as exc:
                    if exc.status == INVALID:
                        raise
                    results.append(f"{redact_url(url)} {exc.status}")
            if not ok:
                raise _Result(UNAVAILABLE, "; ".join(results))
            if probe and not logs_ok:
                raise _Result(UNAVAILABLE, "no endpoint serves eth_getLogs, so launchpad discovery cannot run; add an RPC "
                                           "that serves logs: " + "; ".join(results))
            where = f"{len(configured)} configured" if configured else "none configured: tested the public endpoint(s)"
            return f"chain {spec.evm_chain_id} ({where}): " + "; ".join(results)
        return test

    async def honeypot_is() -> str:
        from yonixalpha_core.chains.evm.safety import honeypot_is as hp
        from yonixalpha_core.chains.registry import BSC_WBNB

        r = await hp(BSC_WBNB, client)
        if not r.get("available"):
            raise _Result(UNAVAILABLE, f"Honeypot.is did not answer: {r.get('detail')}")
        return "answered (WBNB probe); enrichment only, never the sole safety check"

    tests: dict[str, Callable[[], Awaitable[str]]] = {
        "bsc_rpc": evm_rpc("bsc"), "robinhood_rpc": evm_rpc("robinhood"), "honeypot_is": honeypot_is,
        "solana_rpc": solana_rpc, "solana_rpc_backup": solana_rpc_backup,
        "solana_rpc_backup_2": backup_n("SOLANA_RPC_BACKUP_URL_2"), "solana_rpc_backup_3": backup_n("SOLANA_RPC_BACKUP_URL_3"),
        "solana_ws": solana_ws, "helius": helius,
        "jupiter": jupiter, "pumpportal": pumpportal, "telegram": telegram,
    }
    fn = tests.get(name)
    if fn is None:
        return {"provider": name, "status": INVALID, "detail": f"unknown provider; one of {sorted(tests)}", "latency_ms": 0,
                "tested_at": time.time()}
    return await _run(name, settings, fn)


PROVIDERS = ["bsc_rpc", "robinhood_rpc", "honeypot_is", "solana_rpc", "solana_rpc_backup", "solana_rpc_backup_2", "solana_rpc_backup_3", "solana_ws", "helius", "jupiter", "pumpportal", "telegram"]
