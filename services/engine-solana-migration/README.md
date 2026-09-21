# engine-solana-migration

Engine B per the project spec: detects when a token migrates/graduates from
a launch platform's bonding curve to a real AMM pool (Raydium/Orca/etc.).

## Why this ships with zero AMM parsers by default

`engine-solana-discovery` and `engine-solana-momentum` can both parse
generic SPL Token Program instructions (`initializeMint`, `transferChecked`)
because every Solana token — regardless of which launch platform created it
— uses the same, single, foundational Token Program interface. There is no
equivalent single interface for "a pool was created": each AMM (Raydium's
AMM v4, Orca's Whirlpools, Meteora's DLMM, ...) has its own program ID and
its own, mutually incompatible instruction layout for pool initialization.

This codebase was built in a network-restricted sandbox with no outbound
access to Solana RPC endpoints or any AMM's documentation (see the repo
root `ARCHITECTURE_AUDIT.md`). Hand-writing a parser for a specific AMM's
instruction layout without being able to verify the current program ID and
field names against live documentation would mean fabricating
trading-relevant parsing logic — the project spec is explicit that this
must never happen (section 53: "no fabricated data").

## What's actually here

- `app/detect.py` — a real, tested dispatch mechanism: `register_parser(program_id, parser_fn)`
  registers a parser for a specific AMM program; `extract_pool_initialization`
  dispatches to it. The registry starts empty.
- `app/candidates.py` — a real, tested function that takes a detected
  migration (in whatever shape a parser produces) and persists it: a
  `TokenEvent` (event_type="migration") plus an `engine="migration"`
  `TradingCandidate` in DISCOVERED state, idempotently.
- `app/main.py` — wires RPC health checks (always active) and, only if
  `MIGRATION_AMM_PROGRAM_IDS` names a program with a parser registered for
  it, a `logsSubscribe` watch for that program's transactions.

## Activating real detection

1. Verify the current program ID and pool-initialization instruction
   layout for the AMM(s) you want to watch, against their current official
   documentation (not this repo — it has none to offer here).
2. Write a parser function matching the `PoolInitParser` signature in
   `app/detect.py` and call `register_parser(program_id, your_parser)`
   during startup (e.g. in `app/main.py`, before `run()` is called).
3. Set `MIGRATION_AMM_PROGRAM_IDS=name:programid,...` in the environment.

Until that happens, this service runs, reports its health, and does
nothing else — which is the correct behavior for a detector with no
verified detection logic, not a bug.
