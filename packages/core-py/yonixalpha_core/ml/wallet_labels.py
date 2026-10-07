"""How wallets trade: one labelled observation per wallet entry (master §36-37; M12b).

Every buy episode of a profiled BSC / Robinhood wallet (its first buy of a
token, then its sells) becomes an observation, labelled from what the token
did afterwards:

  SUCCESSFUL_ENTRY_PATTERN  the price reached +50 % over the wallet's entry
                            within an hour, before any -50 %
  FAILED_ENTRY_PATTERN      the price fell 50 % below the entry first (or
                            never reached +50 %)
  LATE_ENTRY                the wallet bought at 3x or more the token's first
                            traded price (after the move; needs the token's
                            first trade in the retained history)
  PREMATURE_EXIT            within an hour after the wallet's last sell, the
                            price doubled from that sell price
  LATE_EXIT                 the token was up 2x or more over the entry while
                            the wallet held, and the wallet sold at half that
                            peak or less (or still holds after such a fall)
  MISSED_WINNER             (separate rows, copy targets and validated wallets
                            only) a token of a launchpad the wallet was trading
                            that hour doubled within its first hour, and the
                            wallet never bought it

Features are taken at the entry: the token's age, its multiple over its first
price, buyers / trades / volume before the entry, the wallet's buy size, the
wallet's record from its EARLIER episodes whose outcome was already known at
this entry, chain and launchpad. Wallet identities are never features (the
model learns patterns, not "wallet X bought"). Curves quoted in another token
are left out (amounts not in BNB / ETH). Review data: shadow models only.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

FEATURE_VERSION = "walletfeat-2026.10.1"
LABEL_VERSION = "walletlabel-2026.10.1"
HORIZON = timedelta(minutes=60)
SETTLE = timedelta(hours=24)  # a token is labelled once, a day after its launch (most of its trading is over)
SUCCESS_UP, FAIL_DOWN = 1.5, 0.5
LATE_ENTRY_MULTIPLE = 3.0
PREMATURE_UP = 2.0
LATE_EXIT_PEAK, LATE_EXIT_GIVEBACK = 2.0, 0.5
MISSED_WINNER_UP = 2.0
SUCCESSFUL, FAILED, LATE_ENTRY, PREMATURE, LATE_EXIT, MISSED = (
    "SUCCESSFUL_ENTRY_PATTERN", "FAILED_ENTRY_PATTERN", "LATE_ENTRY", "PREMATURE_EXIT", "LATE_EXIT", "MISSED_WINNER")
LABELS = (SUCCESSFUL, FAILED, LATE_ENTRY, PREMATURE, LATE_EXIT, MISSED)
CHAINS = ("bsc", "robinhood")
LAUNCHPADS = ("fourmeme", "flap", "pons_v2", "pons_v1", "genius_fun")
NUMERIC = ("token_age_s", "entry_multiple", "buyers_before", "trades_before", "buy_volume_before", "sell_volume_before",
           "entry_size", "prior_episodes", "prior_success_rate")
FEATURE_NAMES: tuple[str, ...] = NUMERIC + tuple(f"{n}__missing" for n in NUMERIC) + \
    tuple(f"chain_{c}" for c in CHAINS) + tuple(f"lp_{lp}" for lp in LAUNCHPADS)
E18 = Decimal(10) ** 18


class Trade:
    __slots__ = ("at", "holder", "is_buy", "price", "quote")

    def __init__(self, at: datetime, holder: str, is_buy: bool, price: float, quote: float):
        self.at, self.holder, self.is_buy, self.price, self.quote = at, holder, is_buy, price, quote


def episode_labels(entry: Trade, wallet_trades: list[Trade], token_trades: list[Trade], first_price: float | None,
                   now: datetime) -> dict[str, Any]:
    """Labels and outcome numbers of one wallet's episode in one token.
    token_trades: every trade of the token, time-ordered (after the entry
    included); wallet_trades: this wallet's trades of it."""
    pe = entry.price
    after = [t for t in token_trades if entry.at < t.at <= entry.at + HORIZON]
    out: dict[str, Any] = {"labels": [], "entry_price": pe}
    first_up = next((t.at for t in after if t.price >= SUCCESS_UP * pe), None)
    first_down = next((t.at for t in after if t.price <= FAIL_DOWN * pe), None)
    out["max_return_60m_pct"] = round((max((t.price for t in after), default=pe) / pe - 1) * 100, 4)
    out["min_return_60m_pct"] = round((min((t.price for t in after), default=pe) / pe - 1) * 100, 4)
    success = first_up is not None and (first_down is None or first_up < first_down)
    out["labels"].append(SUCCESSFUL if success else FAILED)
    out["successful_entry"] = success
    if first_price:
        out["entry_multiple"] = round(pe / first_price, 4)
        if pe >= LATE_ENTRY_MULTIPLE * first_price:
            out["labels"].append(LATE_ENTRY)
    sells = [t for t in wallet_trades if not t.is_buy and t.at > entry.at]
    if sells:
        last = sells[-1]
        out["exit_price"], out["exit_at"] = last.price, last.at.isoformat()
        post = [t.price for t in token_trades if last.at < t.at <= last.at + HORIZON]
        if post and max(post) >= PREMATURE_UP * last.price:
            out["labels"].append(PREMATURE)
        held = [t.price for t in token_trades if entry.at <= t.at <= last.at]
        peak = max(held, default=pe)
        if peak >= LATE_EXIT_PEAK * pe and last.price <= LATE_EXIT_GIVEBACK * peak:
            out["labels"].append(LATE_EXIT)
    else:
        held = [t.price for t in token_trades if t.at >= entry.at]
        peak = max(held, default=pe)
        if held and peak >= LATE_EXIT_PEAK * pe and held[-1] <= LATE_EXIT_GIVEBACK * peak:
            out["labels"].append(LATE_EXIT)
            out["still_holding"] = True
    out["available_at"] = (entry.at + HORIZON).isoformat()
    return out


