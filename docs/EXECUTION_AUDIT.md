# Live execution audit (Pump.fun / PumpSwap / Jupiter / RPC)

Status on 2026-09-27: **implemented and tested offline. Live execution on
mainnet is UNVERIFIED.** It stays unverified until the dry run in section 8
has been run on the server against real mints. Nothing in this change
enables live trading, switches modes or relaxes a safety check.

## 1. Three production errors, three separate problems

| Error | Root cause | Fix |
|---|---|---|
| `rpc.all_endpoints_failed method=getSignaturesForAddress (db:alchemy: HTTP 429; db:chainstack: HTTP 403)` | Two different providers failing for different reasons. **Alchemy 429**: the free plan's rate limit. The funding/creator lookups (`getSignaturesForAddress`, one call per wallet) had already been throttled in cb69dfc, but the backups still took the overflow when Helius cooled down. **Chainstack 403**: the provider refuses the request — the key, endpoint URL or plan does not allow it. It is not a rate limit, and retrying never helps. | 429: per-method cooldown with exponential backoff (a streak resets on success), and optional lookups (`call_optional`) are not sent while every endpoint is cooling down. 403: the endpoint is marked **AUTHENTICATION FAILED** when it refuses a core method (`getAccountInfo`, `getLatestBlockhash`, `sendTransaction`, …) or 3+ different methods. Once marked, it gets no traffic until a later success. A 403 on one non-core method only skips that provider for that method. Both states appear on the RPC dashboard. |
| `rpc.failover env:primary → db:alchemy` | Expected behaviour: Helius (the primary) was cooling down, so the manager moved to the next endpoint. The problem was that every backup was also unusable (Alchemy 429, Chainstack 403), so failover ran out of options. | Failover now skips endpoints whose auth has failed and endpoints cooling down *for that method*. It records which endpoint served each method; this is the dashboard's "Request routing" section. |
| `LIVE entry failed: … program FAdo9NCw1ssek6Z6yeWzWjhLVsr8uiCwcWNUnKgzTnHe is not allowed; expected exactly one trade instruction, found 0` | The transaction came from PumpPortal's `trade-local` API (`pool=pump`). PumpPortal returned a transaction that did the trade through `FAdo9NCw…`, which Solscan labels "Arbitrage Bot (FAdo9)": an unverified, upgradeable program whose upgrade authority is a single key. That program appears in none of the official Pump sources: pump-public-docs, `@pump-fun/pump-sdk`, `@pump-fun/pump-swap-sdk`. The transaction contained **no** top-level Pump or PumpSwap instruction, which is why the count was "found 0". **The guard was right to refuse it.** Signing it would have let an unaudited, upgradeable third-party program move the wallet's SOL. | The guard is **not** relaxed. FAdo9 stays refused, and a test pins that (`test_10b`). Execution no longer depends on a third party's transaction by default: the new **native builder** constructs the Pump / PumpSwap transaction itself from on-chain state, byte-identical to the official SDKs. PumpPortal is still available as a builder option, but its output goes through the same guard. |

## 2. Execution routes, decided from on-chain state

The route used to be chosen from the token's lifecycle label. It is now
chosen by `yonixalpha_core/solana/venue.py::resolve`, which makes **one**
`getMultipleAccounts` call for [mint, bonding-curve PDA, canonical PumpSwap
pool PDA]:

| On-chain state | Venue | Transaction |
|---|---|---|
| Curve exists, `complete == false` | `PUMP_BONDING_CURVE` | Pump `buy` / `sell` |
| Canonical pool exists, owned by PumpSwap, mints match, reserves read from the vaults | `PUMP_AMM` | PumpSwap `buy` / `sell` (WSOL wrap/unwrap) |
| Curve complete, pool not yet created | `NO_EXECUTABLE_ROUTE` (`MIGRATION_IN_PROGRESS`) | none — not traded |
| Not a Pump token, and Jupiter returns a route | `JUPITER_ROUTE` | Jupiter v6 `/swap` |
| Not a Pump token, no route | `NO_EXECUTABLE_ROUTE` | none |
| RPC or Jupiter unreachable | `RPC_UNAVAILABLE` | none — a controlled failure, not a crash |

"NO DEX POOL" is no longer a generic rejection. A fresh token on its
bonding curve simply trades on the curve.

`OTHER_SUPPORTED_DEX` is defined but nothing produces it. Direct Raydium or
Meteora building is not implemented. Non-Pump tokens are reached through
Jupiter.

### Migration race

