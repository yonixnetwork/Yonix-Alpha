# Creator history, migrated-token USD liquidity, name filters, snipe settings

Date: 2026-09-26. These are additions to the working pipeline. No existing check was removed or replaced. Every change is
an extra finding in the same safety gate, controlled by a dashboard setting.

## 1. Creator (developer) token history

**What is counted.** The number of pump.fun tokens the launch creator's wallet has created, this token included.

**How it is counted.**
- Every pump.fun launch creates one `BondingCurve` account. Since 2025 that account stores the creator wallet at byte
  offset 49, right after the `complete` flag at offset 48.
- One `getProgramAccounts` call on the Pump program is filtered by:
  - the account discriminator;
  - `memcmp` on the creator at offset 49;
  - a one-byte data slice, the `complete` flag.
- The result is every curve the wallet created, and whether each one migrated.
- Curves stay on chain after migration, so migrated launches are counted too.
- The result is cached in Redis for 1 hour per creator. A token launched after the cache was filled is added to the
  count, because this wallet created it.
- Module: `yonixalpha_core/solana/creator_history.py`.

**Helius: measured live on 2026-09-26.** The on-chain count does **not** work on Helius:

- A plain `getProgramAccounts` on the Pump program is refused: "Too many accounts requested (10000001 pubkeys)".
- The paginated `getProgramAccountsV2` pages through the program's whole account space, not the filtered matches. Five
  pages (50,000 accounts, 8.5 s) found nothing, not even the creator's own new curve. A full scan would be about 1,000
  pages per creator, so it is not used.
- One refusal is remembered for 24 h, so evaluations make no further calls and add no latency.
- On Helius the count therefore comes from this system's pump.fun stream only: PASS "at least N" when it meets the
  minimum, otherwise **UNKNOWN**.
- A second source for the full count, pump.fun's public API, is **NOT COMPLETE**. Its response format must first be
  checked from the server, because it cannot be reached from the build environment.

A refused request (JSON-RPC codes -32600/-32601/-32602) no longer counts against the RPC endpoint's health, so it can
never put the primary RPC into cooldown for the other reads.

**What it cannot count.**
- Curves created before pump.fun added the creator field do not carry it, so they are not counted. The count can
  undercount such a wallet. It never overcounts.
- If the RPC refuses or fails the query, the only other evidence is this system's own stream: launches by that wallet
  that it observed, kept for 7 days. That is a lower bound, and it is used only when it already meets the minimum
  (shown as "at least N"). Otherwise the result is **CREATOR HISTORY: UNKNOWN**. The number is never estimated.

**Also reported**, from the stream only, so limited to what it saw:
- previous launches where the creator sold within 5 minutes;
- previous launches the fresh-token observation rejected, with the reason;
- the creator selling this token now;
- creator-funded early buyers. This existing indicator is unchanged.

**Decision** (per engine scope, Risk Settings → *Creator risk*):

| Setting | Default | Meaning |
|---|---|---|
| `creator_history_check` | ON | Creator History Check ON/OFF |
| `min_creator_tokens_created` | 5 | Minimum tokens created |
| `creator_below_threshold_action` | WARN | WARN / REDUCE_SIZE / REQUIRE_MANUAL_APPROVAL / REJECT |
| `creator_history_unknown_action` | WARN | Same choices, when the count is UNKNOWN |
| `max_creator_tokens_created` | 0 (off) | Serial-launcher ceiling (your `MAX_DEV_TOKENS`): at or above it, approval is needed |

- The default action is WARN. Fewer than 5 launches is not treated as malicious.
- REQUIRE_MANUAL_APPROVAL behaves as everywhere else. In AUTO mode there is no approval step, so it becomes NO_TRADE.

**Display.** Examples of the messages:

- `CREATOR TOKENS CREATED: 12 · Minimum: 5 — CREATOR HISTORY: PASS; previous launches 11, 3 migrated`
- `Creator Tokens Created: 2 · Minimum: 5 · Action: REJECT · Reason: CREATOR_HISTORY_BELOW_THRESHOLD — CREATOR HISTORY: REJECT`
- `CREATOR TOKENS CREATED: UNKNOWN · Minimum: 5 · Action: WARN · Reason: CREATOR_HISTORY_UNKNOWN — CREATOR HISTORY: UNKNOWN (getProgramAccounts failed: …)`

Where to see them:

- a **Creator history** card on each decision's page (Decisions → a decision);
- the gate findings on **Fresh Observation**.

**It replaces nothing.** These checks all still run on their own:

- holders and creator allocation;
- creator-linked and related wallets, clusters, wash-trading indicators;
- volume, buyers and sellers;
- liquidity and sellability;
- tax, slippage and impact;
- token program and authorities;
- execution.

A test confirms that a clean 40-launch history still rejects a token with an active mint authority or a 50% top holder.

## 2. Migrated (PumpSwap) tokens: minimum usable liquidity in USD

**Applies only to migrated tokens.** A fresh bonding-curve token is judged on:

- curve state and executability;
- buy/sell simulation;
- reserves, volume, buyers and sellers, pressure and price;
- creator, holder and wallet risk;
- tax, slippage and impact.

It is never rejected because a DEX pool or a USD figure does not exist. Once the token migrates, the migration engine
evaluates it with pool rules, including this one.

**Measured on the pool itself:**

