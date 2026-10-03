"""External wallet intelligence: Nansen and MadeOnSol (master §24-25, M11).

Enrichment only. A provider's labels and P/L are shown next to YonixAlpha's
own measurements; they are never a trade signal, never replace the own FIFO
ledger (wallet_pnl), never validate a wallet on their own and never copy a
wallet. Both providers are paid and optional:

  - no key: NOT_CONFIGURED, nothing is called;
  - a key but enrichment switched off (the default): nothing is called;
  - switched on: calls stay inside a daily budget per provider (they cost
    credits), each wallet at most once per refresh window.

Endpoints (read from the providers' own code, 2026-10-03):

  Nansen     https://api.nansen.ai, header `apikey` (nansen-ai/nansen-cli src/api.js)
             GET  /api/v1/account                       free account check
             POST /api/v1/profiler/address/labels       {address, chain, pagination}
             POST /api/v1/profiler/address/pnl-summary  {address, chain, date: {from, to}}
             POST /api/v1/smart-money/dex-trades        {chains, filters, pagination}
             chains: "solana", "bnb" (BSC). Robinhood Chain is not covered.
  MadeOnSol  https://madeonsol.com/api/v1, header `Authorization: Bearer`
             (madeonsol/madeonsol-sdk src/index.ts) - Solana only
             GET /me                       tier and daily quota (free)
             GET /wallet/{address}/pnl     FIFO P/L summary (SOL)
             GET /kol/{wallet}             KOL profile (404: not a tracked KOL)
             GET /kol/leaderboard          KOL leaderboard (discovery)

Response fields beyond those typed in the providers' SDKs are kept as given
and marked provider-reported; YonixAlpha does not verify them.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta
from typing import Any

import httpx

NANSEN, MADEONSOL = "nansen", "madeonsol"
PROVIDERS = (NANSEN, MADEONSOL)
NANSEN_BASE = "https://api.nansen.ai"
MADEONSOL_BASE = "https://madeonsol.com/api/v1"
NANSEN_CHAIN = {"solana": "solana", "bsc": "bnb"}
CHAINS = {NANSEN: ("solana", "bsc"), MADEONSOL: ("solana",)}
KEY_SETTING = {NANSEN: "NANSEN_API_KEY", MADEONSOL: "MADEONSOL_API_KEY"}
TIMEOUT = 20.0

OK, NOT_CONFIGURED, UNSUPPORTED, UNAUTHORIZED, PAYMENT_REQUIRED, RATE_LIMITED, UNAVAILABLE, BUDGET_EXHAUSTED = (
    "OK", "NOT_CONFIGURED", "UNSUPPORTED_CHAIN", "UNAUTHORIZED", "PAYMENT_REQUIRED", "RATE_LIMITED", "UNAVAILABLE",
    "BUDGET_EXHAUSTED")
NOTE = "provider-reported; not verified by YonixAlpha and never used as a trade signal"
BUDGET_KEY = "yx:enrich:calls:{provider}:{day}"
SETTINGS_KEY = "enrichment_settings"


@dataclass
class EnrichmentSettings:
    enabled: bool = False  # paid APIs: off until the operator switches it on
    nansen_daily_calls: int = 100
    madeonsol_daily_calls: int = 200
    refresh_hours: int = 24
    wallets_per_pass: int = 10
    discovery: bool = False  # candidate wallets from the providers' leaderboards / smart money
    discovery_limit: int = 25

    @classmethod
    def parse(cls, raw: dict[str, Any] | None) -> "EnrichmentSettings":
        d = cls()
        for f in fields(cls):
            if raw and f.name in raw:
                v = raw[f.name]
                if f.type in ("bool", bool):
                    if not isinstance(v, bool):
                        raise ValueError(f"{f.name}: true or false")
                else:
                    if isinstance(v, bool) or not isinstance(v, int):
                        raise ValueError(f"{f.name}: a whole number")
                setattr(d, f.name, v)
        limits = {"nansen_daily_calls": (0, 10_000), "madeonsol_daily_calls": (0, 10_000), "refresh_hours": (1, 720),
                  "wallets_per_pass": (1, 200), "discovery_limit": (1, 100)}
        for name, (lo, hi) in limits.items():
            if not lo <= getattr(d, name) <= hi:
                raise ValueError(f"{name}: {lo}..{hi}")
        return d

    def budget(self, provider: str) -> int:
        return self.nansen_daily_calls if provider == NANSEN else self.madeonsol_daily_calls

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ProviderError(Exception):
    def __init__(self, status: str, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


def key_of(settings: Any, provider: str) -> str | None:
    v = getattr(settings, KEY_SETTING[provider], None)
    if v is None:
        return None
    v = v.get_secret_value() if hasattr(v, "get_secret_value") else str(v)
    return v or None


def _raise_for(r: httpx.Response, provider: str) -> None:
    if r.status_code in (401, 403):
        raise ProviderError(UNAUTHORIZED, f"{provider}: HTTP {r.status_code} (key rejected or plan does not include this)")
    if r.status_code == 402:
        raise ProviderError(PAYMENT_REQUIRED, f"{provider}: HTTP 402 (out of credits)")
    if r.status_code == 429:
        raise ProviderError(RATE_LIMITED, f"{provider}: HTTP 429 (retry after {r.headers.get('retry-after', '?')} s)")
    if r.status_code >= 400:
        raise ProviderError(UNAVAILABLE, f"{provider}: HTTP {r.status_code}")


async def _call(client: httpx.AsyncClient, provider: str, key: str, method: str, path: str,
                body: dict[str, Any] | None = None, params: dict[str, Any] | None = None,
                allow_404: bool = False) -> Any:
    if provider == NANSEN:
        url, headers = NANSEN_BASE + path, {"apikey": key, "Accept": "application/json"}
    else:
        url, headers = MADEONSOL_BASE + path, {"Authorization": f"Bearer {key}", "Accept": "application/json"}
    headers["User-Agent"] = "yonixalpha-enrichment"
    try:
        r = await client.request(method, url, headers=headers, json=body, params=params, timeout=TIMEOUT)
    except httpx.HTTPError as exc:
        raise ProviderError(UNAVAILABLE, f"{provider}: {type(exc).__name__}") from exc
    if allow_404 and r.status_code == 404:
        return None
    _raise_for(r, provider)
    try:
        return r.json()
    except ValueError as exc:
        raise ProviderError(UNAVAILABLE, f"{provider}: response is not JSON") from exc


def _scalars(d: Any, limit: int = 40) -> dict[str, Any]:
    """The plain fields of a provider object (numbers, text, booleans), so a
    stored record stays small and never carries nested provider payloads."""
    if not isinstance(d, dict):
        return {}
    out = {k: v for k, v in d.items() if isinstance(v, (int, float, str, bool)) or v is None}
    return dict(list(out.items())[:limit])


def _date_range(now: datetime, days: int) -> dict[str, str]:
    return {"from": (now - timedelta(days=days)).date().isoformat(), "to": now.date().isoformat()}


# --- per-wallet lookups --------------------------------------------------------------------------

async def nansen_wallet(client: httpx.AsyncClient, key: str, chain: str, address: str, now: datetime,
                        days: int = 30) -> tuple[dict[str, Any], int]:
    """(record, calls made). Labels and the P/L summary of one wallet."""
    nchain = NANSEN_CHAIN.get(chain)
    if nchain is None:
        raise ProviderError(UNSUPPORTED, f"Nansen does not cover {chain}")
    labels = await _call(client, NANSEN, key, "POST", "/api/v1/profiler/address/labels",
                         {"address": address, "chain": nchain, "pagination": {"page": 1, "per_page": 100}})
    pnl = await _call(client, NANSEN, key, "POST", "/api/v1/profiler/address/pnl-summary",
                      {"address": address, "chain": nchain, "date": _date_range(now, days)})
    rows = (labels or {}).get("data") if isinstance(labels, dict) else labels
    names = sorted({str(x.get("label")) for x in rows or [] if isinstance(x, dict) and x.get("label")})
    summary = _scalars(pnl.get("data") if isinstance(pnl, dict) and isinstance(pnl.get("data"), dict) else pnl)
    return {"labels": names, "name": None, "pnl": summary, "pnl_window_days": days,
            "pnl_currency": "USD" if any("usd" in k for k in summary) else None}, 2


async def madeonsol_wallet(client: httpx.AsyncClient, key: str, chain: str, address: str, now: datetime,
                           days: int = 30) -> tuple[dict[str, Any], int]:
    if chain != "solana":
        raise ProviderError(UNSUPPORTED, f"MadeOnSol covers Solana only, not {chain}")
    pnl = await _call(client, MADEONSOL, key, "GET", f"/wallet/{address}/pnl")
    kol = await _call(client, MADEONSOL, key, "GET", f"/kol/{address}", allow_404=True)
    summary = _scalars((pnl or {}).get("summary"))
    labels = []
    name = None
    if isinstance(kol, dict):  # KolWalletProfile: wallet, kol_name, kol_twitter, total_pnl_usd, win_rate, trade_count
        name = kol.get("kol_name")
        labels = ["KOL"]
    coverage = (pnl or {}).get("coverage") if isinstance(pnl, dict) else None
    return {"labels": labels, "name": name, "pnl": summary, "pnl_window_days": (pnl or {}).get("window_days"),
            "pnl_currency": "SOL", "coverage": _scalars(coverage), "kol": _scalars(kol, 10) if isinstance(kol, dict) else None,
            "cost_basis_from": ((pnl or {}).get("notes") or {}).get("cost_basis_observable_from")}, 2


LOOKUP = {NANSEN: nansen_wallet, MADEONSOL: madeonsol_wallet}


# --- discovery -----------------------------------------------------------------------------------

async def nansen_candidates(client: httpx.AsyncClient, key: str, chain: str, limit: int) -> list[dict[str, Any]]:
    """Wallets Nansen labels as smart money that traded on a DEX recently.
    Rows are read by `trader_address` / `trader_address_label` (the profiler
    dex-trades shape); NOT VERIFIED for smart-money/dex-trades until the first
    real response, so an unrecognised shape is reported, never guessed."""
    nchain = NANSEN_CHAIN.get(chain)
    if nchain is None:
        raise ProviderError(UNSUPPORTED, f"Nansen does not cover {chain}")
    res = await _call(client, NANSEN, key, "POST", "/api/v1/smart-money/dex-trades",
                      {"chains": [nchain], "filters": {}, "pagination": {"page": 1, "per_page": min(100, limit * 4)}})
    rows = res.get("data") if isinstance(res, dict) else res
    if not isinstance(rows, list):
        raise ProviderError(UNAVAILABLE, f"Nansen smart-money response not recognised (keys: "
                                         f"{sorted(res)[:8] if isinstance(res, dict) else type(res).__name__})")
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        a = isinstance(r, dict) and (r.get("trader_address") or r.get("address"))
        if a and a not in out:
            out[a] = {"wallet": a, "label": r.get("trader_address_label"), "source": "nansen smart-money dex trades"}
        if len(out) >= limit:
            break
    if rows and not out:
        raise ProviderError(UNAVAILABLE, "Nansen smart-money rows carry no trader address field")
    return list(out.values())


async def madeonsol_candidates(client: httpx.AsyncClient, key: str, chain: str, limit: int) -> list[dict[str, Any]]:
    if chain != "solana":
        raise ProviderError(UNSUPPORTED, f"MadeOnSol covers Solana only, not {chain}")
    res = await _call(client, MADEONSOL, key, "GET", "/kol/leaderboard", params={"period": "30d", "limit": min(limit, 100)})
    rows = (res or {}).get("leaderboard") or []
    return [{"wallet": r["wallet"], "label": r.get("name"), "source": "madeonsol KOL leaderboard (30d)",
             "provider_metrics": _scalars(r, 20)} for r in rows if isinstance(r, dict) and r.get("wallet")][:limit]


CANDIDATES = {NANSEN: nansen_candidates, MADEONSOL: madeonsol_candidates}


# --- connection test (free endpoints) -----------------------------------------------------------

async def test_connection(client: httpx.AsyncClient, provider: str, key: str | None) -> str:
    if not key:
        raise ProviderError(NOT_CONFIGURED, f"{KEY_SETTING[provider]} is not set")
    if provider == NANSEN:
        acct = await _call(client, NANSEN, key, "GET", "/api/v1/account")
        return f"Nansen account reachable ({', '.join(f'{k}={v}' for k, v in list(_scalars(acct).items())[:3]) or 'ok'})"
    me = await _call(client, MADEONSOL, key, "GET", "/me")
    daily = ((me or {}).get("quota") or {}).get("daily") or {}
    return f"MadeOnSol {me.get('tier_label') or me.get('tier') or 'account'}: {daily.get('remaining', '?')} of " \
           f"{daily.get('limit', '?')} requests left today"


# --- budget --------------------------------------------------------------------------------------

async def calls_today(redis, provider: str, now: datetime) -> int:
    v = await redis.get(BUDGET_KEY.format(provider=provider, day=now.strftime("%Y%m%d")))
    return int(v) if v else 0


async def spend(redis, provider: str, now: datetime, n: int) -> None:
    k = BUDGET_KEY.format(provider=provider, day=now.strftime("%Y%m%d"))
    await redis.incrby(k, n)
    await redis.expire(k, 3 * 86400)


async def load_settings(session) -> EnrichmentSettings:
    from yonixalpha_core.db.models import PlatformSetting

    row = await session.get(PlatformSetting, SETTINGS_KEY)
    try:
        return EnrichmentSettings.parse(row.value if row else None)
    except ValueError:
        return EnrichmentSettings()


# --- passes (run by copy-engine) -----------------------------------------------------------------

STOP_PROVIDER = {UNAUTHORIZED, PAYMENT_REQUIRED, RATE_LIMITED}  # do not keep calling a refusing provider this pass
DISCOVERY_EVERY = timedelta(hours=24)
DISCOVERY_KEY = "yx:enrich:discovery:{provider}:{chain}"


async def _wallets_to_enrich(session, cfg: EnrichmentSettings, now: datetime) -> list[tuple[str, str]]:
    """Copy targets first, then validated / paper-followed wallets, then the
    highest-scored wallets still collecting history."""
    from sqlalchemy import desc, select
    from sqlalchemy.sql.expression import nulls_last

    from yonixalpha_core.db.models import CopyTarget, WalletProfile

    out: list[tuple[str, str]] = []
    for t in (await session.execute(select(CopyTarget).order_by(CopyTarget.created_at))).scalars():
        out.append((t.chain, t.wallet))
    stage = WalletProfile.metrics["discovery"]["stage"].astext
    for stages in (("VALIDATED", "PAPER_FOLLOWED"), ("COLLECTING_HISTORY",)):
        rows = (await session.execute(select(WalletProfile.chain, WalletProfile.wallet).where(stage.in_(stages))
                                      .order_by(nulls_last(desc(WalletProfile.score)), desc(WalletProfile.last_seen))
                                      .limit(cfg.wallets_per_pass * 5))).all()
        out += [(c, w) for c, w in rows]
    return list(dict.fromkeys(out))


async def lookup_into(session, redis, client: httpx.AsyncClient, provider: str, key: str, chain: str, wallet: str,
                      now: datetime) -> str:
    """Looks one wallet up with one provider and stores the result (or the
    error) in wallet_enrichment; spends the calls made. Returns the status."""
    from yonixalpha_core.db.models import WalletEnrichment

    row = await session.get(WalletEnrichment, (chain, wallet, provider))
    if row is None:
        row = WalletEnrichment(chain=chain, wallet=wallet, provider=provider, kind="PROFILE", status=UNAVAILABLE)
        session.add(row)
    try:
        data, calls = await LOOKUP[provider](client, key, chain, wallet, now)
        await spend(redis, provider, now, calls)
        row.kind, row.status, row.data, row.error = "PROFILE", OK, {**data, "note": NOTE}, None
    except ProviderError as exc:
        await spend(redis, provider, now, 1)
        row.status, row.error = exc.status, exc.detail[:300]
    row.fetched_at = row.updated_at = now
    return row.status


async def enrich_pass(session_factory, redis, settings: Any, client: httpx.AsyncClient, now: datetime) -> dict[str, Any]:
    """Looks up at most `wallets_per_pass` wallets per provider whose record is
    missing or older than the refresh window, inside the daily budget."""
    from yonixalpha_core.db.models import WalletEnrichment

    async with session_factory() as session:
        cfg = await load_settings(session)
        if not cfg.enabled:
            return {"status": "OFF"}
        keys = {p: key_of(settings, p) for p in PROVIDERS}
        if not any(keys.values()):
            return {"status": NOT_CONFIGURED}
        wallets = await _wallets_to_enrich(session, cfg, now)
        out: dict[str, Any] = {}
        for provider in PROVIDERS:
            key = keys[provider]
            if not key:
                out[provider] = NOT_CONFIGURED
                continue
            done, stats = 0, {"looked_up": 0, "errors": 0}
            for chain, wallet in wallets:
                if done >= cfg.wallets_per_pass:
                    break
                if chain not in CHAINS[provider]:
                    continue
                row = await session.get(WalletEnrichment, (chain, wallet, provider))
                if row is not None and row.kind == "PROFILE" and row.fetched_at and \
                        now - row.fetched_at < timedelta(hours=cfg.refresh_hours):
                    continue
                if await calls_today(redis, provider, now) + 2 > cfg.budget(provider):
                    stats["stopped"] = BUDGET_EXHAUSTED
                    break
                done += 1
                status = await lookup_into(session, redis, client, provider, key, chain, wallet, now)
                if status == OK:
                    stats["looked_up"] += 1
                else:
                    stats["errors"] += 1
                    if status in STOP_PROVIDER:
                        stats["stopped"] = status
                        break
            out[provider] = stats
        await session.commit()
    return out


async def discovery_pass(session_factory, redis, settings: Any, client: httpx.AsyncClient, now: datetime) -> dict[str, Any]:
    """Once a day per provider and chain: candidate wallets from the
    provider's feed, stored as kind CANDIDATE. A candidate is never copied:
    it is listed for the operator, and becomes a profile only through its
    own trades in YonixAlpha's data and the validation gates."""
    from yonixalpha_core.db.models import WalletEnrichment

    async with session_factory() as session:
        cfg = await load_settings(session)
        if not (cfg.enabled and cfg.discovery):
            return {"status": "OFF"}
        out: dict[str, Any] = {}
        for provider in PROVIDERS:
            key = key_of(settings, provider)
            if not key:
                out[provider] = NOT_CONFIGURED
                continue
            for chain in CHAINS[provider]:
                mark = DISCOVERY_KEY.format(provider=provider, chain=chain)
                if await redis.get(mark):
                    continue
                if await calls_today(redis, provider, now) + 1 > cfg.budget(provider):
                    out[f"{provider}:{chain}"] = BUDGET_EXHAUSTED
                    continue
                await redis.set(mark, now.isoformat(), ex=int(DISCOVERY_EVERY.total_seconds()))
                await spend(redis, provider, now, 1)
                try:
                    found = await CANDIDATES[provider](client, key, chain, cfg.discovery_limit)
                except ProviderError as exc:
                    out[f"{provider}:{chain}"] = exc.detail
                    continue
                new = 0
                for c in found:
                    row = await session.get(WalletEnrichment, (chain, c["wallet"], provider))
                    if row is None:
                        session.add(WalletEnrichment(chain=chain, wallet=c["wallet"], provider=provider, kind="CANDIDATE",
                                                     status=OK, data={**c, "note": NOTE}, discovered_at=now, updated_at=now))
                        new += 1
                    elif row.discovered_at is None:
                        row.discovered_at = now
                out[f"{provider}:{chain}"] = {"found": len(found), "new": new}
        await session.commit()
    return out