def entry_features(entry: Trade, token_trades: list[Trade], first_at: datetime | None, first_price: float | None,
                   prior: list[dict[str, Any]], chain: str, launchpad: str,
                   prior_known: tuple[int, int] | None = None) -> dict[str, Any]:
    """Features at the entry: only trades before it, and only earlier
    episodes whose outcome was known at the entry (available_at <= entry).
    prior_known: (episodes, successful) already counted that way (builder)."""
    before = [t for t in token_trades if t.at < entry.at]
    if prior_known is None:
        known = [p for p in prior if datetime.fromisoformat(p["available_at"]) <= entry.at]
        prior_known = (len(known), sum(1 for p in known if p.get("successful_entry")))
    n_prior, n_success = prior_known
    x: dict[str, Any] = {
        "token_age_s": (entry.at - first_at).total_seconds() if first_at else None,
        "entry_multiple": entry.price / first_price if first_price else None,
        "buyers_before": float(len({t.holder for t in before if t.is_buy})),
        "trades_before": float(len(before)),
        "buy_volume_before": sum(t.quote for t in before if t.is_buy),
        "sell_volume_before": sum(t.quote for t in before if not t.is_buy),
        "entry_size": entry.quote,
        "prior_episodes": float(n_prior),
        "prior_success_rate": n_success / n_prior if n_prior else None,
    }
    for n in NUMERIC:
        x[f"{n}__missing"] = 1.0 if x.get(n) is None else 0.0
    for c in CHAINS:
        x[f"chain_{c}"] = 1.0 if chain == c else 0.0
    for lp in LAUNCHPADS:
        x[f"lp_{lp}"] = 1.0 if launchpad == lp else 0.0
    return x


def missed_winners(wallet: str, wallet_tokens: set[str], active: list[tuple[datetime, str]],
                   winners: list[tuple[str, str, datetime]]) -> list[tuple[str, str, datetime]]:
    """Winners (token, launchpad, launch time) launched while the wallet was
    trading that launchpad (a trade within an hour either side) that the
    wallet never bought."""
    out = []
    for token, lp, launched in winners:
        if token in wallet_tokens:
            continue
        if any(alp == lp and abs((at - launched).total_seconds()) <= HORIZON.total_seconds() for at, alp in active):
            out.append((token, lp, launched))
    return out


# --- builder (ml service) ------------------------------------------------------------------------

