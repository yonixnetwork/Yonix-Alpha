"""HTTP adapters for migrated-token execution quotes (Jupiter Swap API v1)
and pool data (DexScreener).

Jupiter: request/response shapes from the official OpenAPI spec
(jup-ag/jupiter-quote-api-node/swagger.yaml). The free host is
lite-api.jup.ag; api.jup.ag needs an `x-api-key` header.

DexScreener field names are cross-checked only against a third-party client
(PyPI `dexscreener`), since the official docs weren't reachable from the
build environment. Parsing is defensive and any missing field reads as
unavailable.

Neither adapter has been exercised against the live APIs from the build
sandbox (egress blocked). scripts/verify-live-data.py does that on the
droplet.
"""

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import httpx

from yonixalpha_core.safety.liquidity import BPS
from yonixalpha_core.safety.models import ExecutionQuote, Observation, Venue
from yonixalpha_core.solana.pumpfun import WSOL_MINT

JUPITER_FREE_BASE = "https://lite-api.jup.ag/swap/v1"
JUPITER_KEYED_BASE = "https://api.jup.ag/swap/v1"
DEXSCREENER_BASE = "https://api.dexscreener.com"
NO_ROUTE_CODES = {"COULD_NOT_FIND_ANY_ROUTE", "TOKEN_NOT_TRADABLE", "NO_ROUTES_FOUND"}
LAMPORTS = Decimal(1_000_000_000)


class RateBudget:
    """Token bucket shared by every caller of one provider in a process, so a
    burst of candidates can't exhaust a free-tier quota."""

    def __init__(self, per_minute: int):
        self.capacity = max(per_minute, 1)
        self.tokens = float(self.capacity)
        self.rate = self.capacity / 60.0
        self.updated = time.monotonic()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
        self.updated = now

    def try_acquire(self, n: int = 1) -> bool:
        self._refill()
        if self.tokens >= n:
            self.tokens -= n
            return True
        return False

    async def acquire(self, n: int = 1, timeout: float = 30.0) -> bool:
        deadline = time.monotonic() + timeout
        async with self._lock:
            while not self.try_acquire(n):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                needed = max((n - self.tokens) / self.rate, 0.05)
                await asyncio.sleep(min(needed, remaining))
            return True


@dataclass
class QuoteResult:
    status: str  # "ok" | "no_route" | "error"
    data: dict[str, Any] | None = None
    error: str | None = None

    @property
    def out_amount(self) -> int | None:
        if self.status != "ok" or not self.data:
            return None
        try:
            return int(self.data["outAmount"])
        except (KeyError, TypeError, ValueError):
            return None

    @property
    def labels(self) -> list[str]:
        if not self.data:
            return []
        return [step.get("swapInfo", {}).get("label", "?") for step in self.data.get("routePlan", [])]


class JupiterClient:
    def __init__(self, client: httpx.AsyncClient, api_key: str | None, budget: RateBudget):
        self.client = client
        self.api_key = api_key
        self.base = JUPITER_KEYED_BASE if api_key else JUPITER_FREE_BASE
        self.budget = budget

    async def quote(self, input_mint: str, output_mint: str, amount_raw: int, slippage_bps: int) -> QuoteResult:
        if amount_raw <= 0:
            return QuoteResult("error", error="amount must be positive")
        if not await self.budget.acquire():
            return QuoteResult("error", error="jupiter rate budget exhausted")
        headers = {"x-api-key": self.api_key} if self.api_key else {}
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount_raw),
            "slippageBps": str(slippage_bps),
            "restrictIntermediateTokens": "true",
        }
        try:
            resp = await self.client.get(f"{self.base}/quote", params=params, headers=headers, timeout=10.0)
        except httpx.HTTPError as exc:
            return QuoteResult("error", error=f"transport: {exc!r}")
        if resp.status_code == 200:
            try:
                body = resp.json()
            except ValueError:
                return QuoteResult("error", error="non-JSON 200 response")
            if "outAmount" not in body or "routePlan" not in body:
                return QuoteResult("error", error="200 response missing outAmount/routePlan")
            return QuoteResult("ok", data=body)
        code = None
        try:
            code = resp.json().get("errorCode")
        except ValueError:
            pass
        if resp.status_code == 400 and code in NO_ROUTE_CODES:
            return QuoteResult("no_route", error=code)
        return QuoteResult("error", error=f"HTTP {resp.status_code} {code or resp.text[:120]}")

    async def execution_quote(
        self, mint: str, size_sol: Decimal, reference_sol: Decimal, slippage_bps: int, venue: Venue = Venue.JUPITER
    ) -> tuple[ExecutionQuote, dict[str, Any]]:
        """Four quotes: tiny buy, sized buy, sell of the sized buy's tokens,
        tiny sell. Impact is measured against the tiny quotes on the same
        side, so it's unit-free and doesn't depend on how Jupiter's own
        priceImpactPct field is scaled. Returns (quote, raw evidence)."""
        observed = datetime.now(timezone.utc)
        size_raw = int(size_sol * LAMPORTS)
        ref_raw = int(reference_sol * LAMPORTS)
        evidence: dict[str, Any] = {"size_sol": str(size_sol), "reference_sol": str(reference_sol)}

        ref_buy = await self.quote(WSOL_MINT, mint, ref_raw, slippage_bps)
        buy = await self.quote(WSOL_MINT, mint, size_raw, slippage_bps)
        evidence["buy"] = {"status": buy.status, "error": buy.error, "labels": buy.labels,
                           "priceImpactPct": (buy.data or {}).get("priceImpactPct")}
        buy_ok: bool | None = True if buy.status == "ok" else (False if buy.status == "no_route" else None)
        sell_ok: bool | None = None
        entry_impact = exit_impact = round_trip = fee = None
        entry_price = exit_price = None

        tokens_out = buy.out_amount
        ref_tokens = ref_buy.out_amount
        if buy_ok and tokens_out and ref_tokens:
            sell = await self.quote(mint, WSOL_MINT, tokens_out, slippage_bps)
            ref_sell = await self.quote(mint, WSOL_MINT, ref_tokens, slippage_bps)
            evidence["sell"] = {"status": sell.status, "error": sell.error, "labels": sell.labels}
            sell_ok = True if sell.status == "ok" else (False if sell.status == "no_route" else None)
            sol_back = sell.out_amount
            ref_sol_back = ref_sell.out_amount
            if sell_ok and sol_back and ref_sol_back:
                p_ref_buy = Decimal(ref_raw) / Decimal(ref_tokens)
                p_buy = Decimal(size_raw) / Decimal(tokens_out)
                p_ref_sell = Decimal(ref_sol_back) / Decimal(ref_tokens)
                p_sell = Decimal(sol_back) / Decimal(tokens_out)
                entry_impact = max((p_buy / p_ref_buy - 1) * BPS, Decimal(0))
                exit_impact = max((1 - p_sell / p_ref_sell) * BPS, Decimal(0))
                round_trip = (1 - Decimal(sol_back) / Decimal(size_raw)) * BPS
                # Whatever the round trip lost beyond the two measured impacts
                # is fees (DEX + Jupiter), split evenly across the two legs.
                fee = max((round_trip - entry_impact - exit_impact) / 2, Decimal(0))
                entry_price = p_buy
                exit_price = p_sell
        return (
            ExecutionQuote(
                observation=Observation("jupiter", observed),
                venue=venue,
                size_quote=size_sol,
                buy_route_available=buy_ok,
                sell_route_available=sell_ok,
                entry_impact_bps=entry_impact,
                exit_impact_bps=exit_impact,
                round_trip_loss_bps=round_trip,
                fee_bps_per_side=fee if fee is not None else Decimal(0),
                expected_entry_price=entry_price,
                expected_exit_price=exit_price,
            ),
            evidence,
        )


