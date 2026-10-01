"""Real-time EVM transaction streams (master upgrade §9, §13).

Robinhood Chain sequencer feed (§13). Robinhood Chain is an Arbitrum Orbit
chain; its sequencer publishes every sequenced message on a public
WebSocket feed (wss://feed.mainnet.chain.robinhood.com, a delayed copy at
wss://delayed-feed.mainnet.chain.robinhood.com) in Nitro's broadcast format:

  {"version": 1, "messages": [{"sequenceNumber": N, "message": {"message":
     {"header": {"kind": 3, "timestamp": t, ...}, "l2Msg": base64}, ...}}],
   "confirmedSequenceNumberMessage": {...}}

A header of kind 3 carries an L2 message: kind 4 = one signed transaction,
kind 3 = a batch of length-prefixed (uint64 big endian) nested messages,
kind 7 = a compressed transaction (counted, not decoded). Each signed
transaction is decoded (hash, to, value, selector); its sender is recovered
only for transactions to a watched contract, plus a budget of others per
second (pure-Python recovery costs ~7 ms). A transaction from a copy target,
or to a launchpad contract, is recorded in Redis for 15 minutes so later
stages can see how much earlier the stream knew about it.

Measured: connection state, reconnects, messages, transactions, sequence
gaps (and how many messages were missing), duplicates, out-of-order
messages, the feed delay (now minus the message timestamp, 1 s resolution)
and matches. On reconnect the client asks for the next sequence number
(Arbitrum-Requested-Sequence-Number) so a short drop loses nothing. After
`fallback_after_failures` failed connections to the primary feed it uses
the delayed feed and retries the primary every `primary_retry_minutes`;
the delayed feed is labelled as such and never treated as primary-speed.

BSC pending transactions (§9). eth_subscribe newPendingTransactions with
full transaction bodies over a WSS endpoint configured on RPC / Data
Providers. A provider that only streams hashes (LIMITED) or refuses the
subscription (REFUSED) is reported, and copy trading keeps using confirmed
trades. bsc-mempool (github.com/1chimaruGin/bsc-mempool) reads full pending
bodies from its own bsc-geth node over IPC; this server cannot run one.

Nothing here trades: a sequenced or pending transaction can still revert, so
decisions stay on confirmed trades and these streams only measure and
record.
"""

from __future__ import annotations

import asyncio
import base64
import json
import statistics
import time
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

import rlp
from eth_utils import keccak

from yonixalpha_core.logging import get_logger
from yonixalpha_core.redact import redact_url

log = get_logger("core.evm_streams")

SETTINGS_KEY = "evm_streams"
ROBINHOOD_FEED = "wss://feed.mainnet.chain.robinhood.com"
ROBINHOOD_DELAYED_FEED = "wss://delayed-feed.mainnet.chain.robinhood.com"
ROBINHOOD_CHAIN_ID = 4663
SEEN_TTL = 900
# Contracts outside the launchpad registry whose transactions are launches /
# trades: the official Pons launchAndBuy router (pons-launch-engine ABI).
EXTRA_CONTRACTS = {"robinhood": {"0xe33e9e479df8802cb0866d5d05258bec4cf62948"}}
CONNECTING, CONNECTED, RECONNECTING, NOT_CONFIGURED, DISABLED, REFUSED, LIMITED, WRONG_CHAIN = (
    "CONNECTING", "CONNECTED", "RECONNECTING", "NOT_CONFIGURED", "DISABLED", "REFUSED", "LIMITED", "WRONG_CHAIN")
L1_L2_MESSAGE, L2_BATCH, L2_SIGNED_TX, L2_SIGNED_COMPRESSED = 3, 3, 4, 7
MAX_DEPTH = 16


