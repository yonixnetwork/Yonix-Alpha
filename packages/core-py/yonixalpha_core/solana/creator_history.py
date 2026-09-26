"""Creator (developer) history of a pump.fun token.

How many pump.fun tokens has the launch creator's wallet created? Counted
on chain: every pump.fun launch creates one BondingCurve account (a PDA of
the Pump program), and since 2025 that account stores the creator's wallet
right after the `complete` flag:

    discriminator (8) | virtual_token u64 | virtual_sol u64 | real_token u64 |
    real_sol u64 | total_supply u64 | complete bool (offset 48) |
    creator pubkey (offset 49) | ...

A getProgramAccounts filtered on the discriminator and on the creator at
offset 49, with a one-byte data slice (the `complete` flag), returns every
curve this wallet created and whether each one migrated. Curves are not
closed after migration, so migrated launches are counted too. Curves
created before pump.fun added the creator field do not carry it and are
not counted: the on-chain count can undercount a wallet that launched only
before that, never overcount.

When the RPC refuses or fails the query, the only other evidence is this
system's own stream: launches by the wallet that the pump.fun stream saw
(kept 7 days). That is a lower bound. It is reported as such and used only
when it already meets the minimum; otherwise the history is UNKNOWN. A
number is never estimated.

Extras, from the stream only (so bounded to what it saw and still keeps):
the creator selling early on its previous launches, and previous launches
the fresh-token observation rejected.
"""

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from redis.asyncio import Redis
from solders.pubkey import Pubkey

from yonixalpha_core.solana import pump_stream
from yonixalpha_core.solana.codec import b58encode
from yonixalpha_core.solana.pumpfun import BONDING_CURVE_ACCOUNT, PUMP_PROGRAM_ID

CREATOR_OFFSET = 49
COMPLETE_OFFSET = 48
CACHE_TTL = 3600  # a creator's on-chain count, re-read hourly
ERROR_TTL = 120  # a failed query is not retried for this long
MAX_CACHED_CURVES = 2000
MAX_PREVIOUS_CHECKED = 20  # previous stream-observed launches inspected for behaviour
EARLY_SELL_SECONDS = 300

SOURCE_ONCHAIN = "on-chain: pump.fun BondingCurve accounts with this creator (getProgramAccounts)"
SOURCE_STREAM = "this system's pump.fun stream (launches it observed; lower bound)"

VERIFIED = "VERIFIED"
LOWER_BOUND = "LOWER_BOUND"
UNKNOWN = "UNKNOWN"


def cache_key(creator: str) -> str:
    return f"yx:creator_hist:{creator}"


def bonding_curve_pda(mint: str) -> str:
    return str(Pubkey.find_program_address([b"bonding-curve", bytes(Pubkey.from_string(mint))],
                                           Pubkey.from_string(PUMP_PROGRAM_ID))[0])


@dataclass
class CreatorHistory:
    creator: str | None
    status: str  # VERIFIED | LOWER_BOUND | UNKNOWN
    tokens_created: int | None  # pump.fun tokens created by the wallet, this one included
    previous_launches: int | None
    previous_migrated: int | None
    source: str | None
    observed_at: str
    stream_observed_launches: int | None = None
    stream_since: str | None = None
    previous_checked_for_sells: int = 0
    previous_creator_sold_early: int | None = None
    previous_observation_rejected: int | None = None
    previous_rejection_reasons: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _rows(res: Any) -> list[dict]:
    if isinstance(res, dict):  # withContext shape
        res = res.get("value")
    if not isinstance(res, list):
        raise ValueError("getProgramAccounts returned an unexpected shape")
    return res


async def onchain_curves(rpc, creator: str) -> list[tuple[str, bool]]:
    """(curve address, complete) for every BondingCurve naming `creator`."""
    import base64

    res = await rpc.call("getProgramAccounts", [PUMP_PROGRAM_ID, {
        "encoding": "base64", "commitment": "confirmed",
        "dataSlice": {"offset": COMPLETE_OFFSET, "length": 1},
        "filters": [{"memcmp": {"offset": 0, "bytes": b58encode(BONDING_CURVE_ACCOUNT)}},
                    {"memcmp": {"offset": CREATOR_OFFSET, "bytes": creator}}],
    }])
    out: list[tuple[str, bool]] = []
    for row in _rows(res):
        data = ((row.get("account") or {}).get("data") or [""])[0]
        raw = base64.b64decode(data) if data else b""
        out.append((row["pubkey"], bool(raw[:1] == b"\x01")))
    return out