After the transaction is built, and **before it is signed**, the executor
re-resolves the venue. If it has changed (the token graduated between build
and sign), the stale transaction is dropped and a new one is built for the
new venue. This happens at most `MAX_VENUE_REBUILDS = 2` times; after that
the order fails with `VENUE_UNSTABLE`. A transaction built for the wrong
venue is never signed (`test_2`).

## 3. Native Pump / PumpSwap builder (`pump_tx.py`, `tx_builders.py`)

The builder follows pump-public-docs (checked on 2026-09-14) and the
official SDKs `@pump-fun/pump-sdk` 2.0.0 and `@pump-fun/pump-swap-sdk`
1.20.0.

- **Bonding-curve buy:** 16 IDL accounts + `bonding-curve-v2` + a buyback fee
  recipient (18 in total). The data is `buy(amount, max_sol_cost,
  track_volume=Some(true))`.
- **Bonding-curve sell:** 14 accounts, + the user volume accumulator for
  cashback coins, + `bonding-curve-v2` + the buyback recipient.
- **PumpSwap buy:**
  - create the WSOL account, transfer SOL into it and `sync_native`;
  - create the token account (idempotent);
  - `buy(base_out, max_quote_in, track_volume)` with the cashback / pool-v2 /
    buyback remaining accounts;
  - close the WSOL account.
  - `extend_account` is added first when the pool account is shorter than 300
    bytes.
- **PumpSwap sell:** create the WSOL account, sell, close.
- **Fee recipients** come from the on-chain `Global` account (Pump) and the
  `GlobalConfig` account (PumpSwap), cached for 60 s. Mayhem-mode coins use
  the reserved recipients.
- **Token-2022** mints use the Token-2022 program for the token account and
  for the trade.
- **Slippage is explicit.**
  - Buys: the expected token amount is computed with the on-chain fee
    (plus a 1% margin), and `max_sol_cost = size × (1 + slippage)`. That
    cost is also capped by the risk engine's `max_sol_in_lamports`.
  - Sells: `min_sol_output = expected × (1 − slippage)`.
- **Proof:** `tests/fixtures/pump_sdk/generate.cjs` runs the official SDKs
  (offline) to produce `fixtures.json`. `tests/test_pump_tx_sdk_parity.py`
  checks our instructions against it byte for byte: program, data and
  every account's key, signer and writable flags. It covers:
  - Token and Token-2022 mints;
  - cashback and non-cashback coins;
  - a normal pool and a pool under 300 bytes.

## 4. Provider-aware transaction guard (`txguard.py`)