@dataclass(frozen=True)
class StreamConfig:
    robinhood_feed_enabled: bool = True
    robinhood_feed_url: str = ROBINHOOD_FEED
    robinhood_delayed_url: str = ROBINHOOD_DELAYED_FEED
    fallback_after_failures: int = 3
    primary_retry_minutes: int = 10
    bsc_pending_enabled: bool = True
    recover_budget_per_s: int = 30

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_config(data: dict | None) -> tuple[StreamConfig, list[str]]:
    base, errors, values = StreamConfig(), [], {}
    types = {f.name: f.type for f in fields(StreamConfig)}
    for k, v in (data or {}).items():
        if k not in types:
            errors.append(f"unknown setting {k}")
        elif types[k] in ("bool", bool):
            if not isinstance(v, bool):
                errors.append(f"{k}: true or false")
            else:
                values[k] = v
        elif types[k] in ("str", str):
            if not (isinstance(v, str) and v.startswith("wss://") and len(v) < 300):
                errors.append(f"{k}: a wss:// URL")
            else:
                values[k] = v
        else:
            try:
                n = int(v)
            except (TypeError, ValueError):
                errors.append(f"{k}: a whole number")
                continue
            if n < 1:
                errors.append(f"{k}: at least 1")
            else:
                values[k] = n
    return StreamConfig(**{**asdict(base), **values}), errors


async def load_config(session) -> StreamConfig:
    from yonixalpha_core.db.models import PlatformSetting

    row = await session.get(PlatformSetting, SETTINGS_KEY)
    cfg, errors = parse_config(dict(row.value) if row else None)
    return StreamConfig() if errors else cfg


# --- decoding ------------------------------------------------------------------------------------------

def _int(b: bytes) -> int:
    return int.from_bytes(b, "big") if b else 0


def decode_signed_tx(raw: bytes) -> dict[str, Any] | None:
    """hash, type, to, value, selector, nonce of a signed transaction (legacy
    or typed envelope). None when it does not decode."""
    try:
        if raw and raw[0] <= 0x7F:
            kind, fields_ = raw[0], rlp.decode(raw[1:])
            to_i, val_i, data_i, nonce_i = {1: (4, 5, 6, 1), 2: (5, 6, 7, 1), 4: (5, 6, 7, 1)}.get(kind, (None,) * 4)
            if to_i is None:
                return {"hash": "0x" + keccak(raw).hex(), "type": kind, "to": None, "value": 0, "selector": None,
                        "nonce": None, "undecoded": True}
        else:
            kind, fields_ = 0, rlp.decode(raw)
            to_i, val_i, data_i, nonce_i = 3, 4, 5, 0
        to = fields_[to_i]
        data = fields_[data_i]
        return {"hash": "0x" + keccak(raw).hex(), "type": kind, "to": ("0x" + to.hex()) if to else None,
                "value": _int(fields_[val_i]), "selector": ("0x" + data[:4].hex()) if len(data) >= 4 else None,
                "nonce": _int(fields_[nonce_i])}
    except Exception:  # noqa: BLE001 - a malformed transaction is counted, never fatal
        return None


def recover_sender(raw: bytes) -> str | None:
    from eth_account import Account

    try:
        return Account.recover_transaction(raw).lower()
    except Exception:  # noqa: BLE001
        return None


def l2_transactions(l2msg: bytes, depth: int = 0) -> tuple[list[bytes], int]:
    """Signed transactions in an L2 message (batches unrolled) and how many
    compressed ones were skipped."""
    if not l2msg or depth > MAX_DEPTH:
        return [], 0
    kind, body = l2msg[0], l2msg[1:]
    if kind == L2_SIGNED_TX:
        return [body], 0
    if kind == L2_SIGNED_COMPRESSED:
        return [], 1
    if kind != L2_BATCH:
        return [], 0
    out: list[bytes] = []
    skipped, i = 0, 0
    while i + 8 <= len(body):
        n = int.from_bytes(body[i:i + 8], "big")
        i += 8
        if n <= 0 or i + n > len(body):
            break
        txs, sk = l2_transactions(body[i:i + n], depth + 1)
        out += txs
        skipped += sk
        i += n
    return out, skipped


