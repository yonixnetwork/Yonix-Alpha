"""Creator (developer) history of a pump.fun token.

How many pump.fun tokens has the launch creator's wallet created? Counted
on chain: every pump.fun launch creates one BondingCurve account (a PDA of
the Pump program), and since 2025 that account stores the creator's wallet
right after the `complete` flag:

    discriminator (8) | virtual_token u64 | virtual_sol u64 | real_token u64 |
    real_sol u64 | total_supply u64 | complete bool (offset 48) |
    creator pubkey (offset 49) | ...

A program-account query filtered on the discriminator and on the creator at
offset 49, with a one-byte data slice (the `complete` flag), returns every
curve this wallet created and whether each one migrated. Helius serves it
as the paginated getProgramAccountsV2 (the Pump program has over 10 million
accounts, too many for plain getProgramAccounts); other RPCs get the plain
call. A scan stopped at the page limit is a lower bound, not a count. Curves are not
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
V2_PAGE_LIMIT = 10000  # Helius getProgramAccountsV2 maximum page size
MAX_V2_PAGES = 5  # beyond this the count is reported as a lower bound
MAX_PREVIOUS_CHECKED = 20  # previous stream-observed launches inspected for behaviour
EARLY_SELL_SECONDS = 300

SOURCE_ONCHAIN = "on-chain: pump.fun BondingCurve accounts with this creator"
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


@dataclass
class CurveScan:
    curves: list[tuple[str, bool]]  # (curve address, complete)
    complete: bool  # every page read
    total_results: int | None  # the RPC's own count of matches, when it reports one
    method: str
    pages: int


def _parse_rows(rows: list) -> list[tuple[str, bool]]:
    import base64

    out: list[tuple[str, bool]] = []
    for row in rows:
        data = ((row.get("account") or {}).get("data") or [""])[0]
        raw = base64.b64decode(data) if data else b""
        out.append((row["pubkey"], bool(raw[:1] == b"\x01")))
    return out


async def onchain_curves(rpc, creator: str) -> CurveScan:
    """Every BondingCurve naming `creator`.

    Helius refuses plain getProgramAccounts on the Pump program (over 10
    million accounts) and serves getProgramAccountsV2 instead: pages of at
    most 10,000, a `paginationKey` until the last page, optionally
    `totalResults` (helius-sdk types). At most MAX_V2_PAGES pages are read;
    a scan cut short is reported as incomplete, so its count is a lower
    bound. RPCs without V2 get the plain call."""
    config = {
        "encoding": "base64", "commitment": "confirmed",
        "dataSlice": {"offset": COMPLETE_OFFSET, "length": 1},
        "filters": [{"memcmp": {"offset": 0, "bytes": b58encode(BONDING_CURVE_ACCOUNT)}},
                    {"memcmp": {"offset": CREATOR_OFFSET, "bytes": creator}}],
    }
    curves: list[tuple[str, bool]] = []
    key, total, pages = None, None, 0
    try:
        while True:
            res = await rpc.call("getProgramAccountsV2", [PUMP_PROGRAM_ID, {**config, "limit": V2_PAGE_LIMIT,
                                                                           **({"paginationKey": key} if key else {})}])
            page = res.get("value", res) if isinstance(res, dict) else None
            if not isinstance(page, dict) or not isinstance(page.get("accounts"), list):
                raise ValueError("getProgramAccountsV2 returned an unexpected shape")
            curves += _parse_rows(page["accounts"])
            pages += 1
            key = page.get("paginationKey")
            if isinstance(page.get("totalResults"), int):
                total = page["totalResults"]
            if not key:
                return CurveScan(curves, True, total, "getProgramAccountsV2", pages)
            if pages >= MAX_V2_PAGES:
                return CurveScan(curves, False, total, "getProgramAccountsV2", pages)
    except Exception as v2_exc:  # noqa: BLE001 - V2 unavailable on this RPC: try the plain call
        if pages:
            raise
        try:
            res = await rpc.call("getProgramAccounts", [PUMP_PROGRAM_ID, config])
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"getProgramAccountsV2: {str(v2_exc)[:160]}; getProgramAccounts: {str(exc)[:160]}") from exc
        return CurveScan(_parse_rows(_rows(res)), True, None, "getProgramAccounts", 1)


async def _cached_curves(redis: Redis, rpc, creator: str) -> tuple[dict | None, str | None]:
    """The creator's curve scan, cached per creator: {curves, count,
    complete, total_results, method}, or (None, error)."""
    raw = await redis.get(cache_key(creator))
    if raw:
        c = json.loads(raw)
        if c.get("error"):
            return None, c["error"]
        c["curves"] = [(p, bool(f)) for p, f in c["curves"]]
        return c, None
    try:
        scan = await onchain_curves(rpc, creator)
    except Exception as exc:  # noqa: BLE001 - RPC refusal/failure is data unavailability
        err = f"creator curve query failed: {type(exc).__name__}: {str(exc)[:300]}"
        await redis.set(cache_key(creator), json.dumps({"error": err}), ex=ERROR_TTL)
        return None, err
    c = {"count": len(scan.curves), "complete": scan.complete and len(scan.curves) <= MAX_CACHED_CURVES,
         "total_results": scan.total_results, "method": scan.method, "pages": scan.pages,
         "curves": [[p, int(f)] for p, f in scan.curves[:MAX_CACHED_CURVES]]}
    await redis.set(cache_key(creator), json.dumps(c), ex=CACHE_TTL)
    c["curves"] = scan.curves[:MAX_CACHED_CURVES]
    return c, None


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
    scan, err = await _cached_curves(redis, rpc, creator)
    curves = scan["curves"] if scan is not None else None
    h = CreatorHistory(creator, UNKNOWN, None, None, None, None, at, error=err)
    if scan is not None:
        includes_current = any(p == curve for p, _ in curves)
        current_complete = any(p == curve and f for p, f in curves)
        # This launch is the wallet's own even when it is not in the list
        # (the list was cached before it existed, or the scan stopped early).
        found = scan["count"] + (0 if includes_current else 1)
        via = f" via {scan.get('method', 'getProgramAccounts')}"
        if scan["complete"]:
            h.status, h.source, h.tokens_created = VERIFIED, SOURCE_ONCHAIN + via, found
            h.previous_migrated = sum(1 for _, f in curves if f) - (1 if current_complete else 0)
        else:
            # Incomplete: a lower bound (the RPC's own match count when it gives one).
            h.status, h.tokens_created = LOWER_BOUND, max(found, scan.get("total_results") or 0)
            h.source = SOURCE_ONCHAIN + via + (f", scan stopped after {scan.get('pages')} page(s)"
                                               if scan["count"] <= MAX_CACHED_CURVES else
                                               f", more than {MAX_CACHED_CURVES} curves")
        h.previous_launches = h.tokens_created - 1
    await _stream_behaviour(redis, h, creator, mint)
    if curves is None and h.stream_observed_launches:
        seen = h.stream_observed_launches
        # The current launch is in the stream set when its create was seen.
        h.status, h.source, h.tokens_created = LOWER_BOUND, SOURCE_STREAM, seen
        h.previous_launches = seen - 1
    return h
