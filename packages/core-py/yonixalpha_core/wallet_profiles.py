"""Chain-agnostic wallet profiles from trades this system observed.

A profile is a set of measured metrics, descriptive behaviour labels
(SNIPER, SCALPER, HOLDER, HIGH_ACTIVITY, POSSIBLE_BOT) and a configurable
score with its components shown. There is deliberately no "best wallet"
label and no overall winner ranking: the API sorts by whichever metric the
operator picks, and a wallet with too few closed trades has no score
(INSUFFICIENT_DATA) rather than a lucky high one.

Sources:
- BSC / Robinhood Chain: evm_trades (every decoded launchpad / pool trade).
- Solana: launch_buyers (early buyers of launches the system decided on,
  with their resolved launch outcome) — a narrower view than a full trade
  history, stated in each profile's `source`.

Win rate is shrunk toward a base rate with a Beta prior (the same approach
as wallet_intel.beta_reputation), so a 2-for-2 wallet does not look better
than a 60-for-100 one.

EVM profiles also carry (master upgrade §25-28): the validation checks
(wallet_validation), the market-regime test (market_regimes), the discovery
stage, and for VALIDATED wallets a paper follow: their recent buys replayed
with copy_outcomes.evaluate (entry at the first trade after we would have
seen theirs, exit at their own sell or at the horizon). Nothing here adds a
copy target.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Iterable

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from yonixalpha_core import copy_outcomes, market_regimes, wallet_pnl, wallet_validation
from yonixalpha_core.db.models import EvmToken, EvmTrade, LaunchBuyer, PlatformSetting, WalletProfile

E18 = Decimal(10) ** 18


@dataclass(frozen=True)
class ScoreConfig:
    min_closed: int = 5  # fewer closed positions: no score
    prior_strength: float = 10.0
    base_win_rate: float = 0.35
    w_win_rate: float = 0.45
    w_pnl: float = 0.25
    w_early: float = 0.10
    w_sample: float = 0.20
    pnl_scale: float = 1.0  # native units at which the pnl component reaches ~0.76 (tanh)
    early_seconds: int = 60


def _labels(m: dict[str, Any]) -> list[str]:
    out = []
    if m["tokens_with_launch"] >= 5 and (m["early_entry_share"] or 0) >= 0.5:
        out.append("SNIPER")
    if m["avg_hold_s"] is not None and m["avg_hold_s"] < 300:
        out.append("SCALPER")
    if m["avg_hold_s"] is not None and m["avg_hold_s"] > 3600:
        out.append("HOLDER")
    if m["trades"] >= 100:
        out.append("HIGH_ACTIVITY")
    if m["median_gap_s"] is not None and m["median_gap_s"] < 3 and m["trades"] >= 50:
        out.append("POSSIBLE_BOT")
    return out


def score(m: dict[str, Any], cfg: ScoreConfig) -> tuple[float | None, dict[str, Any]]:
    n, wins = m["closed_tokens"], m["wins"]
    if n < cfg.min_closed:
        return None, {"status": "INSUFFICIENT_DATA", "closed": n, "min_closed": cfg.min_closed}
    a = cfg.base_win_rate * cfg.prior_strength
    shrunk = (wins + a) / (n + cfg.prior_strength)
    pnl_c = 0.5 + 0.5 * math.tanh(float(m["realized_pnl"]) / cfg.pnl_scale)
    early_c = float(m["early_entry_share"] or 0)
    sample_c = 1 - math.exp(-n / 20)
    comps = {"win_rate_shrunk": round(shrunk, 4), "pnl": round(pnl_c, 4), "early_entry": round(early_c, 4),
             "sample_size": round(sample_c, 4)}
    total = (cfg.w_win_rate * shrunk + cfg.w_pnl * pnl_c + cfg.w_early * early_c + cfg.w_sample * sample_c)
    weights = cfg.w_win_rate + cfg.w_pnl + cfg.w_early + cfg.w_sample
    return round(total / weights, 4), {"status": "SCORED", "components": comps, "weights": asdict(cfg)}


def evm_metrics(trades: Iterable[Any], launch_at: dict[str, datetime], cfg: ScoreConfig,
                now: datetime | None = None, history_days: float = 7,
                vcfg: wallet_validation.ValidationConfig | None = None,
                regimes: dict[datetime, dict[str, str]] | None = None) -> dict[str, Any]:
    """trades: one wallet's EvmTrade rows (any order). launch_at: token ->
    observed launch time (only tokens whose launch was seen). `pnl` is the
    FIFO profit/loss profile (wallet_pnl) over the same trades; `validation`
    and `regimes` are the §26 / §28 tests over its closed trades."""
    rows = sorted(trades, key=lambda t: t.at)
    per: dict[str, dict[str, Any]] = defaultdict(lambda: {"bq": Decimal(0), "bt": Decimal(0), "sq": Decimal(0),
                                                          "st": Decimal(0), "first_buy": None, "last_sell": None})
    buy_sizes = []
    for t in rows:
        p = per[t.token]
        if t.is_buy:
            p["bq"] += Decimal(t.quote_amount)
            p["bt"] += Decimal(t.token_amount)
            p["first_buy"] = p["first_buy"] or t.at
            buy_sizes.append(Decimal(t.quote_amount) / E18)
        else:
            p["sq"] += Decimal(t.quote_amount)
            p["st"] += Decimal(t.token_amount)
            p["last_sell"] = t.at
    closed = pnl = wins = 0
    holds = []
    realized = Decimal(0)
    for p in per.values():
        if p["bt"] > 0 and p["st"] >= p["bt"] * Decimal("0.9"):
            closed += 1
            cost = p["bq"] * min(Decimal(1), p["st"] / p["bt"])
            r = (p["sq"] - cost) / E18
            realized += r
            wins += 1 if r > 0 else 0
            if p["first_buy"] and p["last_sell"]:
                holds.append((p["last_sell"] - p["first_buy"]).total_seconds())
    with_launch = [(tok, p) for tok, p in per.items() if tok in launch_at and p["first_buy"]]
    early = sum(1 for tok, p in with_launch if (p["first_buy"] - launch_at[tok]).total_seconds() <= cfg.early_seconds)
    gaps = [(b.at - a.at).total_seconds() for a, b in zip(rows, rows[1:])]
    pnl = realized
    ledger_in = [wallet_pnl.TradeIn(t.token, t.at, t.is_buy, Decimal(t.token_amount), Decimal(t.quote_amount) / E18,
                                    Decimal(getattr(t, "fee", None) or 0) / E18) for t in rows]
    closed_trades = wallet_pnl.fifo(ledger_in).closed
    validation = wallet_validation.validate(closed_trades, trades=len(rows), unique_tokens=len(per),
                                            trade_times=[t.at for t in rows], cfg=vcfg or wallet_validation.ValidationConfig())
    return {
        "validation": validation,
        "regimes": market_regimes.regime_test(closed_trades, regimes or {}),
        "discovery": wallet_validation.discovery_status(validation, None),
        "pnl": wallet_pnl.profile(ledger_in, now or (rows[-1].at if rows else datetime.now()), history_days),
        "trades": len(rows), "buys": sum(1 for t in rows if t.is_buy), "sells": sum(1 for t in rows if not t.is_buy),
        "tokens": len(per), "closed_tokens": closed, "wins": wins,
        "win_rate": round(wins / closed, 4) if closed else None, "realized_pnl": pnl,
        "volume": sum((Decimal(t.quote_amount) for t in rows), Decimal(0)) / E18,
        "median_buy": statistics.median(buy_sizes) if buy_sizes else None,
        "avg_hold_s": round(sum(holds) / len(holds), 1) if holds else None,
        "tokens_with_launch": len(with_launch),
        "early_entry_share": round(early / len(with_launch), 4) if with_launch else None,
        "median_gap_s": statistics.median(gaps) if gaps else None,
        "first_seen": rows[0].at if rows else None, "last_seen": rows[-1].at if rows else None,
    }


def solana_metrics(rows: Iterable[LaunchBuyer], cfg: ScoreConfig) -> dict[str, Any]:
    rows = sorted(rows, key=lambda r: r.first_buy_at)
    resolved = [r for r in rows if r.outcome is not None]
    wins = sum(1 for r in resolved if r.outcome == "WIN")
    with_launch = [r for r in rows if r.launch_created_at]
    early = sum(1 for r in with_launch if (r.first_buy_at - r.launch_created_at).total_seconds() <= cfg.early_seconds)
    no_sells = ("Solana profiles come from launch_buyers (first buys only): without the wallet's sells there is no "
                "closed-trade ledger to validate or to split by market regime")
    validation = {"status": wallet_validation.INSUFFICIENT, "reason": no_sells, "checks": []}
    return {
        "validation": validation,
        "regimes": {"status": "INSUFFICIENT_DATA", "reason": no_sells, "dimensions": {}},
        "discovery": wallet_validation.discovery_status(validation, None),
        "pnl": {"all": {"status": "INSUFFICIENT_DATA", "closed_trades": None, "reasons": [
            "Solana profiles come from launch_buyers: the first buys of launches this system decided on. "
            "Their sells are not recorded, so per-trade PnL, wins / losses and profit factor cannot be computed; "
            "win rate below is the launch outcome (WIN / LOSS), not the wallet's own result"]},
                "windows": {}, "notes": [], "cost_basis": None},
        "trades": len(rows), "buys": len(rows), "sells": None, "tokens": len({r.mint for r in rows}),
        "closed_tokens": len(resolved), "wins": wins, "win_rate": round(wins / len(resolved), 4) if resolved else None,
        "realized_pnl": Decimal(0), "volume": sum((r.sol_in for r in rows), Decimal(0)),
        "median_buy": statistics.median([r.sol_in for r in rows]) if rows else None, "avg_hold_s": None,
        "tokens_with_launch": len(with_launch), "early_entry_share": round(early / len(with_launch), 4) if with_launch else None,
        "median_gap_s": None, "sold_early_share": round(sum(1 for r in rows if r.sold_early) / len(rows), 4) if rows else None,
        "first_seen": rows[0].first_buy_at if rows else None, "last_seen": rows[-1].first_buy_at if rows else None,
    }


def _js(v: Any) -> Any:
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, dict):
        return {k: _js(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_js(x) for x in v]
    return v


async def _upsert(session: AsyncSession, chain: str, wallet: str, m: dict[str, Any], source: str, cfg: ScoreConfig,
                  now: datetime) -> None:
    sc, detail = score(m, cfg)
    values = dict(chain=chain, wallet=wallet, metrics=_js(m), labels=_labels(m) if source != "launch_buyers" else
                  [x for x in _labels(m) if x == "SNIPER"], score=sc, score_detail=_js(detail), source=source,
                  trades=m["trades"], tokens=m["tokens"], first_seen=m["first_seen"], last_seen=m["last_seen"], updated_at=now)
    stmt = insert(WalletProfile).values(**values)
    await session.execute(stmt.on_conflict_do_update(index_elements=["chain", "wallet"],
                                                     set_={k: stmt.excluded[k] for k in values if k not in ("chain", "wallet")}))


MAX_WALLETS = 2000  # most active wallets profiled per chain and rebuild
WALLET_BATCH = 100  # wallets whose trades are loaded at once (bounded memory)


PAPER_FOLLOW_BUYS = 20  # most recent first buys per token replayed for a VALIDATED wallet
PAPER_FOLLOW_REFRESH = timedelta(hours=1)
ASSUMED_DETECTION = timedelta(seconds=3)  # paper follow: we see a confirmed trade a few seconds after it


async def validation_config(session: AsyncSession) -> wallet_validation.ValidationConfig:
    row = await session.get(PlatformSetting, wallet_validation.SETTINGS_KEY)
    cfg, _errors = wallet_validation.parse_config(dict(row.value) if row else None)
    return cfg


async def paper_follow(session: AsyncSession, chain: str, wallet: str, rows: list, now: datetime) -> dict[str, Any]:
    """Replays the wallet's recent first buys per token as paper copies."""
    first_buys: dict[str, Any] = {}
    for t in sorted(rows, key=lambda t: t.at):
        if t.is_buy and t.token not in first_buys and t.at <= now - copy_outcomes.HORIZON - ASSUMED_DETECTION:
            first_buys[t.token] = t
    picked = sorted(first_buys.values(), key=lambda t: t.at)[-PAPER_FOLLOW_BUYS:]
    results = []
    e = EvmTrade
    for b in picked:
        seen = b.at + ASSUMED_DETECTION
        path_rows = (await session.execute(select(e.at, e.quote_amount, e.token_amount, e.trader, e.is_buy).where(
            e.chain == chain, e.token == b.token, e.at >= seen, e.at <= seen + copy_outcomes.HORIZON).order_by(e.at))).all()
        path = [copy_outcomes.Point(at, px) for at, q, tok, _, _ in path_rows
                if (px := copy_outcomes.unit_price(chain, q, tok)) is not None]
        sell = next(((at, q, tok) for at, q, tok, trader, is_buy in path_rows
                     if not is_buy and trader.lower() == wallet.lower()), None)
        exit_ = None
        if sell is not None and (px := copy_outcomes.unit_price(chain, sell[1], sell[2])) is not None:
            exit_ = copy_outcomes.Point(sell[0], px)
        results.append(copy_outcomes.evaluate(seen, path, exit_))
    done = [r for r in results if r["status"] == "EVALUATED"]
    res = sorted(r["result_pct"] for r in done)
    n = len(res)
    return {"buys_replayed": len(results), "evaluated": n, "no_price_data": len(results) - n,
            "won": sum(1 for r in done if r["label"] == "WOULD_HAVE_WON"),
            "lost": sum(1 for r in done if r["label"] == "WOULD_HAVE_LOST"),
            "avg_result_pct": round(sum(res) / n, 2) if n else None,
            "median_result_pct": round(statistics.median(res), 2) if n else None,
            "assumed_detection_s": ASSUMED_DETECTION.total_seconds(), "horizon_min": copy_outcomes.HORIZON.total_seconds() / 60,
            "basis": copy_outcomes.BASIS, "at": now.isoformat()}