The guard is not relaxed. It also no longer applies one rule ("exactly one
trade instruction") to everything. It now checks each transaction against
what its venue should contain.

**Rules for every transaction**
- Our wallet is the fee payer and the only signer.
- Only allowed programs appear. Allowed means: System, ComputeBudget, SPL
  Token, Token-2022, Associated Token, plus the venue's program. Anything
  else is refused, including FAdo9.
- The priority fee is within limits.
- No SOL is transferred to anyone else.
- Token accounts may only be created with our wallet as owner.
- Address lookup tables are resolved before checking. A table that can't be
  resolved is refused.

**Pump / PumpSwap transactions**
- Exactly one trade instruction, sent to the venue's program. A bonding-curve
  order can't be satisfied by a PumpSwap instruction, and the reverse is
  refused too.
- The correct discriminator.
- Account-position checks: user, mint, *our* token account, the curve PDA
  or pool, and WSOL as the quote mint.
- The spend is at most the maximum.
- The minimum tokens out is at least what was quoted.

**Jupiter transactions**
- Only `route` and `shared_accounts_route` (exact-in) are accepted.
  Token-ledger routes, exact-out routes and unknown instructions are
  refused (fail closed).
- Our wallet is the authority.
- The source and destination are our accounts for the right mints.
- `in_amount` is at most the size.
- Slippage is within bounds.
- The platform fee is 0.
- The worst-case output is at least the quote's minimum.

When the guard refuses a transaction, the **unsigned** transaction is stored
on the order so it can be decoded afterwards (`order_inspect`, section 8).

## 5. Stages, retry safety, diagnostics

Every LIVE order records its stages. Each stage carries the signature, slot,
venue, provider, RPC endpoint, latency and error where relevant.

```
VENUE_RESOLVED → TRANSACTION_BUILT → TRANSACTION_GUARD_PASSED → [VENUE_CHANGED → TRANSACTION_BUILT → …]
→ TRANSACTION_SIGNED → SIMULATED → TRANSACTION_SUBMITTED → TRANSACTION_CONFIRMED → FILL_VERIFIED
```

These are the failure stages. Each one stops the order at that point:

- `NO_EXECUTABLE_ROUTE`
- `RPC_UNAVAILABLE`
- `TRANSACTION_BUILD_FAILED`
- `TRANSACTION_GUARD_REJECTED`
- `VENUE_UNSTABLE`
- `SIMULATION_FAILED`
- `TRANSACTION_FAILED`
- `TRANSACTION_EXPIRED`

**Retry safety**
- The signature is persisted *before* the transaction is sent.
- Rebroadcasts resend the same signed bytes, so they carry the same
  signature.
- Once a buy has been signed, it is never rebuilt or re-signed. A buy that
  seemed to time out but actually landed is recognised as confirmed; there
  is never a second buy (`test_11`).

The execution funnel on the dashboard shows, per token, the "Route /
execution" trail: venue, provider, stages and error. So PROMOTE → BUY can
be followed to the exact stage where it stopped.

## 6. Tests (all passing locally; CI runs them)

| # | Scenario | Test |
|---|---|---|
| 1 | Fresh token, no DEX pool → bonding curve (Token and Token-2022) | `test_execution_venues::test_1_*` |
| 2 | Token migrates between build and sign → rebuilt for PumpSwap, the stale transaction is never sent | `test_2_*` |
| 2b | Curve complete, pool not yet created → not traded | `test_2b_*` |
| 3 | Migrated token → canonical PumpSwap pool (300-byte and 243-byte pool) | `test_3_*` |
| 4 | Non-Pump token → Jupiter route with a lookup table, validated | `test_4_*` |
| 5 | Non-Pump token, no route → `NO_EXECUTABLE_ROUTE` | `test_5_*` |
| 6 | Rate-limited provider cools down, the backup serves | `test_rpc_rate_limits::test_6_*` |
| 7 | Provider refusing basic requests → AUTHENTICATION FAILED; cleared by a success | `test_7_*`, `test_7b_*` |
| 8 | Every provider failing → one controlled error; routing recorded | `test_rpc_rate_limits::test_8_*`, `test_execution_venues::test_8_*` |
| 9 | Jupiter setup + compute + swap + cleanup accepted | `test_9_*` |
| 10 | Tampered Jupiter transactions refused (FAdo9, foreign destination, foreign authority, platform fee, excess slippage, SOL sent out) | `test_10_*` |
| 10b | Pump semantics checked account by account; the FAdo9 production layout gives "found 0" | `test_10b_*` |
| 11 | Timed out but landed → confirmed, never re-bought | `test_11_*` |
| — | Byte parity with the official Pump SDKs | `test_pump_tx_sdk_parity.py` (16) |

**Fixtures.** The Pump fixtures are generated by the **official SDKs**; the
chain state is served by `tests/chain_fake.py`. They are **not** transactions
captured from mainnet. The sandbox this was built in cannot reach mainnet
RPC, Jupiter, PumpPortal or Solscan, and no transaction layout was invented
instead. Real-chain verification is the dry run in section 8.

## 7. What was not changed

- The guard is not bypassed and not relaxed. No program was added "to make
  it pass".
- Wallet validation, token validation, risk checks, sellability checks and
  slippage protection are unchanged.
- Live trading is not switched on by this change, and the mode is not
  changed.

## 8. Verifying on the server (required before calling live execution fixed)

In `/opt/yonixalpha`, after deploying:

```bash
C="docker compose --env-file .env -f infra/docker/docker-compose.yml -f infra/docker/docker-compose.prod.yml"

# Build + guard + simulate a 0.01 SOL buy. Nothing is signed or sent.
$C run --rm paper-trading python -m yonixalpha_core.tools.exec_dryrun <FRESH_PUMP_MINT> --sol 0.01
$C run --rm paper-trading python -m yonixalpha_core.tools.exec_dryrun <MIGRATED_PUMP_MINT> --sol 0.01

# What happened to the last LIVE orders, stage by stage (decodes refused transactions)
$C run --rm paper-trading python -m yonixalpha_core.tools.order_inspect --last 5
```

`exec_dryrun` exit codes:
- `0`: `GUARD: PASSED` and `SIMULATION: OK`;
- `2`: no executable route;
- `3`: the build, the guard or the simulation failed. The program logs are
  printed.

A simulation runs the real on-chain programs against real accounts. A
passing simulation on a fresh mint and on a migrated mint is the evidence
needed before this path can be called verified.

**Chainstack 403.** No code change fixes this. Check the Chainstack
endpoint URL, key and plan in the RPC dashboard, or disable that provider.
The dashboard shows it as AUTHENTICATION FAILED so that it is no longer
mistaken for a rate limit.