async def _cached_curves(redis: Redis, rpc, creator: str) -> tuple[list[tuple[str, bool]] | None, int, bool, str | None]:
    """(curves, total count, truncated, error). Cached per creator."""
    raw = await redis.get(cache_key(creator))
    if raw:
        c = json.loads(raw)
        if c.get("error"):
            return None, 0, False, c["error"]
        return [(p, bool(f)) for p, f in c["curves"]], c["count"], c["truncated"], None
    try:
        curves = await onchain_curves(rpc, creator)
    except Exception as exc:  # noqa: BLE001 - RPC refusal/failure is data unavailability
        err = f"getProgramAccounts failed: {type(exc).__name__}: {str(exc)[:160]}"
        await redis.set(cache_key(creator), json.dumps({"error": err}), ex=ERROR_TTL)
        return None, 0, False, err
    truncated = len(curves) > MAX_CACHED_CURVES
    await redis.set(cache_key(creator), json.dumps({"count": len(curves), "truncated": truncated,
                                                    "curves": [[p, int(f)] for p, f in curves[:MAX_CACHED_CURVES]]}),
                    ex=CACHE_TTL)
    return curves[:MAX_CACHED_CURVES], len(curves), truncated, None


async def _stream_behaviour(redis: Redis, h: CreatorHistory, creator: str, mint: str) -> None:
    key = pump_stream.creator_key(creator)
    rows = await redis.zrevrange(key, 0, MAX_PREVIOUS_CHECKED, withscores=True)
    started = await redis.get(pump_stream.STREAM_STARTED)
    h.stream_observed_launches = int(await redis.zcard(key))
    h.stream_since = datetime.fromtimestamp(int(started), tz=timezone.utc).isoformat() if started else None
    previous = [(m, int(ts)) for m, ts in rows if m != mint][:MAX_PREVIOUS_CHECKED]
    sold = rejected = checked = 0
    reasons: list[str] = []
    for prev, created in previous:
        trades = await pump_stream.load_trades(redis, prev)
        if trades:
            checked += 1
            if any(t.trader == creator and not t.is_buy and (t.at.timestamp() - created) <= EARLY_SELL_SECONDS
                   for t in trades):
                sold += 1
        report = await redis.get(pump_stream.obs_report_key(prev))
        if report:
            try:
                r = json.loads(report)
            except ValueError:
                continue
            if r.get("outcome") == "REJECT":
                rejected += 1
                if r.get("reasons"):
                    reasons.append(f"{prev[:6]}…: {r['reasons'][-1]}")
    h.previous_checked_for_sells = checked
    h.previous_creator_sold_early = sold if checked else None
    h.previous_observation_rejected = rejected if previous else None
    h.previous_rejection_reasons = reasons[:5]


async def creator_history(redis: Redis, rpc, creator: str | None, mint: str, curve: str | None,
                          now: datetime) -> CreatorHistory:
    """History of `creator`, the wallet that launched `mint`. `curve` is the
    token's bonding-curve address (derived from the mint when unknown)."""
    at = now.isoformat()
    if not creator:
        return CreatorHistory(None, UNKNOWN, None, None, None, None, at,
                              error="creator wallet unknown (the create event was not seen by this stream)")
    curve = curve or bonding_curve_pda(mint)
    curves, count, truncated, err = await _cached_curves(redis, rpc, creator)
    h = CreatorHistory(creator, UNKNOWN, None, None, None, None, at, error=err)
    if curves is not None:
        includes_current = any(p == curve for p, _ in curves)
        current_complete = any(p == curve and f for p, f in curves)
        # A token created after the count was cached is not in it yet; it
        # was created by this wallet all the same.
        total = count if (includes_current or truncated) else count + 1
        h.status, h.source, h.tokens_created = VERIFIED, SOURCE_ONCHAIN, total
        h.previous_launches = total - 1
        h.previous_migrated = sum(1 for _, f in curves if f) - (1 if current_complete else 0)
        if truncated:
            h.status, h.previous_migrated = LOWER_BOUND, None
            h.source = SOURCE_ONCHAIN + f" (more than {MAX_CACHED_CURVES} curves)"
    await _stream_behaviour(redis, h, creator, mint)
    if curves is None and h.stream_observed_launches:
        seen = h.stream_observed_launches
        # The current launch is in the stream set when its create was seen.
        h.status, h.source, h.tokens_created = LOWER_BOUND, SOURCE_STREAM, seen
        h.previous_launches = seen - 1
    return h