async def rebuild_evm(session: AsyncSession, chain: str, now: datetime, cfg: ScoreConfig = ScoreConfig(),
                      days: int = 14, min_trades: int = 3, max_wallets: int = MAX_WALLETS) -> int:
    """Profiles the most active wallets over the retained trade history
    (14 days). Trades are loaded per batch of wallets, never the whole chain
    at once: BSC alone records close to a million launchpad trades a day."""
    since = now - timedelta(days=days)
    t = EvmTrade
    vcfg = await validation_config(session)
    await market_regimes.update(session, chain, now, timedelta(days=days))
    regimes = await market_regimes.load_regimes(session, chain, since)
    wallets = [w for w, in (await session.execute(select(t.trader).where(t.chain == chain, t.at >= since)
                                                   .group_by(t.trader).having(func.count() >= min_trades)
                                                   .order_by(func.count().desc()).limit(max_wallets))).all()]
    launches = dict((await session.execute(select(EvmToken.token, EvmToken.created_at).where(
        EvmToken.chain == chain, EvmToken.created_block.is_not(None)))).all())
    n = 0
    for i in range(0, len(wallets), WALLET_BATCH):
        batch = wallets[i:i + WALLET_BATCH]
        by_wallet: dict[str, list] = defaultdict(list)
        for row in (await session.execute(select(t).where(t.chain == chain, t.at >= since, t.trader.in_(batch)))).scalars():
            by_wallet[row.trader].append(row)
        prior = {w: m for w, m in (await session.execute(select(WalletProfile.wallet, WalletProfile.metrics).where(
            WalletProfile.chain == chain, WalletProfile.wallet.in_(list(by_wallet))))).all()}
        for wallet, rows in by_wallet.items():
            m = evm_metrics(rows, launches, cfg, now, days, vcfg, regimes)
            if m["validation"]["status"] == wallet_validation.VALIDATED:
                pf = (prior.get(wallet) or {}).get("paper_follow")
                fresh = pf and pf.get("at") and now - datetime.fromisoformat(pf["at"]) < PAPER_FOLLOW_REFRESH
                m["paper_follow"] = pf if fresh else await paper_follow(session, chain, wallet, rows, now)
            m["discovery"] = wallet_validation.discovery_status(m["validation"], m.get("paper_follow"))
            await _upsert(session, chain, wallet, m, "evm_trades", cfg, now)
            n += 1
        session.expunge_all()
    return n


async def rebuild_solana(session: AsyncSession, now: datetime, cfg: ScoreConfig = ScoreConfig(), days: int = 30,
                         min_rows: int = 3) -> int:
    rows = (await session.execute(select(LaunchBuyer).where(LaunchBuyer.first_buy_at >= now - timedelta(days=days)))).scalars().all()
    by_wallet: dict[str, list] = defaultdict(list)
    for r in rows:
        by_wallet[r.wallet].append(r)
    n = 0
    for wallet, rs in by_wallet.items():
        if len(rs) < min_rows:
            continue
        await _upsert(session, "solana", wallet, solana_metrics(rs, cfg), "launch_buyers", cfg, now)
        n += 1
    return n