def parse_broadcast(payload: str | bytes) -> list[dict[str, Any]]:
    """Feed messages of one broadcast: sequence number, timestamp, raw txs."""
    data = json.loads(payload)
    out = []
    for m in data.get("messages") or []:
        inner = ((m.get("message") or {}).get("message")) or {}
        header = inner.get("header") or {}
        txs: list[bytes] = []
        skipped = 0
        if header.get("kind") == L1_L2_MESSAGE and inner.get("l2Msg"):
            txs, skipped = l2_transactions(base64.b64decode(inner["l2Msg"]))
        out.append({"seq": int(m.get("sequenceNumber")), "timestamp": header.get("timestamp"), "txs": txs,
                    "compressed_skipped": skipped, "kind": header.get("kind")})
    return out


# --- stats ---------------------------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class StreamStats:
    chain: str
    source: str
    state: str = CONNECTING
    url: str | None = None
    fallback_active: bool = False
    connected_at: str | None = None
    reconnects: int = 0
    connect_failures: int = 0
    failures_in_row: int = 0  # since the last successful connection
    messages: int = 0
    transactions: int = 0
    undecoded: int = 0
    compressed_skipped: int = 0
    senders_recovered: int = 0
    senders_skipped: int = 0
    matched: int = 0
    matched_copy_targets: int = 0
    matched_launchpads: int = 0
    last_seq: int | None = None
    gaps: int = 0
    missing_messages: int = 0
    duplicates: int = 0
    out_of_order: int = 0
    last_message_at: str | None = None
    last_error: str | None = None
    detail: str | None = None
    delays: list = field(default_factory=list)  # feed delay samples (s), last 200

    def sequence(self, seq: int) -> bool:
        """Tracks one sequence number; False for a duplicate (skip it)."""
        if self.last_seq is None or seq == self.last_seq + 1:
            self.last_seq = seq
            return True
        if seq > self.last_seq + 1:
            self.gaps += 1
            self.missing_messages += seq - self.last_seq - 1
            self.last_seq = seq
            return True
        if seq == self.last_seq:
            self.duplicates += 1
            return False
        self.out_of_order += 1  # older than the last one: replayed or reordered
        self.duplicates += 1
        return False

    def report(self) -> dict[str, Any]:
        d = {k: v for k, v in asdict(self).items() if k != "delays"}
        d["delay_s_median"] = statistics.median(self.delays) if self.delays else None
        d["delay_s_p95"] = sorted(self.delays)[int(len(self.delays) * 0.95) - 1] if len(self.delays) >= 20 else None
        d["at"] = _now_iso()
        return d


class _Budget:
    def __init__(self, per_s: int) -> None:
        self.per_s, self.window, self.used = per_s, int(time.monotonic()), 0

    def take(self) -> bool:
        now = int(time.monotonic())
        if now != self.window:
            self.window, self.used = now, 0
        if self.used >= self.per_s:
            return False
        self.used += 1
        return True


Watch = Callable[[], Awaitable[tuple[set[str], set[str]]]]  # -> (copy target wallets, launchpad contracts), lower case


async def record_seen(redis, chain: str, rec: dict[str, Any]) -> None:
    if redis is not None:
        await redis.set(f"yx:evm:seen:{chain}:{rec['hash']}", json.dumps(rec), ex=SEEN_TTL)


async def seen(redis, chain: str, tx_hash: str | None) -> dict[str, Any] | None:
    if redis is None or not tx_hash:
        return None
    raw = await redis.get(f"yx:evm:seen:{chain}:{tx_hash.lower()}")
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


async def publish(redis, stats: StreamStats) -> None:
    if redis is None:
        return
    try:
        await redis.set(f"yx:evm:stream:{stats.chain}:{stats.source}", json.dumps(stats.report()), ex=180)
    except Exception as exc:  # noqa: BLE001 - the dashboard misses one update; the stream keeps running
        log.warning("evm_streams.publish_failed", chain=stats.chain, source=stats.source, error=type(exc).__name__)


SOURCES = ("sequencer_feed", "pending_tx")


