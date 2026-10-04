# Read-only diagnostics commands

Run these on the server in `/opt/yonixalpha` after deploying. None of them
signs, sends or changes anything, and none of them prints a key or a full
RPC URL (only `scheme://host`). Paste the output back for analysis.

Set this once per SSH session:

```bash
cd /opt/yonixalpha
C="docker compose --env-file .env -f infra/docker/docker-compose.yml -f infra/docker/docker-compose.prod.yml"
```

| What | Command | Measures |
|---|---|---|
| RPC health and capabilities | `$C run --rm decision-engine python -m yonixalpha_core.tools.rpc_check --capabilities` | For each endpoint: getSlot test, then a probe of getLatestBlockhash, getBalance, getTokenAccountsByOwner, getSignaturesForAddress, getTransaction (declaring version 1), getSignatureStatuses and simulateTransaction (unsigned dummy). Also which transaction versions it returned, and what each running service learned: unsupported methods, 401/403, 429s by method, versions served. |
| A real version-1 transaction | `$C run --rm paper-trading python -m yonixalpha_core.tools.tx_fixture --version 1` | Prints one mainnet v1 transaction exactly as the RPC renders it, plus each endpoint's answer for the same signature. This becomes the v1 regression fixture; no v1 layout is invented. |
| Trade latency and price execution | `$C run --rm paper-trading python -m yonixalpha_core.tools.trade_report --last 30` | Per LIVE order: decision→submit, decision→confirm, queue wait, quote, build, guard and re-check, simulation, submission, confirmation, and slots to land. For buys, the price chain decision → build → before our trade → trade → all-in, with its cause. Summary: average, median and worst of each step; causes; failures; confirmation speed per priority setting. Orders placed before this deploy are measured from their recorded stages. |
| Where the SOL of each trade went | `$C run --rm paper-trading python -m yonixalpha_core.tools.cost_report --last 20` | Per confirmed LIVE order, from the transaction itself: trade amount, program fees (protocol, creator, LP), network + priority fee, SOL deposited into accounts the transaction created (a token account's deposit is rent, returned only if that account is closed), refunds, and any unexplained residual. Then the wallet's token accounts: how many are empty and how much SOL of rent they hold. |
| Rent reclaims | `$C run --rm paper-trading python -m yonixalpha_core.tools.order_inspect --last 5` (RENT orders appear with their stages) and `cost_report` (a RENT row shows the refunds; the empty-account total drops) | What was closed, the refund, and what is still locked. |
| Why losing trades were entered | `$C run --rm paper-trading python -m yonixalpha_core.tools.loss_report --mode LIVE --hours 72` | Per loss: PnL, MFE/MAE, exit reason, the entry decision's features, the exit check at entry, volatility source, data errors, every warning at entry, and the LOSS_ANALYSIS class. Then how many decisions were blocked by missing volatility, grouped by reason. |
| Candidate funnel | `$C run --rm paper-trading python -m yonixalpha_core.tools.execution_funnel` | Where every token stopped (DISCOVERED … POSITION OPEN) and the exact final blocker. |
| One order in detail | `$C run --rm paper-trading python -m yonixalpha_core.tools.order_inspect --last 5` | Stages, venue, guard result; decodes the transaction the guard refused. |
| Build + guard + simulate (no signing) | `$C run --rm paper-trading python -m yonixalpha_core.tools.exec_dryrun <MINT> --sol 0.01` | That the working buy path still builds and simulates on the real chain. |
| Runtime config revision | `$C run --rm decision-engine python -m yonixalpha_core.tools.rpc_check` (section "LOADED BY THE RUNNING SERVICES") | The database configuration revision, and the revision each service runs (SYNCED / behind). |
| Meteora DBC quotes against the chain | `$C run --rm decision-engine python -m yonixalpha_core.tools.dbc_verify` | Decodes recent DBC swaps and the most active pools (layout check), then replays consecutive swaps of each pool from the price the previous one left, in each swap's own mode (exact in, partial fill, exact out): next sqrt price and curve amount must be exactly equal. PASS / FAIL per pool. Read-only, background priority. |
| Raydium LaunchLab quotes against the chain | `$C run --rm decision-engine python -m yonixalpha_core.tools.launchlab_verify` | Decodes recent LaunchLab TradeEvents, the pools and their configs (layout check against the events), then replays every trade from the pool amounts the event reports from before it: traded amounts and the pool's change must be exactly equal, per curve type. Also prints which pools a quote can serve (curve types, Token-2022 flags, statuses). Read-only, background priority. |
| 24/7 acceptance (master §81) | `$C run --rm decision-engine python -m yonixalpha_core.tools.acceptance_247 --since <UTC time>` | Per service: heartbeat now and restarts in the window. Per duty (discovering, monitoring, copying, managing positions, updating exits, recording PnL): events in the window, the newest, the longest silence vs its limit, as ACTIVE / GAP / FAIL / NO EVENT. Restart evidence (LIVE reconciliation, EVM scan gaps) and history before the window. Procedure: `docs/ACCEPTANCE_24x7.md`. |
| Stream heartbeat | `$C exec -T redis sh -c 'redis-cli -a "$REDIS_PASSWORD" --no-auth-warning GET yx:pump:hb'` | When the Pump.fun stream last delivered an event. |
| Position-loop cadence | `$C exec -T redis sh -c 'redis-cli -a "$REDIS_PASSWORD" --no-auth-warning GET yx:pm:last_pass'` | Last pass of the open-position loop: `at` (should be within a few seconds of now), `pass_ms`, positions managed / closed / unpriced. |
| SOL/USD rate | `$C exec -T redis sh -c 'redis-cli -a "$REDIS_PASSWORD" --no-auth-warning GET yx:sol_usd'` | The rate the dashboard converts market caps with (empty = "USD unavailable"). |

To keep the output for sending, add `> report.txt 2>&1`.
