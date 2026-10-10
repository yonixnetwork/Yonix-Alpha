# Early entry: root-cause audit (2026-10-10)

Scope: why YONIXALPHA enters fresh pump.fun tokens late, traced in code
before anything was changed. The audit was done first. Thresholds were not
changed and no safety rule was weakened. Everything below comes from
reading the code at `ad17dfa`.

Numbers measured on the server are NOT in this document: the production
database was not reachable from the development container. The tool added
for that measurement is `python -m yonixalpha_core.tools.entry_timing`
(see "How to measure" at the end). Every latency below is a **code bound**:
the longest or shortest a stage can take given its timers. It is not an
observed value.

## 1. The entry path as it was

| # | Stage | Where | Timing in code |
|---|---|---|---|
| 1 | Create event received | `solana/pump_stream.py` (logsSubscribe on the pump program, commitment `confirmed`) | about 0.4 to 1 s after the slot; the trade timestamps are on-chain block seconds |
| 2 | Observation window | `services/engine-solana-discovery/app/funnel.py` | `fresh_observation_seconds` = 10 s after launch |
| 3 | Funnel pass | `engine-solana-discovery/app/main.py` | `FUNNEL_INTERVAL_SECONDS` = 10: a token whose window ended just after a pass waited up to 10 s more |
| 4 | Promotion rule | `funnel.observe_fresh` | at least 8 trades, 6 unique buyers, 0.5 SOL since launch, not deteriorating (`fresh_promote_*`, `safety/settings.py:104-108`) |
| 5 | Decision loop | `services/decision-engine/app/main.py` | `EVAL_INTERVAL_SECONDS` = 15 s sleep between passes over all candidates |
| 6 | Per-candidate pacing | `decision-engine/app/gate_eval.py` | `REEVALUATE_EVERY_SECONDS` = 30 (`yx:gate:pace:{id}`); nothing re-evaluated sooner, whatever happened to the token |
| 7 | Data assembly | `solana/assembler.assemble_fresh` | sequential RPC calls (mint, curve, holders); timed in `evidence.timings_ms` |
| 8 | Volatility | `solana/flow.py` (`MIN_RETURNS_FOR_VOLATILITY` = 3) and `solana/entry_quality.py` | AVAILABLE needs 3 or more 10-second returns, so about 40 s of trading; earlier it is LOW_CONFIDENCE or UNAVAILABLE |
| 9 | Gate result before that | `safety/gate.py:833`, `safety/planning.py:275` | LOW_CONFIDENCE -> REQUIRE_MANUAL_APPROVAL; UNAVAILABLE + automatic stop -> `AUTO_SL_NO_VOLATILITY` -> NO_TRADE |
| 10 | Strategy signal | `strategies/solana.py:28` (`fresh_launch_signal`) | 300 s window; buy/sell ratio >= 1.5, acceleration, price change > 0 **since the window start** |
| 11 | LIVE execution | live order worker, `execution_orders.diagnostics.timing` | quote, build, sign, submit, confirm; measured per order |
| 12 | PAPER fill | `paper_engine` / `paper_execution` | decision price plus measured live drift, failure rates and fixed costs |

## 2. Root causes, in order of effect

1. **Volatility gates the automatic entry at about 40 s of trading.** The
   stop is sized from volatility. With fewer than 3 ten-second returns the
   gate returns REQUIRE_MANUAL_APPROVAL or NO_TRADE. This is a correct safety
   rule (a stop sized from 2 returns is not a stop). It also means the
   existing pipeline cannot enter automatically in the first ~40 s. **Not
   changed.** The new strategies record when they would have entered so the
   cost of this wait can be measured, not guessed.
2. **Timers stack.** Window 10 s + funnel cadence up to 10 s + loop sleep up
   to 15 s + 30 s pacing between evaluations. Worst case, a token that
   becomes eligible right after an evaluation waits 30 s before it is
   looked at again, even if it doubled meanwhile. None of these waits is a
   safety rule.
3. **The fresh signal measures price change since the window start, not
   recent acceleration.** A token that pumped 300% and is now fading still
   shows "price up since window start" and passes that condition. The
   signal cannot distinguish "early in the move" from "after the move".
4. **Data assembly is sequential RPC.** Each evaluation waits for each call
   in turn. The timings are recorded (`evidence.timings_ms`), but whether
   this matters versus the timers above must be measured on the server.
5. **LIVE adds about 3 s of confirmation latency and the trades are tiny.**
   Server output from 2026-10-09 shows LIVE buys of 0.0014 to 0.01 SOL
   (limited by risk-per-trade with a ~0.109 SOL wallet). At that size the
   fixed network and priority fees are a large share of the trade, so a
   late entry plus fees leaves little room. This is a sizing fact, not a
   timing bug. The size was not changed.

## 3. What was ruled out

- **Stream delay.** The create event arrives in about a second (on-chain
  timestamps vs receive time). The new `received_at` field in the create
  meta now records this per token.
- **Safety filters as "the" cause.** The filters reject or wait. They do not
  delay an eligible token. The waits come from timers and from the
  volatility data requirement.
- **PumpSwap (migrated) entries.** Migrated tokens are not on the pump
  stream, so they are not part of the fresh latency. They are measured
  separately (migrated variants, section 4 of the report).

## 4. Changes made (none touch a safety rule, threshold or size)

| Cause | Change | File |
|---|---|---|
| Funnel cadence | fast pass every 2 s for launches whose window just ended; the full pass keeps its 10 s | `engine-solana-discovery/app/funnel.py` (`fast_pass`, `matured`), `app/main.py` |
| Loop sleep | the decision loop waits on a wake list (`yx:gate:wake`, BLPOP) and otherwise every 5 s; new candidates first, newest first; legacy candidates keep 15 s | `decision-engine/app/main.py`, `core gate_events.py` |
| 30 s pacing | a meaningful market event (significant buy, +1 SOL net inflow, phase change, seller surge, creator sell, liquidity change, migration) can bring the next evaluation forward to `event_min_interval_seconds` (default 10). The 30 s fallback stays: nothing is evaluated less often | `gate_eval.py`, `gate_events.py` |
| Signal blind to acceleration | three competing entry strategies computed from the stream in SHADOW (early acceleration, smart-wallet confirmation, momentum continuation), each answering CANDIDATE / WAIT / NO_TRADE with reasons | `core entry_intel.py`, `engine-solana-discovery/app/entry_shadow.py` |
| No timeline | every stage time-stamped per token (Redis `yx:ee:tl:{mint}` + existing DB timestamps) | `core entry_timing.py`, `tools/entry_timing.py` |

The existing pipeline is unchanged as the champion (`CURRENT_GATE_ENTRY`).
A new strategy can only be put in PAPER mode by the operator, and even then
its candidates go through the same safety gate and can never become a LIVE
order (`paper_only`).

## 5. How to measure (server)

```
cd /opt/yonixalpha
C="docker compose --env-file .env -f infra/docker/docker-compose.yml -f infra/docker/docker-compose.prod.yml"
$C exec -T api python -m yonixalpha_core.tools.entry_timing --hours 24
$C exec -T api python -m yonixalpha_core.tools.entry_timing --hours 24 --entered-only
```

Run it once right after deploying this change. The tool is new, but the
"before" numbers come from database timestamps that already exist for the
candidates of the previous 24 h. Then run it again 24 h later. The dashboard page "Entry Intelligence" shows the same
data. Where a launch time or a price was not recorded, the tool prints "-".
It does not estimate a missing value.