async def reports(redis, chain: str) -> dict[str, dict[str, Any]]:
    """The last published report per source (absent: not running or older than 3 minutes)."""
    out: dict[str, dict[str, Any]] = {}
    for source in SOURCES:
        raw = await redis.get(f"yx:evm:stream:{chain}:{source}")
        try:
            if raw:
                out[source] = json.loads(raw)
        except ValueError:
            continue
    return out


# --- Robinhood sequencer feed --------------------------------------------------------------------------

class SequencerFeed:
    def __init__(self, chain: str, cfg: StreamConfig, watch: Watch, redis, *, connect=None,
                 chain_id: int = ROBINHOOD_CHAIN_ID, reload: Callable[[], Awaitable[StreamConfig]] | None = None) -> None:
        self.chain, self.cfg, self.watch, self.redis = chain, cfg, watch, redis
        self.reload = reload  # re-reads the settings before each connection and every minute
        self.chain_id = chain_id
        self.stats = StreamStats(chain, "sequencer_feed")
        self._connect = connect
        self._budget = _Budget(cfg.recover_budget_per_s)
        self._wallets: set[str] = set()
        self._contracts: set[str] = set()
        self._watch_at = 0.0
        self._primary_failures = 0
        self._fallback_until = 0.0
        self._reload_at = time.monotonic()

    def _url(self) -> str:
        now = time.monotonic()
        use_fallback = self._primary_failures >= self.cfg.fallback_after_failures and now < self._fallback_until
        self.stats.fallback_active = use_fallback
        return self.cfg.robinhood_delayed_url if use_fallback else self.cfg.robinhood_feed_url

    async def _refresh_watch(self) -> None:
        if time.monotonic() - self._watch_at >= 30:
            self._wallets, self._contracts = await self.watch()
            self._watch_at = time.monotonic()

    async def handle(self, payload: str | bytes, now: float | None = None) -> int:
        """One broadcast: sequence tracking, decoding, matching. Returns matches."""
        now = time.time() if now is None else now
        matches = 0
        await self._refresh_watch()
        for m in parse_broadcast(payload):
            if not self.stats.sequence(m["seq"]):
                continue
            self.stats.messages += 1
            self.stats.last_message_at = _now_iso()
            self.stats.compressed_skipped += m["compressed_skipped"]
            if m["timestamp"]:
                self.stats.delays = (self.stats.delays + [max(0.0, now - float(m["timestamp"]))])[-200:]
            for raw in m["txs"]:
                tx = decode_signed_tx(raw)
                self.stats.transactions += 1
                if tx is None or tx.get("undecoded"):
                    self.stats.undecoded += 1
                    continue
                to = (tx["to"] or "").lower()
                to_launchpad = to in self._contracts
                sender = None
                if to_launchpad or self._budget.take():
                    sender = recover_sender(raw)
                    self.stats.senders_recovered += 1
                else:
                    self.stats.senders_skipped += 1
                by_target = sender is not None and sender in self._wallets
                if not (by_target or to_launchpad):
                    continue
                matches += 1
                self.stats.matched += 1
                self.stats.matched_copy_targets += int(by_target)
                self.stats.matched_launchpads += int(to_launchpad)
                await record_seen(self.redis, self.chain, {
                    **tx, "from": sender, "seq": m["seq"], "seen_at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
                    "source": "delayed_feed" if self.stats.fallback_active else "sequencer_feed",
                    "match": [x for x, ok in (("copy_target", by_target), ("launchpad", to_launchpad)) if ok]})
        return matches

    async def run(self, stop: asyncio.Event) -> None:
        import websockets

        connect = self._connect or websockets.connect
        backoff = 1.0
        last_publish = 0.0
        while not stop.is_set():
            if self.reload is not None:
                try:
                    self.cfg = await self.reload()
                    self._budget.per_s = self.cfg.recover_budget_per_s
                except Exception as exc:  # noqa: BLE001 - keep the last settings; never stop data-evm
                    self.stats.last_error = f"settings: {type(exc).__name__}: {str(exc)[:120]}"
            if not self.cfg.robinhood_feed_enabled:
                self.stats.state = DISABLED
                await publish(self.redis, self.stats)
                await _sleep(stop, 60)
                continue
            url = self._url()
            self.stats.url = redact_url(url)
            headers = {"Arbitrum-Feed-Client-Version": "2"}
            if self.stats.last_seq is not None:
                headers["Arbitrum-Requested-Sequence-Number"] = str(self.stats.last_seq + 1)
            try:
                self.stats.state = CONNECTING if self.stats.reconnects == 0 else RECONNECTING
                async with connect(url, additional_headers=headers, max_size=None, open_timeout=15,
                                   ping_interval=20, ping_timeout=20) as ws:
                    cid = _response_header(ws, "Arbitrum-Chain-Id")
                    if cid is not None and int(cid) != self.chain_id:
                        self.stats.state, self.stats.last_error = WRONG_CHAIN, f"feed is for chain {cid}"
                        raise ConnectionError(self.stats.last_error)
                    self.stats.state, self.stats.connected_at = CONNECTED, _now_iso()
                    self.stats.failures_in_row = 0
                    if url == self.cfg.robinhood_feed_url:
                        self._primary_failures = 0
                    backoff = 1.0
                    while not stop.is_set():
                        try:
                            payload = await asyncio.wait_for(ws.recv(), timeout=30)
                        except asyncio.TimeoutError:
                            continue  # quiet feed: pings keep the connection; the loop re-checks stop
                        await self.handle(payload)
                        if time.monotonic() - last_publish >= 5:
                            last_publish = time.monotonic()
                            await publish(self.redis, self.stats)
                        if self.stats.fallback_active and time.monotonic() >= self._fallback_until:
                            break  # time to try the primary feed again
                        if self.reload is not None and time.monotonic() - self._reload_at >= 60:
                            self._reload_at = time.monotonic()
                            try:
                                new = await self.reload()
                            except Exception:  # noqa: BLE001 - keep the current settings
                                new = self.cfg
                            changed = (new.robinhood_feed_enabled, new.robinhood_feed_url, new.robinhood_delayed_url) != \
                                (self.cfg.robinhood_feed_enabled, self.cfg.robinhood_feed_url, self.cfg.robinhood_delayed_url)
                            self.cfg = new
                            self._budget.per_s = new.recover_budget_per_s
                            if changed:
                                break  # reconnect with the new settings
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - every failure is counted and retried
                self.stats.last_error = f"{type(exc).__name__}: {str(exc)[:160]}"
                self.stats.connect_failures += 1
                self.stats.failures_in_row += 1
                if url == self.cfg.robinhood_feed_url:
                    self._primary_failures += 1
                    if self._primary_failures >= self.cfg.fallback_after_failures:
                        self._fallback_until = time.monotonic() + self.cfg.primary_retry_minutes * 60
                if self.stats.state != WRONG_CHAIN:
                    self.stats.state = RECONNECTING
            self.stats.reconnects += 1
            await publish(self.redis, self.stats)
            await _sleep(stop, backoff)
            backoff = min(60.0, backoff * 2)