| Field | How |
|---|---|
| Usable liquidity (USD) | Pool SOL reserve (what a seller can withdraw) × SOL/USD |
| Total liquidity (USD) | Both sides at the pool price (2 × usable): the figure aggregators show |
| Executable size (depth) | Largest size whose entry and exit price impacts stay within `max_entry_impact_bps` / `max_exit_impact_bps` (bisection on the exact pool model) |
| Entry/exit impact | At the planned size |
| Entry/exit slippage | Impact plus pool fee at the planned size, against the marginal price |

**SOL/USD** comes from a Jupiter quote of 1 SOL → USDC. That is an executable price, not an index. The fallback is
DexScreener's `priceUsd / priceNative` for the token's pair. It is cached for 60 s. If neither answers, the result is
`MIGRATED_LIQUIDITY_USD_UNKNOWN` → NO_TRADE. It is never guessed.

**Decision:** usable below the minimum → **NO_TRADE**, for example:

```
Usable Liquidity: $8,200 (54.67 SOL at $150.00) · Total: $16,400 · Minimum: $10,000 · Decision: NO_TRADE · Reason: INSUFFICIENT_MIGRATED_LIQUIDITY
```

Settings (Risk Settings → *Migrated liquidity (PumpSwap)*, scope `solana_migration`):

| Setting | Default |
|---|---|
| `migrated_liquidity_check` | ON |
| `min_migrated_liquidity_usd` | 10000 |
| `migrated_max_entry_slippage_bps` | 400 |
| `migrated_max_exit_slippage_bps` | 600 |
| `max_entry_impact_bps` / `max_exit_impact_bps` | 300 / 500, shared with every venue and shown in the same group |

**Behaviour change** (intended, from the specification):

- A young migrated pool below the SOL liquidity minimum used to WAIT. With the USD check on, a pool below $10,000 is
  now NO_TRADE.
- NO_TRADE does not end the candidate. It is re-evaluated until its observation limit, so a pool that grows can still
  qualify.
- With the check OFF, the old WAIT behaviour is unchanged. A test covers this.

## 3. Token-name filters and the scam-name preset

| Setting | Default | Finding |
|---|---|---|
| `min_name_length` | 2 | `NAME_TOO_SHORT` → REJECT |
| `skip_duplicate_names` | ON | `DUPLICATE_NAME` → REJECT |
| `ascii_names_only` | OFF | `NON_ASCII_NAME` → REJECT |

About `DUPLICATE_NAME`:

- It fires when the name was already launched by another mint in the last 24 h. The first launch keeps the name.
- Names are compared case-, space- and punctuation-insensitively.
- Symbols are not compared, because popular tickers are reused all the time.

**Rules → "Add scam-name preset"** installs your word lists as ordinary GLOBAL BLOCK rules. Each one can be disabled,
edited or deleted. Running it again skips rules that already exist, and the action is audited.

- `BLACKLISTED_WORDS` become name substring rules.
- `BLACKLISTED_EXACT` become exact rules on the name and on the symbol.

## 4. Your sniper configuration keys

Settings → **Pump.fun snipe settings**:

| Key | Where it lives now |
|---|---|
| `SNIPE_MODE` fresh_launch / migration / both / off | Strategy modes of Fresh Tokens and Migrated Tokens |
| `AUTO_BUY` | ON → strategy mode AUTO; OFF → MANUAL (each entry waits for approval) |
| `BUY_AMOUNT_SOL` | Strategy config `manual_position_size_sol` (both strategies); reduced automatically by risk limits |
| `SLIPPAGE`, `PRIORITY_FEE` | Live execution settings `entry_slippage_pct`, `priority_fee_sol` |
| `COOLDOWN_SECONDS` | `cooldown_after_loss_seconds` (a pause after a loss; there is no pause after every buy) |
| `MIN_NAME_LENGTH`, `SKIP_DUPLICATE_NAMES`, `ASCII_NAMES_ONLY`, `MAX_DEV_TOKENS` | Safety settings, saved in both Pump.fun scopes |
| `BLACKLISTED_WORDS`, `BLACKLISTED_EXACT` | Rules → scam-name preset |
| `BUY_POOLS` | Automatic by lifecycle: curve → `pump`, migrated → `pump-amm` |
| `WS_URL`, `TRADE_URL`, Jupiter URLs | Server `.env` / fixed in code; not editable from the browser, by design |
| `SOL_MINT` | Constant |
| `LAUNCHPAD` meteora / both, `METEORA_DBC_PROGRAM` | **NOT COMPLETE**: no Meteora DBC stream, curve model or execution route exists |

## 5. Verification

- **Tests:** `packages/core-py/tests/test_creator_and_migrated_liquidity.py` covers the ten required cases (marked
  `test_required_N`) and the module details.
  - The paths under test: the real assemblers with the stream in Redis and the RPC faked at the JSON-RPC boundary, the
    real gate, and the versioned settings store.
  - The API tests cover settings editing, enums and the preset.
- **Live, on the droplet:** `$C run --rm decision-engine python -m yonixalpha_core.tools.verify_live --seconds 60` (see CONTROL_CENTER.md §3) now reports:
  - `creator_history`: the `getProgramAccounts` count for a creator sampled from the live stream;
  - `sol_usd`: the Jupiter SOL → USDC price.

  Whether your RPC plan serves this `getProgramAccounts` query is **UNVERIFIED** until that runs. If it refuses, the
  gate reports CREATOR HISTORY: UNKNOWN (default action WARN), and trading continues on every other check.
