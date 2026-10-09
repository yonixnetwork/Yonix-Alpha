"""X narrative intelligence (shadow mode, optional) — 2026-10-10.

Enriches Solana candidates with public X posts about them, through the
OFFICIAL X API v2 recent search only (GET /2/tweets/search/recent, read from
xdevplatform/docs 2026-10-09: last 7 days, 10-100 results per request, 450
requests / 15 min per app, pay-per-use at $0.005 per Post read, the same Post
not charged twice within one UTC day). No scraping, no logged-in sessions, no
undocumented endpoints.

What it is NOT allowed to do (spec §10-11):
  - block, delay or veto anything: the gate, risk, execution and position
    manager never wait for it. Candidates are queued (one Redis ZADD) and a
    separate background pass looks them up later;
  - buy anything: no X result creates, lifts or changes a trade. It runs in
    SHADOW: scores are recorded next to the decision, COMBINED_SIGNAL_SCORE
    uses `narrative_weight` (default 0, i.e. equal to the on-chain score) and
    is never read by the gate;
  - invent data: no posts -> NO_DATA / NO_MATCH, never a neutral or bullish
    sentiment; metrics the API did not return are absent, not zero.

Identity resolution (spec §5): a post is about a token only with evidence.
  EXACT_MINT   the mint address appears in the text or an expanded URL     0.95
  NAME_TICKER  the cashtag AND the exact token name both appear            0.50
  TICKER_ONLY  only the cashtag (many tokens share a ticker)               0.15
  NAME_ONLY    only the name (could be the person / event, not the token)  0.10
A post that names a DIFFERENT Solana address next to the ticker is
conflicting evidence and is rejected for this token. Posts below 0.5 are kept
as candidates (counted, never scored). An author whose handle matches the
token name is flagged, never treated as an endorsement.

Storage (X Developer Policy: stored X content must follow deletions): only
derived numbers, Post IDs, author IDs and links are persisted; post text is
kept in Redis for `cache_ttl_seconds` at most.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import time
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import httpx

from yonixalpha_core.logging import get_logger

log = get_logger("core.x_narrative")

SEARCH_URL = "https://api.x.com/2/tweets/search/recent"
COST_PER_POST_USD = Decimal("0.005")  # pricing.mdx, Posts: Read, per resource returned
TIMEOUT_SECONDS = 5.0
SETTINGS_KEY = "x_narrative_settings"
QUEUE = "yx:x:queue"  # zset mint -> enqueued ts
QUEUE_META = "yx:x:meta:{mint}"  # name / symbol / decision context, short TTL
CACHE = "yx:x:cache:{mint}"  # last result (text included), TTL cache_ttl_seconds
REQUESTS_PER_MINT = "yx:x:req:{mint}"
SPEND = "yx:x:spend:{day}"  # USD spent today (string decimal)
SEEN_POSTS = "yx:x:seen:{day}"  # post ids already charged today
STATUS = "yx:x:status"
COOLDOWN = "yx:x:cooldown"  # set after RATE_LIMITED / UNAUTHORIZED / BUDGET_EXHAUSTED: no request until it expires
QUEUE_MAX = 500

# statuses
NOT_CONFIGURED, DISABLED, OK, NO_MATCH, NO_DATA = "NOT_CONFIGURED", "DISABLED", "OK", "NO_MATCH", "NO_DATA"
RATE_LIMITED, TIMEOUT, UNAVAILABLE, UNAUTHORIZED, MALFORMED = "RATE_LIMITED", "TIMEOUT", "UNAVAILABLE", "UNAUTHORIZED", "MALFORMED"
BUDGET_EXHAUSTED, INSUFFICIENT_DATA = "BUDGET_EXHAUSTED", "INSUFFICIENT_DATA"
STOP_STATUSES = (RATE_LIMITED, UNAUTHORIZED, BUDGET_EXHAUSTED)  # provider-wide: pause, store nothing per mint

EXACT_MINT, NAME_TICKER, TICKER_ONLY, NAME_ONLY, CONFLICT = "EXACT_MINT", "NAME_TICKER", "TICKER_ONLY", "NAME_ONLY", "CONFLICT"
CONFIDENCE = {EXACT_MINT: Decimal("0.95"), NAME_TICKER: Decimal("0.50"), TICKER_ONLY: Decimal("0.15"),
              NAME_ONLY: Decimal("0.10"), CONFLICT: Decimal("0")}
ACCEPT_AT = Decimal("0.5")
MIN_POSTS_FOR_METRICS = 3
WINDOWS = (60, 300, 900, 3600)
SOLANA_ADDRESS = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")


@dataclass
class XNarrativeSettings:
    enabled: bool = False  # dashboard switch; X_NARRATIVE_ENABLED in .env must be true as well
    provider: str = "official"
    max_requests_per_token: int = 2
    max_posts_per_query: int = 10  # recent search: 10..100, each returned Post is billed
    max_query_age_seconds: int = 3600  # posts older than this are not requested
    cache_ttl_seconds: int = 600
    max_daily_budget_usd: Decimal = Decimal("1.00")  # 200 Post reads per day
    narrative_weight: Decimal = Decimal("0")  # shadow: COMBINED = ONCHAIN when 0; never read by the gate
    query_scope: str = "qualified"  # qualified: only candidates whose on-chain signal qualified; all: every evaluated

    def to_dict(self) -> dict[str, Any]:
        return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in asdict(self).items()}


LIMITS = {"max_requests_per_token": (1, 10), "max_posts_per_query": (10, 100), "max_query_age_seconds": (60, 7 * 86400),
          "cache_ttl_seconds": (60, 86400)}


def parse_settings(raw: dict[str, Any] | None) -> tuple[XNarrativeSettings, list[str]]:
    s, errors = XNarrativeSettings(), []
    for f in fields(XNarrativeSettings):
        if not raw or f.name not in raw:
            continue
        v = raw[f.name]
        if f.name == "enabled":
            if not isinstance(v, bool):
                errors.append("enabled: true or false")
            else:
                s.enabled = v
        elif f.name == "provider":
            if v != "official":
                errors.append("provider: only the official X API is supported")
        elif f.name == "query_scope":
            if v not in ("qualified", "all"):
                errors.append("query_scope: qualified or all")
            else:
                s.query_scope = v
        elif f.name in ("max_daily_budget_usd", "narrative_weight"):
            try:
                d = Decimal(str(v))
            except ArithmeticError:
                errors.append(f"{f.name}: a number")
                continue
            hi = Decimal("1000") if f.name == "max_daily_budget_usd" else Decimal("1")
            if not Decimal(0) <= d <= hi:
                errors.append(f"{f.name}: 0..{hi}")
            else:
                setattr(s, f.name, d)
        else:
            if isinstance(v, bool) or not isinstance(v, int):
                errors.append(f"{f.name}: a whole number")
                continue
            lo, hi = LIMITS[f.name]
            if not lo <= v <= hi:
                errors.append(f"{f.name}: {lo}..{hi}")
            else:
                setattr(s, f.name, v)
    return s, errors


async def load_settings(session) -> XNarrativeSettings:
    from yonixalpha_core.db.models import PlatformSetting

    row = await session.get(PlatformSetting, SETTINGS_KEY)
    s, errors = parse_settings(row.value if row else None)
    return XNarrativeSettings() if errors else s


def bearer(app_settings: Any) -> str | None:
    v = getattr(app_settings, "X_API_BEARER_TOKEN", None)
    v = v.get_secret_value() if hasattr(v, "get_secret_value") else v
    return v or None


def provider_status(app_settings: Any, cfg: XNarrativeSettings | None = None) -> str:
    """NOT_CONFIGURED without a bearer token (never a fake healthy status),
    DISABLED while either switch is off, else ENABLED."""
    if not bearer(app_settings):
        return NOT_CONFIGURED
    if not getattr(app_settings, "X_NARRATIVE_ENABLED", False) or (cfg is not None and not cfg.enabled):
        return DISABLED
    return "ENABLED"


# --- queue (called from the gate; one ZADD, never awaited for a result) -------------------------------

async def enqueue(redis, mint: str, name: str | None, symbol: str | None, context: dict[str, Any], now: float | None = None) -> bool:
    """Queues a candidate for a later lookup. Cheap and non-blocking for the
    caller: the lookup happens in run_pass. Returns whether it was new."""
    now = now or time.time()
    added = await redis.zadd(QUEUE, {mint: now}, nx=True)
    if added:
        await redis.set(QUEUE_META.format(mint=mint), json.dumps({"name": name, "symbol": symbol, **context}, default=str),
                        ex=3600)
        if await redis.zcard(QUEUE) > QUEUE_MAX:
            await redis.zremrangebyrank(QUEUE, 0, -QUEUE_MAX - 1)  # oldest dropped first
    return bool(added)


# --- query -------------------------------------------------------------------------------------------

def _clean_term(text: str | None, limit: int = 40) -> str:
    return re.sub(r'[^\w\s\-.]', "", (text or "")).strip()[:limit]


def build_query(mint: str, name: str | None, symbol: str | None) -> str:
    """Bounded recent-search query (<= 512 chars): the exact mint, or the
    cashtag together with the exact name. Retweets excluded (no new author)."""
    parts = [f'"{mint}"']
    sym, nm = _clean_term(symbol, 12), _clean_term(name)
    if sym and nm and sym.lower() != nm.lower():
        parts.append(f'(${sym} "{nm}")')
    elif sym:
        parts.append(f"${sym}")
    return f"({' OR '.join(parts)}) -is:retweet"[:512]


class XApiError(Exception):
    def __init__(self, status: str, detail: str, retry_after: int | None = None):
        super().__init__(detail)
        self.status, self.detail, self.retry_after = status, detail, retry_after


async def search_recent(client: httpx.AsyncClient, token: str, query: str, max_results: int, start_time: datetime,
                        timeout: float = TIMEOUT_SECONDS) -> dict[str, Any]:
    params = {"query": query, "max_results": str(max(10, min(100, max_results))),
              "start_time": start_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
              "tweet.fields": "created_at,author_id,public_metrics,referenced_tweets,entities,lang",
              "expansions": "author_id", "user.fields": "created_at,username,name,verified,public_metrics"}
    try:
        r = await client.get(SEARCH_URL, params=params, headers={"Authorization": f"Bearer {token}"}, timeout=timeout)
    except httpx.TimeoutException as exc:
        raise XApiError(TIMEOUT, f"no answer within {timeout:.0f}s") from exc
    except httpx.HTTPError as exc:
        raise XApiError(UNAVAILABLE, type(exc).__name__) from exc
    if r.status_code in (401, 403):
        raise XApiError(UNAUTHORIZED, f"HTTP {r.status_code}")
    if r.status_code == 429:
        reset = r.headers.get("x-rate-limit-reset")
        raise XApiError(RATE_LIMITED, "HTTP 429", int(reset) - int(time.time()) if reset and reset.isdigit() else None)
    if r.status_code >= 500:
        raise XApiError(UNAVAILABLE, f"HTTP {r.status_code}")
    if r.status_code != 200:
        raise XApiError(UNAVAILABLE, f"HTTP {r.status_code}")
    try:
        body = r.json()
    except ValueError as exc:
        raise XApiError(MALFORMED, "non-JSON response") from exc
    if not isinstance(body, dict) or not isinstance(body.get("data", []), list):
        raise XApiError(MALFORMED, "unexpected response shape")
    return body


# --- identity, metrics, scores (pure) ----------------------------------------------------------------

def _text_of(post: dict[str, Any]) -> str:
    urls = [u.get("expanded_url") or u.get("url") or "" for u in ((post.get("entities") or {}).get("urls") or [])]
    return " ".join([post.get("text") or "", *urls])


def classify_post(post: dict[str, Any], mint: str, name: str | None, symbol: str | None) -> tuple[str, list[str]]:
    """(evidence class, notes) of one post for this token."""
    text = _text_of(post)
    low = text.lower()
    notes = []
    if mint in text:
        return EXACT_MINT, ["the mint address appears in the post"]
    others = [a for a in SOLANA_ADDRESS.findall(text) if a != mint and len(a) >= 32]
    sym = (symbol or "").strip()
    tags = {t.get("tag", "").lower() for t in ((post.get("entities") or {}).get("cashtags") or [])}
    has_ticker = bool(sym) and (sym.lower() in tags or re.search(rf"\${re.escape(sym)}\b", text, re.I) is not None)
    nm = (name or "").strip()
    has_name = bool(nm) and len(nm) >= 3 and nm.lower() in low
    if others and (has_ticker or has_name):
        return CONFLICT, [f"names a different Solana address ({others[0][:6]}...) with this ticker/name"]
    if has_ticker and has_name:
        return NAME_TICKER, ["cashtag and exact token name both appear"]
    if has_ticker:
        notes.append("ticker only: many tokens share a ticker")
        return TICKER_ONLY, notes
    if has_name:
        return NAME_ONLY, ["name only: may refer to the person or event, not the token"]
    return NAME_ONLY, ["matched the query without the name or ticker in the visible text"]


def _norm(text: str) -> str:
    return hashlib.sha1(re.sub(r"\W+", " ", re.sub(r"https?://\S+", "", text.lower())).strip().encode()).hexdigest()


def author_credibility(user: dict[str, Any] | None, now: datetime) -> dict[str, Any]:
    """Lightweight, documented heuristics; verification is reported, never
    proof of identity, endorsement or truth (spec §7)."""
    if not user:
        return {"score": None, "why": "author not returned"}
    age_days = None
    if user.get("created_at"):
        try:
            age_days = (now - datetime.fromisoformat(user["created_at"].replace("Z", "+00:00"))).days
        except ValueError:
            age_days = None
    followers = ((user.get("public_metrics") or {}).get("followers_count"))
    parts = []
    if age_days is not None:
        parts.append(min(1.0, age_days / 365))
    if isinstance(followers, int):
        parts.append(min(1.0, math.log10(followers + 1) / 5))
    return {"score": round(sum(parts) / len(parts), 3) if parts else None, "account_age_days": age_days,
            "followers": followers, "verified_reported_by_x": bool(user.get("verified"))}


def analyse(body: dict[str, Any], mint: str, name: str | None, symbol: str | None, now: datetime,
            onchain_score: Decimal | None = None, narrative_weight: Decimal = Decimal(0)) -> dict[str, Any]:
    """Identity, velocity, coordination and scores from one search result."""
    posts = body.get("data") or []
    users = {u.get("id"): u for u in ((body.get("includes") or {}).get("users") or []) if isinstance(u, dict)}
    accepted, candidates, rejected = [], [], []
    for p in posts:
        if not isinstance(p, dict) or not p.get("id"):
            continue
        cls, notes = classify_post(p, mint, name, symbol)
        u = users.get(p.get("author_id"))
        rec = {"id": str(p["id"]), "link": f"https://x.com/i/web/status/{p['id']}", "author_id": p.get("author_id"),
               "created_at": p.get("created_at"), "evidence": cls, "confidence": str(CONFIDENCE[cls]), "notes": notes,
               "kind": _kind(p), "metrics": p.get("public_metrics"), "dup": _norm(p.get("text") or "")}
        if u and name and _clean_term(name).replace(" ", "").lower() in (u.get("username") or "").lower():
            rec["notes"] = notes + ["author handle resembles the token name: not treated as an endorsement"]
        if cls == CONFLICT:
            rejected.append(rec)
        elif CONFIDENCE[cls] >= ACCEPT_AT:
            accepted.append(rec)
        else:
            candidates.append(rec)
    out: dict[str, Any] = {"posts_returned": len(posts), "accepted": len(accepted), "candidates": len(candidates),
                           "rejected": len(rejected)}
    if not posts:
        out.update(status=NO_DATA, identity_confidence=None, narrative_score=None, social_data_quality=NO_DATA)
    elif not accepted:
        out.update(status=NO_MATCH, identity_confidence=str(max((Decimal(c["confidence"]) for c in candidates), default=Decimal(0))),
                   narrative_score=None, social_data_quality=NO_MATCH,
                   reason="posts found, but none identifies this token with enough evidence")
    else:
        out.update(status=OK, identity_confidence=str(max(Decimal(a["confidence"]) for a in accepted)))
        out.update(_metrics(accepted, users, now))
    onchain = onchain_score
    narr = Decimal(str(out["narrative_score"])) if out.get("narrative_score") is not None else None
    out["onchain_score"] = str(onchain) if onchain is not None else None
    if onchain is None:
        out["combined_signal_score"] = None
    elif narr is None or not narrative_weight:  # shadow default: the on-chain score, untouched
        out["combined_signal_score"] = str(onchain)
    else:
        out["combined_signal_score"] = str(onchain * (1 - narrative_weight) + narr * narrative_weight)
    out["shadow"] = "recorded only: never read by the gate"
    out["evidence"] = sorted(accepted + candidates[:5] + rejected[:5], key=lambda r: r.get("created_at") or "")[:20]
    return out


def _kind(p: dict[str, Any]) -> str:
    refs = {r.get("type") for r in (p.get("referenced_tweets") or [])}
    return "reply" if "replied_to" in refs else "quote" if "quoted" in refs else "original"


def _metrics(accepted: list[dict[str, Any]], users: dict[str, Any], now: datetime) -> dict[str, Any]:
    def ts(r):
        try:
            return datetime.fromisoformat((r["created_at"] or "").replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return None

    stamped = [(ts(r), r) for r in accepted]
    stamped = [(t, r) for t, r in stamped if t is not None]
    authors = {r["author_id"] for r in accepted if r.get("author_id")}
    windows = {}
    for w in WINDOWS:
        inside = [r for t, r in stamped if (now - t).total_seconds() <= w]
        windows[f"{w}s"] = {"posts": len(inside), "authors": len({r['author_id'] for r in inside})}
    n = len(accepted)
    by_author: dict[str, int] = {}
    for r in accepted:
        by_author[r.get("author_id") or "?"] = by_author.get(r.get("author_id") or "?", 0) + 1
    dup_rate = 1 - len({r["dup"] for r in accepted}) / n
    top_share = max(by_author.values()) / n
    kinds = {k: sum(1 for r in accepted if r["kind"] == k) for k in ("original", "reply", "quote")}
    times = sorted(t for t, _ in stamped)
    burst = sum(1 for a, b in zip(times, times[1:]) if (b - a).total_seconds() <= 10)
    eng = [r["metrics"] for r in accepted if isinstance(r.get("metrics"), dict)]
    engagement = sum(int(m.get("like_count", 0)) + int(m.get("retweet_count", 0)) + int(m.get("reply_count", 0)) +
                     int(m.get("quote_count", 0)) for m in eng) if eng else None
    cred = [author_credibility(users.get(a), now)["score"] for a in authors]
    cred = [c for c in cred if c is not None]
    flags = []
    if n >= MIN_POSTS_FOR_METRICS and dup_rate > 0.5:
        flags.append("REPEATED_CONTENT")
    if n >= 5 and top_share > 0.5:
        flags.append("AUTHOR_CONCENTRATION")
    if n >= 5 and burst >= n * 0.5 and len(authors) <= 2:
        flags.append("SYNCHRONIZED_BURST")
    if n >= 5 and len(authors) <= 2:
        flags.append("FEW_INDEPENDENT_AUTHORS")
    quality = "OK" if n >= MIN_POSTS_FOR_METRICS and len(authors) >= 2 else INSUFFICIENT_DATA
    score = None
    if quality == "OK":
        s = 20 * math.log2(1 + len(authors))  # distinct authors, not post count
        s += 10 * min(3.0, windows["300s"]["authors"] / max(1, windows["3600s"]["authors"] / 12))  # 5-min vs hourly pace
        s *= (1 - 0.25 * len(flags))
        score = round(max(0.0, min(100.0, s)), 1)
    first = times[0].isoformat() if times else None
    return {"windows": windows, "distinct_authors": len(authors), "posts_by_kind": kinds,
            "duplicate_rate": round(dup_rate, 3), "top_author_share": round(top_share, 3),
            "engagement_total": engagement, "engagement_note": None if eng else "the API returned no engagement metrics",
            "source_credibility": round(sum(cred) / len(cred), 3) if cred else None, "coordination_flags": flags,
            "social_data_quality": quality, "narrative_score": score, "first_relevant_post_at": first}


# --- budget ------------------------------------------------------------------------------------------

def _day(now: datetime) -> str:
    return now.astimezone(timezone.utc).strftime("%Y%m%d")


async def spent_today(redis, now: datetime) -> Decimal:
    v = await redis.get(SPEND.format(day=_day(now)))
    return Decimal(v) if v else Decimal(0)


async def charge(redis, post_ids: list[str], now: datetime) -> Decimal:
    """Counts the cost of newly returned posts (a post already read today is
    free: X deduplicates within the UTC day)."""
    key = SEEN_POSTS.format(day=_day(now))
    new = 0
    for pid in post_ids:
        new += int(await redis.sadd(key, pid))
    await redis.expire(key, 2 * 86400)
    cost = COST_PER_POST_USD * new
    if cost:
        total = await spent_today(redis, now) + cost
        await redis.set(SPEND.format(day=_day(now)), str(total), ex=2 * 86400)
    return cost


# --- the pass (decision-engine background task) ------------------------------------------------------

async def lookup(redis, client: httpx.AsyncClient, token: str, cfg: XNarrativeSettings, mint: str, meta: dict[str, Any],
                 now: datetime) -> dict[str, Any]:
    """One bounded lookup for one mint; never raises."""
    cached = await redis.get(CACHE.format(mint=mint))
    if cached:
        return {**json.loads(cached), "from_cache": True}
    reqs = int(await redis.get(REQUESTS_PER_MINT.format(mint=mint)) or 0)
    if reqs >= cfg.max_requests_per_token:
        return {"status": "SKIPPED", "reason": f"already looked up {reqs} time(s) (max_requests_per_token)"}
    worst_case = COST_PER_POST_USD * cfg.max_posts_per_query
    if await spent_today(redis, now) + worst_case > cfg.max_daily_budget_usd:
        return {"status": BUDGET_EXHAUSTED, "reason": f"daily budget {cfg.max_daily_budget_usd} USD reached"}
    await redis.set(REQUESTS_PER_MINT.format(mint=mint), reqs + 1, ex=86400)
    query = build_query(mint, meta.get("name"), meta.get("symbol"))
    try:
        body = await search_recent(client, token, query, cfg.max_posts_per_query,
                                   now - timedelta(seconds=cfg.max_query_age_seconds))
    except XApiError as exc:
        return {"status": exc.status, "reason": exc.detail, "retry_after": exc.retry_after, "query": query}
    await charge(redis, [str(p.get("id")) for p in body.get("data") or [] if isinstance(p, dict)], now)
    onchain = meta.get("onchain_score")
    result = analyse(body, mint, meta.get("name"), meta.get("symbol"), now,
                     Decimal(str(onchain)) if onchain is not None else None, cfg.narrative_weight)
    result["query"] = query
    await redis.set(CACHE.format(mint=mint), json.dumps(result, default=str), ex=cfg.cache_ttl_seconds)
    return result


async def run_pass(session_factory, redis, app_settings: Any, client: httpx.AsyncClient, now: datetime | None = None,
                   batch: int = 5) -> dict[str, Any]:
    """Looks up up to `batch` queued candidates and stores one observation
    each. Does nothing unless both switches are on and a token is set; stops
    early on RATE_LIMITED / UNAUTHORIZED / BUDGET_EXHAUSTED."""
    from yonixalpha_core.db.models import XNarrativeObservation

    now = now or datetime.now(timezone.utc)
    async with session_factory() as s:
        cfg = await load_settings(s)
    status = provider_status(app_settings, cfg)
    counts = {"status": status, "looked_up": 0, "stored": 0}
    if status != "ENABLED":
        await redis.set(STATUS, json.dumps({"status": status, "at": now.isoformat()}), ex=3600)
        return counts
    cooling = await redis.get(COOLDOWN)
    if cooling:
        counts["status"] = cooling
        return counts
    token = bearer(app_settings)
    picked = await redis.zrange(QUEUE, 0, batch - 1)
    for mint in picked:
        await redis.zrem(QUEUE, mint)
        raw = await redis.get(QUEUE_META.format(mint=mint))
        meta = json.loads(raw) if raw else {}
        if cfg.query_scope == "qualified" and not meta.get("qualified"):
            continue
        res = await lookup(redis, client, token, cfg, mint, meta, now)
        counts["looked_up"] += 1
        if res.get("status") in STOP_STATUSES:
            wait = {RATE_LIMITED: max(60, int(res.get("retry_after") or 900)), UNAUTHORIZED: 3600,
                    BUDGET_EXHAUSTED: _seconds_to_utc_midnight(now)}[res["status"]]
            await redis.set(COOLDOWN, res["status"], ex=wait)
            counts["stopped"] = res["status"]
            break
        if res.get("from_cache") or res.get("status") == "SKIPPED":
            continue
        async with session_factory() as s:
            s.add(XNarrativeObservation(
                mint=mint, observed_at=now, status=res.get("status"), query=(res.get("query") or "")[:512],
                identity_confidence=_dec(res.get("identity_confidence")), narrative_score=_dec(res.get("narrative_score")),
                social_data_quality=res.get("social_data_quality"), onchain_score=_dec(res.get("onchain_score")),
                combined_score=_dec(res.get("combined_signal_score")),
                features=_persistable(res), evidence=[{k: e.get(k) for k in ("id", "link", "author_id", "created_at", "evidence",
                                                                                  "confidence", "notes", "kind")}
                                                      for e in res.get("evidence") or []],
                decision_context={k: meta.get(k) for k in ("assessment_id", "decided_at", "engine", "qualified", "executable")},
                posts_returned=int(res.get("posts_returned") or 0)))
            await s.commit()
        counts["stored"] += 1
    await redis.set(STATUS, json.dumps({"status": counts.get("stopped") or OK, "at": now.isoformat(), **counts}), ex=3600)
    return counts


def _seconds_to_utc_midnight(now: datetime) -> int:
    nxt = (now.astimezone(timezone.utc) + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(60, int((nxt - now).total_seconds()))


def _dec(v: Any) -> Decimal | None:
    if v is None:
        return None
    try:
        return Decimal(str(v))
    except ArithmeticError:
        return None


def _persistable(res: dict[str, Any]) -> dict[str, Any]:
    """Derived numbers only (no post text)."""
    keep = ("status", "reason", "posts_returned", "accepted", "candidates", "rejected", "windows", "distinct_authors",
            "posts_by_kind", "duplicate_rate", "top_author_share", "engagement_total", "engagement_note",
            "source_credibility", "coordination_flags", "first_relevant_post_at", "shadow", "retry_after")
    return {k: res.get(k) for k in keep if k in res}


async def features_at(session, mint: str, at: datetime) -> dict[str, Any] | None:
    """The latest observation made AT OR BEFORE `at` (leakage-safe: a model
    trained on decisions never sees social data from after the decision)."""
    from sqlalchemy import select

    from yonixalpha_core.db.models import XNarrativeObservation

    row = (await session.execute(select(XNarrativeObservation).where(
        XNarrativeObservation.mint == mint, XNarrativeObservation.observed_at <= at)
        .order_by(XNarrativeObservation.observed_at.desc()).limit(1))).scalar_one_or_none()
    if row is None:
        return None
    return {"observed_at": row.observed_at, "status": row.status, "identity_confidence": row.identity_confidence,
            "narrative_score": row.narrative_score, "social_data_quality": row.social_data_quality, **(row.features or {})}


async def run_forever(session_factory, redis, app_settings: Any, stop_event: asyncio.Event, interval: float = 15.0) -> None:
    """Background loop for the decision engine. Pauses while the server is at
    CRITICAL resource level; never raises."""
    from yonixalpha_core import resources

    async with httpx.AsyncClient() as client:
        while not stop_event.is_set():
            off = provider_status(app_settings)
            if off != "ENABLED":  # no token or .env switch off: no database read, no request, just the status
                await redis.set(STATUS, json.dumps({"status": off, "at": datetime.now(timezone.utc).isoformat()}), ex=3600)
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=300)
                except asyncio.TimeoutError:
                    pass
                continue
            try:
                level, _ = resources.level(resources.sample(), app_settings)
                if level != "CRITICAL":
                    out = await run_pass(session_factory, redis, app_settings, client)
                    if out.get("looked_up"):
                        log.info("x_narrative.pass", **out)
            except Exception as exc:  # noqa: BLE001 - enrichment never stops the decision engine
                log.warning("x_narrative.pass_failed", error=type(exc).__name__)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