@dataclass
class PoolData:
    observed_at: datetime
    pair_address: str
    dex_id: str
    price_sol: Decimal | None
    liquidity_sol: Decimal | None
    liquidity_usd: Decimal | None
    base_reserve: Decimal | None
    quote_reserve: Decimal | None
    buys_m5: int | None
    sells_m5: int | None
    buys_h1: int | None
    sells_h1: int | None
    volume_h1_usd: Decimal | None
    pair_created_at: datetime | None


def _dec(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except ArithmeticError:
        return None


def parse_dexscreener_pairs(body: Any, mint: str, observed_at: datetime) -> PoolData | None:
    """Picks the SOL-quoted Solana pair for `mint` with the most liquidity.
    Returns None if there is none — no pool is not the same as a small one."""
    pairs = (body or {}).get("pairs") if isinstance(body, dict) else body
    if not isinstance(pairs, list):
        return None
    best = None
    for p in pairs:
        if not isinstance(p, dict) or p.get("chainId") != "solana":
            continue
        if (p.get("baseToken") or {}).get("address") != mint:
            continue
        if (p.get("quoteToken") or {}).get("address") != WSOL_MINT:
            continue
        liq = _dec((p.get("liquidity") or {}).get("usd")) or Decimal(0)
        if best is None or liq > best[0]:
            best = (liq, p)
    if best is None:
        return None
    p = best[1]
    liquidity = p.get("liquidity") or {}
    txns = p.get("txns") or {}
    created_ms = p.get("pairCreatedAt")
    return PoolData(
        observed_at=observed_at,
        pair_address=p.get("pairAddress", ""),
        dex_id=p.get("dexId", ""),
        price_sol=_dec(p.get("priceNative")),
        liquidity_sol=_dec(liquidity.get("quote")),
        liquidity_usd=_dec(liquidity.get("usd")),
        base_reserve=_dec(liquidity.get("base")),
        quote_reserve=_dec(liquidity.get("quote")),
        buys_m5=(txns.get("m5") or {}).get("buys"),
        sells_m5=(txns.get("m5") or {}).get("sells"),
        buys_h1=(txns.get("h1") or {}).get("buys"),
        sells_h1=(txns.get("h1") or {}).get("sells"),
        volume_h1_usd=_dec((p.get("volume") or {}).get("h1")),
        pair_created_at=datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc) if isinstance(created_ms, (int, float)) else None,
    )


class DexScreenerClient:
    def __init__(self, client: httpx.AsyncClient, budget: RateBudget):
        self.client = client
        self.budget = budget

    async def pool(self, mint: str) -> tuple[PoolData | None, str | None]:
        if not await self.budget.acquire():
            return None, "dexscreener rate budget exhausted"
        try:
            resp = await self.client.get(f"{DEXSCREENER_BASE}/latest/dex/tokens/{mint}", timeout=10.0)
        except httpx.HTTPError as exc:
            return None, f"transport: {exc!r}"
        if resp.status_code != 200:
            return None, f"HTTP {resp.status_code}"
        try:
            body = resp.json()
        except ValueError:
            return None, "non-JSON response"
        return parse_dexscreener_pairs(body, mint, datetime.now(timezone.utc)), None
