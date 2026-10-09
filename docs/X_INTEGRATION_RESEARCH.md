# X Integration Research (2026-10-09/10)

What was read, how, and what it means for YonixAlpha. "Verified" means read
in the source itself during this session; "claimed" means stated by an
author and not checked.

## 1. Access during this session

- `docs.x.com`, `developers.jup.ag`, `docs.dexscreener.com`, `solana.com`:
  the session's network proxy refused them (HTTP 403 on CONNECT). They were
  NOT read directly.
- `github.com/xdevplatform/docs` (the source of docs.x.com): cloned, last
  commit 2026-10-09 20:04 UTC. All X statements below come from it.
- The four third-party repositories: cloned read-only (depth 1) into an
  isolated scratch folder; nothing was executed or installed.

## 2. Official X API (verified in xdevplatform/docs)

| Topic | Finding | File |
|---|---|---|
| Recent search | `GET /2/tweets/search/recent`, last 7 days, "available to all developers" | `x-api/posts/search/introduction.mdx` |
| Full-archive search | `GET /2/tweets/search/all`, pay-per-use and Enterprise only | same |
| Limits | recent search 450 / 15 min per app, 300 / 15 min per user; 10 default, 100 max results; 512-char query | `x-api/fundamentals/rate-limits.mdx` |
| Filtered stream | `GET /2/tweets/search/stream`: 50 / 15 min, 1 connection, 1000 rules | same |
| Pricing | pay-per-use, no subscription; **Posts: Read $0.005 per resource returned**; User: Read $0.010; cap 3 million Post reads / month | `x-api/getting-started/pricing.mdx` |
| Deduplication | the same resource is not charged twice within one 24-hour UTC day | same, "Deduplication" |
| Free credits | one-time incentives (up to $70) for adding a card / first auto-recharge; expire after 3 months | `x-api/getting-started/free-credits.mdx` |
| Pagination / polling | `next_token`; `since_id` for polling | `x-api/posts/search/integrate/paginate.mdx` |
| Stored content | stored X content must be kept current; delete/modify within 24 h of a request when deleted/modified on X | `developer-terms/policy.mdx` (lines 61, 208) |
| Off-X matching | associating an X identity with off-X identifiers is restricted | same, "Off-X matching" |
| Use-case binding | the registered use case is binding; changes need approval | same, line 150 |

### Cost consequences for YonixAlpha

- Measured on the server 2026-10-09: about 47 new pump.fun tokens per minute.
  Querying every token with the minimum 10 results would be about
  47 x 10 x $0.005 = $2.35 per minute (about $3,400 per day). Not viable.
- Therefore: only candidates whose on-chain signal already qualified are
  looked up (`query_scope = qualified`), each at most `max_requests_per_token`
  times a day, under a hard USD budget (default $1.00 per day = 200 Post
  reads), with a cache, and a stop until UTC midnight when the budget is
  reached. The filtered stream (one connection, rule-based) is not used:
  every matching post is billed and pump.fun volume would exhaust any budget.
- Nothing here was verified against a live X account: the request shape
  follows the docs; no bearer token was available in this session.

## 3. Third-party repositories

| Repository | Last commit | License | Verified content | Reuse decision |
|---|---|---|---|---|
| DoradoDevs/solana-narrative-scanner | 2026-07-02 | MIT | TypeScript service: BullMQ queues, Postgres (drizzle), Redis, `twitter-api-v2` filtered-stream adapter (`src/adapters/twitter.adapter.ts`), an optional `@the-convocation/twitter-scraper` adapter (logged-in scraping, README warns about ToS), `@xenova/transformers` embeddings, `ml-kmeans` clustering, Telegram/Discord/Reddit adapters. One script-style test (`src/scripts/test-alert.ts`); `npm test` uses vitest but no test files were found. | **Ideas only, no code.** Reused: ticker / base58 address extraction idea (`src/utils/token-extractor.ts`), bot-detection heuristics (`src/scoring/checks/bot-detection.ts`: low-follower share, < 5 s bursts, copy-paste ratio). Rejected: the scraper (ToS), transformer embeddings and k-means (RAM/CPU on a 2 GB server), the filtered stream (cost), the whole Node service (a second runtime and queue system). |
| fdarkaou/sol-trader | 2026-02-13 | MIT | Python "skills" (`skills/sol-trader/scripts/*.py`, `skills/x-gem-finder`, `skills/x-research`). X access goes through the `bird` CLI with `AUTH_TOKEN` / `CT0` browser cookies (`skills/x-research/SKILL.md` lines 84-85), i.e. a logged-in session. No tests. | **Rejected.** Cookie-based logged-in access is exactly what the spec forbids. Its social/on-chain weighting ("Social: 25 %") is a claim with no evaluation in the repository. |
| Laz-Builds/solana-twitter-buy-bot | 2026-07-31 | MIT | `src/twitter-buy-bot.ts`: official API v2 with a bearer token (`/2/users/by/username`, `/2/users/:id/tweets`), `since_id` polling and a `lastSeenId` baseline so only newer posts trigger; buys via Jupiter on a keyword/image trigger with whole-amount settings. No tests. | **Ideas only:** `since_id` cursor and a baseline before acting (duplicate processing). Rejected: keyword-triggered automatic buying, LLM vision trigger. |
| thegreatola/memecoins-trading-agent | 2026-04-24 | MIT | Two TypeScript files (`orchestrator.ts`, `walletTracker.ts`) and persona markdown; no X API usage found by search. No tests. | Not used. |

No third-party code was copied into YonixAlpha. No new Python or Node
dependency was added: the X client is `httpx`, already a dependency.

## 4. Other references

- Jupiter swap v1 and DexScreener: the existing integrations
  (`solana/market_data.py`: `lite-api.jup.ag/swap/v1`, `api.jup.ag/swap/v1`,
  `api.dexscreener.com/latest/dex/tokens/{mint}`) were researched in earlier
  rounds; their documentation could not be re-read in this session (proxy),
  so "still supported" is NOT VERIFIED today. They are unchanged by this
  upgrade.
- Pump.fun program layout: re-checked 2026-10-09 against
  `pump-fun/pump-public-docs` `idl/pump.json` (CreateEvent / TradeEvent field
  order matches `solana/pumpfun.py`).
- Solana token basics (decimals, raw amounts, Token-2022): applied as already
  implemented in `rent_reclaim`, `token_safety` and `live_trading`; the
  solana.com pages were not reachable in this session.

## 5. Resource expectations (measured locally, not on the server)

- `x_narrative.analyse` on 100 posts: 4.4 ms per call, about 95 KiB peak
  traced memory (Python, this sandbox).
- `exit_plan.check_exit`: about 17 microseconds per call.
- Network: at most `max_posts_per_query` (default 10) posts per lookup; with
  the default budget at most 20 lookups per day.
- No new process or container: the X pass is an asyncio task inside the
  existing decision-engine; while disabled it reads nothing and sleeps 5 min.
