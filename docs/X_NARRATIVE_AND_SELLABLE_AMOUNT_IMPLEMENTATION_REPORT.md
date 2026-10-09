# X Narrative Intelligence + Sellable-Amount Protection — Implementation Report (2026-10-10)

Companion documents: `X_NARRATIVE_AND_SELLABLE_AMOUNT_AUDIT.md` (what existed),
`X_INTEGRATION_RESEARCH.md` (sources, costs, licences).

## 1. Existing implementation discovered

See the audit. In short: identity is the mint plus the on-chain CreateEvent
(name, symbol, uri); no social or X data existed; exits were correct in raw
units and never oversold, but nothing stopped a partial take-profit from
leaving dust or a remainder worth less than a sell fee, nor a take-profit
worth less than its own fee; there was no whole-schedule view.

## 2. Research sources actually inspected

Official X docs through their source repository `xdevplatform/docs` (cloned
2026-10-09); four third-party repositories cloned read-only;
`pump-fun/pump-public-docs` IDL. docs.x.com, jup.ag, dexscreener and
solana.com were blocked by the session's network proxy and NOT read
directly (details in the research document).

## 3. Third-party repositories

| Repository | Decision | Why |
|---|---|---|
| DoradoDevs/solana-narrative-scanner (MIT) | ideas only | address/ticker extraction and bot heuristics reused as ideas; scraper, transformer embeddings, k-means and the Node service rejected (ToS, RAM/CPU) |
| fdarkaou/sol-trader (MIT) | rejected | X access through browser cookies (logged-in session) |
| Laz-Builds/solana-twitter-buy-bot (MIT) | ideas only | `since_id` baseline / duplicate handling; keyword-triggered buying rejected |
| thegreatola/memecoins-trading-agent (MIT) | not used | no X integration found |

No third-party code copied, no new dependency added.

## 4. APIs and credentials

- Official X API v2, `GET /2/tweets/search/recent`, Bearer token (app-only).
- `.env`: `X_NARRATIVE_ENABLED=false` (default) and `X_API_BEARER_TOKEN=`.
  The token is a `SecretStr`: never logged, never returned by the API (tested),
  never sent to the browser, never stored in the database.
- Dashboard (Data Providers page): enable switch, query scope, daily USD
  budget, posts per lookup, lookups per token, post age window, cache TTL.
  `narrative_weight` stays 0 (shadow); it is stored but not exposed as a
  control that changes trading.

## 5. Cost and rate limits

- $0.005 per Post returned; the same Post is free again within the UTC day.
- Default budget $1.00/day (200 Post reads, 20 lookups of 10). Spending is
  counted from the posts actually returned, de-duplicated per UTC day.
- Recent search allows 450 requests / 15 min per app: the default budget uses
  at most 20 per day.
- On HTTP 429 the provider pauses until `x-rate-limit-reset` (at least 60 s,
  900 s if absent); on 401/403 for an hour; on budget exhaustion until UTC
  midnight. Nothing is stored per mint for these.

## 6. Files changed

New:
- `packages/core-py/yonixalpha_core/exit_plan.py` — sellable-amount planner.
- `packages/core-py/yonixalpha_core/x_narrative.py` — X client, identity, metrics, budget, background pass.
- `apps/api/app/api/routes/x_narrative.py` — status, settings, per-token observations.
- `apps/api/migrations/versions/0046_x_narrative_observations.py`.
- `apps/web/components/ExitPlan.tsx`, `apps/web/components/XNarrative.tsx`.
- Tests: `packages/core-py/tests/test_exit_plan.py`, `packages/core-py/tests/test_x_narrative.py`,
  `services/paper-trading/tests/test_exit_protection_live.py`, additions in
  `apps/api/tests/test_control_center.py` and `services/decision-engine/tests/test_gate_eval.py`.
- Docs: the three documents named above.

Changed:
- `paper_engine.apply_step` — optional `exit_protection` argument (callers that do not pass it are unchanged).
- `live_trading.manage_live_position` — protection check before a SELL is queued (recorded; applied only in PAPER_AND_LIVE).
- `services/paper-trading/app/gate_manage.py` — loads the protection settings once per pass and passes them.
- `services/decision-engine/app/gate_eval.py` — one Redis ZADD to queue a candidate when X is switched on.
- `services/decision-engine/app/main.py` — the X background task.
- `apps/api/app/api/routes/paper.py` — exit-protection settings and per-position exit plan.
- `config.py`, `db/models.py` (`XNarrativeObservation`), `.env.example`, the trade, token, paper and RPC pages.

## 7. Database changes and rollback

- Migration 0046 creates `x_narrative_observations` (new, empty; index on
  `(mint, observed_at)`). No existing table is altered, nothing is backfilled.
- Settings rows `exit_protection_settings` and `x_narrative_settings` in
  `platform_settings` appear only when saved from the dashboard.