async def build(session, now: datetime, token_limit: int = 200) -> dict[str, int]:
    """Labels the episodes of profiled (non-contract) wallets in tokens
    launched at least SETTLE ago (and still within trade retention), a batch
    of tokens per pass, each token once. Idempotent."""
    from sqlalchemy import func, select
    from sqlalchemy.dialects.postgresql import insert

    from yonixalpha_core.chains.evm import address_kinds
    from yonixalpha_core.chains.evm.store import NATIVE_QUOTES
    from yonixalpha_core.db.models import EvmToken, EvmTrade, WalletProfile, WalletTradeLabel

    retention_start = now - timedelta(days=14)
    e = EvmTrade
    holder = func.lower(func.coalesce(e.extra["recipient"].astext, e.trader))
    profiled = {(c, w.lower()) for c, w in (await session.execute(select(WalletProfile.chain, WalletProfile.wallet).where(
        WalletProfile.chain.in_(CHAINS),
        func.coalesce(WalletProfile.metrics["account"]["kind"].astext, "") != address_kinds.CONTRACT))).all()}
    if not profiled:
        return {"tokens": 0, "episodes": 0}
    # processed = an EPISODE or the NONE marker; a MISSED row (written for
    # winners younger than SETTLE) does not mean the token's episodes exist
    done = select(WalletTradeLabel.token).where(WalletTradeLabel.chain == EvmToken.chain,
                                                WalletTradeLabel.token == EvmToken.token,
                                                WalletTradeLabel.kind.in_(("EPISODE", "NONE"))).exists()
    toks = (await session.execute(select(EvmToken).where(
        EvmToken.chain.in_(CHAINS), EvmToken.created_at >= retention_start, EvmToken.created_at <= now - SETTLE,
        func.coalesce(func.lower(EvmToken.quote_token), NATIVE_QUOTES[0]).in_(NATIVE_QUOTES), ~done)
        .order_by(EvmToken.created_at).limit(token_limit))).scalars().all()
    out = {"tokens": 0, "episodes": 0}
    for tok in toks:
        rows = (await session.execute(select(e.at, holder, e.is_buy, e.quote_amount, e.token_amount).where(
            e.chain == tok.chain, e.token == tok.token, e.token_amount > 0).order_by(e.at))).all()
        trades = [Trade(at, h, b, float(Decimal(q) / Decimal(a)), float(Decimal(q) / E18)) for at, h, b, q, a in rows]
        first_ok = tok.created_at >= retention_start and trades and \
            (trades[0].at - tok.created_at).total_seconds() <= 600
        first_at = trades[0].at if first_ok else None
        first_price = trades[0].price if first_ok else None
        by_wallet: dict[str, list[Trade]] = defaultdict(list)
        for t in trades:
            by_wallet[t.holder].append(t)
        wrote = False
        for w, wt in by_wallet.items():
            if (tok.chain, w) not in profiled:
                continue
            entry = next((t for t in wt if t.is_buy), None)
            if entry is None or entry.at > now - HORIZON:
                continue
            # an episode's outcome is known HORIZON after its entry (available_at),
            # so "known at this entry" is entry_at <= entry - HORIZON; counted in SQL
            n_prior, n_success = (await session.execute(select(
                func.count(), func.count().filter(WalletTradeLabel.outcome["successful_entry"].as_boolean())).where(
                WalletTradeLabel.chain == tok.chain, WalletTradeLabel.wallet == w, WalletTradeLabel.kind == "EPISODE",
                WalletTradeLabel.entry_at <= entry.at - HORIZON))).one()
            res = episode_labels(entry, wt, trades, first_price, now)
            await session.execute(insert(WalletTradeLabel).values(
                chain=tok.chain, wallet=w, token=tok.token, kind="EPISODE", launchpad=tok.launchpad, entry_at=entry.at,
                labels=res["labels"], outcome={k: v for k, v in res.items() if k != "labels"},
                features=entry_features(entry, trades, first_at, first_price, [], tok.chain, tok.launchpad,
                                        prior_known=(n_prior, n_success)),
                feature_version=FEATURE_VERSION, label_version=LABEL_VERSION, created_at=now)
                .on_conflict_do_nothing(index_elements=["chain", "wallet", "token"]))
            out["episodes"] += 1
            wrote = True
        if not wrote:
            # a token none of the profiled wallets traded: a marker row keeps it from being re-read every pass
            await session.execute(insert(WalletTradeLabel).values(
                chain=tok.chain, wallet="-", token=tok.token, kind="NONE", launchpad=tok.launchpad,
                entry_at=tok.created_at, labels=[], outcome={}, features={}, feature_version=FEATURE_VERSION,
                label_version=LABEL_VERSION, created_at=now).on_conflict_do_nothing(index_elements=["chain", "wallet", "token"]))
        out["tokens"] += 1
    return out