def _response_header(ws, name: str) -> str | None:
    try:
        return ws.response.headers.get(name)
    except AttributeError:
        return None


async def _sleep(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


# --- pending transactions (BSC or any EVM WSS) ----------------------------------------------------------

class PendingTxStream:
    """eth_subscribe newPendingTransactions (full bodies) over a provider WSS."""

    def __init__(self, chain: str, urls: Callable[[], Awaitable[list[str]]], watch: Watch, redis, *,
                 enabled: Callable[[], Awaitable[bool]] | None = None, connect=None) -> None:
        self.chain, self.urls, self.watch, self.redis = chain, urls, watch, redis
        self.enabled = enabled
        self.stats = StreamStats(chain, "pending_tx")
        self._connect = connect
        self._wallets: set[str] = set()
        self._contracts: set[str] = set()
        self._watch_at = 0.0

    async def handle(self, msg: dict[str, Any], now: float | None = None) -> int:
        now = time.time() if now is None else now
        if time.monotonic() - self._watch_at >= 30:
            self._wallets, self._contracts = await self.watch()
            self._watch_at = time.monotonic()
        res = (msg.get("params") or {}).get("result")
        if res is None:
            return 0
        self.stats.messages += 1
        self.stats.last_message_at = _now_iso()
        if isinstance(res, str):
            return -1  # hash only: cannot match a sender without fetching every transaction
        self.stats.transactions += 1
        sender, to = (res.get("from") or "").lower(), (res.get("to") or "").lower()
        by_target, to_launchpad = sender in self._wallets, to in self._contracts
        if not (by_target or to_launchpad):
            return 0
        self.stats.matched += 1
        self.stats.matched_copy_targets += int(by_target)
        self.stats.matched_launchpads += int(to_launchpad)
        data = res.get("input") or "0x"
        await record_seen(self.redis, self.chain, {
            "hash": (res.get("hash") or "").lower(), "from": sender, "to": to, "value": int(res.get("value") or "0x0", 16),
            "selector": data[:10] if len(data) >= 10 else None, "seen_at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
            "source": "pending_tx", "match": [x for x, ok in (("copy_target", by_target), ("launchpad", to_launchpad)) if ok]})
        return 1

    async def run(self, stop: asyncio.Event) -> None:
        import websockets

        connect = self._connect or websockets.connect
        backoff = 2.0
        while not stop.is_set():
            try:
                enabled = self.enabled is None or await self.enabled()
                urls = await self.urls() if enabled else []
            except Exception as exc:  # noqa: BLE001 - a settings / database error never stops data-evm
                self.stats.last_error = f"settings: {type(exc).__name__}: {str(exc)[:120]}"
                await _sleep(stop, 60)
                continue
            if not enabled:
                self.stats.state, self.stats.detail = DISABLED, "switched off in the stream settings"
                await publish(self.redis, self.stats)
                await _sleep(stop, 60)
                continue
            if not urls:
                self.stats.state = NOT_CONFIGURED
                self.stats.detail = ("no WSS endpoint on RPC / Data Providers for this chain: copy trading uses "
                                     "confirmed trades")
                await publish(self.redis, self.stats)
                await _sleep(stop, 60)
                continue
            url = urls[0]
            self.stats.url = redact_url(url)
            retry_after = backoff
            try:
                self.stats.state = CONNECTING
                async with connect(url, max_size=None, open_timeout=15, ping_interval=20, ping_timeout=20) as ws:
                    await ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_subscribe",
                                              "params": ["newPendingTransactions", True]}))
                    ack = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
                    if ack.get("error"):
                        self.stats.state = REFUSED
                        self.stats.detail = ("the provider refuses pending-transaction subscriptions: "
                                             + str((ack["error"] or {}).get("message", ack["error"]))[:160])
                        retry_after = 1800
                        raise ConnectionError(self.stats.detail)
                    self.stats.state, self.stats.connected_at, self.stats.detail = CONNECTED, _now_iso(), None
                    self.stats.failures_in_row = 0
                    backoff = 2.0
                    last_publish = 0.0
                    while not stop.is_set():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=30)
                        except asyncio.TimeoutError:
                            continue
                        if await self.handle(json.loads(raw)) == -1:
                            self.stats.state = LIMITED
                            self.stats.detail = ("the provider streams transaction hashes only; matching copy targets "
                                                 "needs full transactions (a plan or node with full pending bodies)")
                            retry_after = 1800
                            break
                        if time.monotonic() - last_publish >= 5:
                            last_publish = time.monotonic()
                            await publish(self.redis, self.stats)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.stats.last_error = f"{type(exc).__name__}: {str(exc)[:160]}"
                self.stats.connect_failures += 1
                self.stats.failures_in_row += 1
                if self.stats.state not in (REFUSED, LIMITED):
                    self.stats.state = RECONNECTING
            self.stats.reconnects += 1
            await publish(self.redis, self.stats)
            await _sleep(stop, retry_after)
            backoff = min(60.0, backoff * 2)
