# Paper trading

Phase 7 adds a real simulated-execution engine: it can open a paper
position, mark it against live prices, close it on a stop-loss/take-profit
hit, and — the piece that matters most — backfill `docs/ML.md`'s
`ml_features.label` column with the real outcome. It is honestly expected
to never open a single position in production, because every path into it
is still blocked by the same root cause every phase since 5 has run into:
**there is no Solana price feed anywhere in this codebase.**

## Why paper trading structurally can't open a position today

A `TradingCandidate` only reaches `QUALIFIED` — the state
`services/paper-trading` watches for — if `decision-engine` both scores it
`LONG` and risk approves it. Per Phase 5/6, that's already all but
impossible (DEGRADED-data confidence is capped below the entry threshold).
But paper trading adds two more independent gates on top, and **both
always fail today regardless of the first**:

1. **No entry price.** `decision-engine` never populates `Decision.entry`
   for any decision type, including `LONG` — see
   `services/decision-engine/app/evaluate.py`: `entry=None,  # no Solana
   price feed exists to set a concrete entry price from`. Without a real
   price, `try_open_position()` refuses to fabricate one and rejects the
   candidate outright (`"cannot paper-trade without a concrete entry
   price"`).
2. **No verified execution route.** Even given a hypothetical entry
   price, `execution_router.route()` only sends a Solana candidate to
   `JUPITER` when a *verified* migration parser confirmed an AMM pool —
   never inferred from weaker evidence like a candidate's `engine` field
   (see `execution_router.py`'s own docstring on exactly this). Engine B
   ships zero registered parsers today (see `ARCHITECTURE_AUDIT.md`), so
   this is always `UNSUPPORTED` in practice, and `try_open_position()`
   rejects the candidate for that too.

`try_open_position()` takes an explicit `migration_confirmed` parameter
(default `False`, exactly matching `execution_router`'s own conservative
default) purely so its success path is testable without weakening that
default — production (`app/main.py`) never passes `True`, because nothing
in this codebase can honestly claim to know a migration was verified.

Both gates are independently, deterministically tested (see
`tests/test_entry.py`) — this isn't a hand-wave, it's the actual, current
behavior of real code against a real database.

## What's real today

- **`paper_positions` table** — one row per simulated position, whole
  lifecycle (open through close) in place. Unlike Binance's real
  Order/Fill split (Phase 4), there's no live exchange whose state could
  diverge from what this process decided, so there's nothing to reconcile
  after a crash: this table *is* the source of truth.
- **`app/entry.py`** — the gating logic above, plus (on the success path
  no code in this codebase can reach today) creating the `PaperPosition`
  and advancing the candidate `QUALIFIED -> ENTRY_PENDING -> ENTERED ->
  MANAGING` in one call — a paper fill is instant, there's no broker
  round-trip to wait on.
- **`app/pricing.py`** — `latest_price(symbol)` reads the most recent
  `market_snapshots` row for a symbol. Genuinely populated for a Binance
  ticker once `BINANCE_SYMBOLS` is configured (`services/data-binance`);
  nothing in this codebase has ever written a Solana price snapshot, so
  this returns `None` for every Solana mint. Never falls back to a stale
  or interpolated guess.
- **`app/manage.py`** — `evaluate_open_position()` checks a caller-supplied
  current price against the position's stop-loss/take-profit levels
  (handles both `LONG` and `SHORT` correctly, though Solana only ever
  produces `LONG`), closes on a hit, computes realized PnL, and —
  critically — backfills every still-`NULL` `ml_features.label` row for
  that candidate with the real outcome (`1` profitable, `0` not),
  `label_source="paper_trading_realized_pnl"`. This is the one thing in
  this codebase that has ever set that column to anything but `NULL`. The
  candidate is advanced `MANAGING -> EXIT_SIGNAL -> EXITING -> CLOSED`.
- **`app/main.py`** — the periodic loop (every 15s): opens every
  `QUALIFIED` candidate it can, looks up a current price for every open
  position, and manages each one. Given the above, on every real run this
  is expected to find zero `QUALIFIED` candidates and zero prices for any
  Solana symbol — an honest no-op, logged as such, not silently hidden.

## What happens once a Solana price feed exists

Once something in this codebase can honestly populate `Decision.entry`
and `market_snapshots` for Solana symbols (a genuine DEX/aggregator price
feed — out of scope for this project's current phases), nothing else here
needs to change: `try_open_position()` starts succeeding on real
candidates, `evaluate_open_position()` starts closing them against real
prices, and `ml_features.label` starts getting backfilled with real
outcomes — which is exactly what `services/ml`'s training job (Phase 6)
has been waiting on since it shipped.

## Where things live

| Concern | Location |
|---|---|
| `PaperPosition` schema | `packages/core-py/yonixalpha_core/db/models.py` |
| Entry gating + simulated fill | `services/paper-trading/app/entry.py` |
| Price lookup | `services/paper-trading/app/pricing.py` |
| Exit logic + ML label backfill | `services/paper-trading/app/manage.py` |
| Periodic loop | `services/paper-trading/app/main.py` |