async def build_missed(session, now: datetime, days: int = 7) -> int:
    """MISSED_WINNER rows for copy targets and validated wallets."""
    from sqlalchemy import func, select
    from sqlalchemy.dialects.postgresql import insert

    from yonixalpha_core.db.models import CopyTarget, EvmTrade, WalletProfile, WalletTradeLabel

    since = now - timedelta(days=days)
    wallets = {(t.chain, t.wallet.lower()) for t in (await session.execute(select(CopyTarget).where(
        CopyTarget.chain.in_(CHAINS)))).scalars()}
    wallets |= {(c, w.lower()) for c, w in (await session.execute(select(WalletProfile.chain, WalletProfile.wallet).where(
        WalletProfile.chain.in_(CHAINS),
        WalletProfile.metrics["discovery"]["stage"].astext.in_(("VALIDATED", "PAPER_FOLLOWED"))))).all()}
    if not wallets:
        return 0
    from sqlalchemy.dialects.postgresql import aggregate_order_by

    from yonixalpha_core.chains.evm.store import NATIVE_QUOTES
    from yonixalpha_core.db.models import EvmToken

    # one grouped query: each token's first traded price and its highest price in its first hour
    price = EvmTrade.quote_amount / EvmTrade.token_amount
    first = func.array_agg(aggregate_order_by(price, EvmTrade.at))[1]
    rows = (await session.execute(select(EvmToken.chain, EvmToken.token, EvmToken.launchpad, EvmToken.created_at,
                                         first, func.max(price)).join(
        EvmTrade, (EvmTrade.chain == EvmToken.chain) & (EvmTrade.token == EvmToken.token)).where(
        EvmToken.chain.in_(CHAINS), EvmToken.created_at >= since, EvmToken.created_at <= now - HORIZON,
        func.coalesce(func.lower(EvmToken.quote_token), NATIVE_QUOTES[0]).in_(NATIVE_QUOTES),
        EvmTrade.token_amount > 0, EvmTrade.at <= EvmToken.created_at + HORIZON)
        .group_by(EvmToken.chain, EvmToken.token, EvmToken.launchpad, EvmToken.created_at))).all()
    winners: dict[str, list[tuple[str, str, datetime]]] = defaultdict(list)
    for chain, token, lp, created, p_first, p_max in rows:
        if p_first and p_max is not None and Decimal(p_max) >= Decimal(str(MISSED_WINNER_UP)) * Decimal(p_first):
            winners[chain].append((token, lp, created))
    holder = func.lower(func.coalesce(EvmTrade.extra["recipient"].astext, EvmTrade.trader))
    # one pass over the window per chain for all wallets (server 2026-10-07:
    # one query per wallet read 7 days of evm_trades each time, for an hour)
    acts_of: dict[tuple[str, str], list] = defaultdict(list)
    for chain in sorted({c for c, _ in wallets}):
        ws = sorted(w for c, w in wallets if c == chain)
        for h, at, lp, token in (await session.execute(select(holder, EvmTrade.at, EvmTrade.launchpad, EvmTrade.token).where(
                EvmTrade.chain == chain, EvmTrade.at >= since, holder.in_(ws)))).all():
            acts_of[(chain, h)].append((at, lp, token))
    n = 0
    for chain, w in wallets:
        acts = acts_of.get((chain, w))
        if not acts:
            continue
        for token, lp, launched in missed_winners(w, {t for _, _, t in acts}, [(a, lp_) for a, lp_, _ in acts],
                                                  winners.get(chain, [])):
            res = await session.execute(insert(WalletTradeLabel).values(
                chain=chain, wallet=w, token=token, kind="MISSED", launchpad=lp, entry_at=launched, labels=[MISSED],
                outcome={"launched_at": launched.isoformat(), "rule": f"doubled within its first hour; wallet active on {lp}"},
                features={}, feature_version=FEATURE_VERSION, label_version=LABEL_VERSION, created_at=now)
                .on_conflict_do_nothing(index_elements=["chain", "wallet", "token"]))
            n += res.rowcount or 0
    return n