- Rollback: `alembic downgrade 0045` drops the table (tested: upgrade ->
  check -> downgrade -> upgrade on a fresh database, "No new upgrade
  operations detected").

## 8. X identity-resolution tests (`tests/test_x_narrative.py`)

Exact mint in text and in an expanded pump.fun URL (EXACT_MINT); ticker only
is TICKER_ONLY (ambiguous, not accepted); the same ticker with a different
Solana address is CONFLICT (rejected); ticker + exact name is NAME_TICKER; a
celebrity name in the post is NAME_ONLY (not an identification); an author
handle resembling the token name is flagged "not treated as an endorsement".

## 9. Narrative-scoring tests

Independent original posts score; 8 copy-paste posts from 2 accounts are
flagged REPEATED_CONTENT, FEW_INDEPENDENT_AUTHORS and SYNCHRONIZED_BURST and
score below 8 independent authors even with high engagement; no posts =
NO_DATA (no score, no identity); irrelevant posts = NO_MATCH; one post =
INSUFFICIENT_DATA; engagement absent when the API did not return it; the
first relevant post time is recorded even when it predates the token;
COMBINED_SIGNAL_SCORE equals the on-chain score at weight 0.

## 10. ML feature and leakage checks

- Observations are stored with `observed_at` = lookup time, apart from any
  outcome. `x_narrative.features_at(session, mint, at)` returns only the latest
  observation at or before `at`; a later observation is never visible (tested).
- The existing ML samples and models are unchanged: X features are not yet in
  any training set. The chronological evaluation (train -> validation ->
  unseen test -> shadow -> paper) and the baseline comparisons are NOT RUN:
  there is no X data yet. No claim is made that X improves anything.

## 11. Partial-exit calculation tests (`tests/test_exit_plan.py`, `test_exit_protection_live.py`)

ROUND_DOWN raw conversion (never more than held); 1-raw dust folded into
the sale even without a price; an uneconomic remainder folded into the sale;
a take-profit worth less than 3 sell fees deferred (sell 0, tokens kept); a
partial that empties the position never deferred; stop loss / trailing /
manual / exit-intelligence EXIT never changed even below the fee; a
defensive reduce enlarged but never deferred; never more than the position
holds; BALANCE_MISMATCH, NO_ROUTE, TRANSFER_RESTRICTION; decimals 6 and 9;
whole schedule 0.4/0.3/0.3 over 1,000,001 raw -> 400,000 / 300,000 / 300,001
(last level takes the dust) and the same plan after TP1 was hit; settings
validation. Paper round trip: a TP that would leave 2 raw units closes the
position (`exit_protection.merged_remainder` on the timeline); without the
setting the old behaviour is unchanged. LIVE: default mode records
`exit_protection.merged_remainder.shadow` and sends exactly the planned 60 %;
PAPER_AND_LIVE sends 60 % + 2 raw in one SELL; a stop loss is unchanged in
both modes.

## 12. Migration and route-switch tests

Unchanged code paths, existing tests still pass: sell route resolved at sell
time; `test_exit_parity.py::test_a_curve_sell_rejected_as_curve_complete_moves_the_next_sell_to_pumpswap`.
The exit planner works on raw units and the price the position manager
already uses, so it does not bind a position to a route.

## 13. Resource measurements (local sandbox, not the server)

`x_narrative.analyse` on 100 posts: 4.4 ms, about 95 KiB peak; `check_exit`:
about 17 microseconds. No new process. While X is disabled its loop reads
nothing and sleeps 5 minutes; while enabled it pauses at CRITICAL resource
level. Server measurements: NOT VERIFIED until deployed.

## 14. Tests

Run on 2026-10-10 in this sandbox (Postgres + Redis local):

| Suite | Result |
|---|---|
| ruff | clean |
| core-py | 941 passed |
| api | 182 passed |
| decision-engine | 59 passed (after fixing one regression found by the suite, below) |
| paper-trading | 85 passed |
| data-solana, data-evm, copy-engine, discovery, migration, momentum, ml | 10, 14, 18, 18, 11, 11, 48 passed |
| web | tsc, lint, production build clean |
| alembic | upgrade, check, downgrade, upgrade clean |

Regression found and fixed before pushing: `gate_eval` read
`settings.X_NARRATIVE_ENABLED` directly; settings objects without the field
(26 decision-engine and 3 paper-trading tests use a minimal namespace) raised
AttributeError. It now reads it with a default of off.

## 15. Not verified

- Any call to the real X API (no bearer token in this session); response
  shapes follow the official docs and the tests use mocks.
- Whether X narrative features predict anything (no data; not evaluated).
- Sellable-amount protection on real LIVE sells (default mode records only).
- Jupiter / DexScreener documentation currency (proxy blocked the docs).
- Server resource use of the new code.

## 16. Deployment

1. `scripts/deploy.sh` (migration 0046 creates an empty table: instant).
2. Exit protection is active for PAPER immediately (mode PAPER); LIVE exits
   are only recorded. Review the recorded LIVE changes on the trade pages
   before choosing PAPER_AND_LIVE on the Paper Trading page.
3. X stays off. To try it: put `X_NARRATIVE_ENABLED=true` and
   `X_API_BEARER_TOKEN=...` in `.env`, `docker compose ... up -d`, then switch
   it on and set the budget on the Data Providers page.

## 17. Rollback

- Exit protection: set mode OFF on the Paper Trading page (no deploy needed).
- X: switch off on the Data Providers page or set `X_NARRATIVE_ENABLED=false`.
- Code: revert the merge commit; `alembic downgrade 0045` removes the table.

## 18. Remaining risks

- The X recent-search cost makes broad coverage expensive; with the default
  budget only a small share of qualified candidates gets a lookup.
- A lookup runs after the decision, so the first decision on a token never
  has X data; later re-evaluations may (always as of their own time).
- Name / ticker matching is lexical; unusual names can be missed or
  over-matched (rejected as CONFLICT or kept as candidates, never scored).
- The deferral rule uses the last marked price; a fast move between the mark
  and the sale can change a sale's value. Full exits are never affected.
