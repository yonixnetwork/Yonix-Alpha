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
| Why losing trades were entered | `$C run --rm paper-trading python -m yonixalpha_core.tools.loss_report --mode LIVE --hours 72` | Per loss: PnL, MFE/MAE, exit reason, the entry decision's features, the exit check at entry, volatility source, data errors, every warning at entry, and the LOSS_ANALYSIS class. Then how many decisions were blocked by missing volatility, grouped by reason. |
| Candidate funnel | `$C run --rm paper-trading python -m yonixalpha_core.tools.execution_funnel` | Where every token stopped (DISCOVERED … POSITION OPEN) and the exact final blocker. |
| One order in detail | `$C run --rm paper-trading python -m yonixalpha_core.tools.order_inspect --last 5` | Stages, venue, guard result; decodes the transaction the guard refused. |
| Build + guard + simulate (no signing) | `$C run --rm paper-trading python -m yonixalpha_core.tools.exec_dryrun <MINT> --sol 0.01` | That the working buy path still builds and simulates on the real chain. |
| Runtime config revision | `$C run --rm decision-engine python -m yonixalpha_core.tools.rpc_check` (section "LOADED BY THE RUNNING SERVICES") | The database configuration revision, and the revision each service runs (SYNCED / behind). |
| Stream heartbeat | `$C exec -T redis sh -c 'redis-cli -a "$REDIS_PASSWORD" --no-auth-warning GET yx:pump:hb'` | When the Pump.fun stream last delivered an event. |

To keep the output for sending, add `> report.txt 2>&1`.
