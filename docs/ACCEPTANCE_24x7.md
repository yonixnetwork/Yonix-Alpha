# 24/7 acceptance test (master §62, §81)

The server is the trading heartbeat. With the browser closed and the phone
offline, it must keep discovering, monitoring, copying, managing positions,
updating exits and recording PnL. After a restart it must reconcile.

This is an operator procedure on the server, because only the real
deployment can show it. The check is not "the dashboard looked fine": it is
`tools.acceptance_247`. That tool reads what the workers wrote during the
unattended window and prints a verdict per duty. It is read-only.

## What the report checks

| §81 duty | Measured from | Kind |
|---|---|---|
| Discovering | Solana launches observed (`token_observations.launched_at`); BSC and Robinhood launches (`evm_tokens.created_at`) | continuous |
| Monitoring | Solana observations decided; BSC and Robinhood launchpad trades (`evm_trades.at`); EVM observations moving; launchpad health probe | continuous |
| Copying | target wallet trades seen and decided (`copy_events.detected_at`) | market-dependent |
| Managing positions | BSC / Robinhood exit checks per open position (`evm_exit_samples.at`); Solana position loop last pass; open positions and their oldest mark | market-dependent |
| Updating exits | position timeline events (`trade_timeline_events`) | market-dependent |
| Recording PnL | positions closed with a realized PnL (`paper_positions.exit_at`) | market-dependent |
| Services | each service's heartbeat now (stale after 90 s, as on the health page) and its restarts in the window | — |
| Restart | LIVE wallet reconciliation time, reconciliation findings, EVM scan ranges skipped and backfilled | — |
| History | rows from before the window, still there for the dashboard | — |

For each duty the report gives:
- the number of events in the window;
- the newest event;
- the longest silence, the window's edges included, against a limit.

The verdicts:

| Verdict | Meaning |
|---|---|
| ACTIVE | events, and no silence longer than the limit |
| GAP | events, but a silence longer than the limit: a stop, or a quiet market. Check the services' restarts and the system events for that time |
| FAIL | no event from a source that never stops. Solana / BSC / Robinhood discovery and trades are such sources: both EVM chains always run in data-evm, so this is FAIL unless every launchpad of the chain was switched off on the Launchpads page |
| NO EVENT | no event where one depends on the market: copying needs a target to trade, exits need an open position. Not a failure, and not a proof either |

The result is **PASS** only with no FAIL and no missing or stale heartbeat.
It proves continuity, never profit.

## Procedure

Set this once per SSH session (as in `DIAGNOSTICS_COMMANDS.md`):

```bash
cd /opt/yonixalpha
C="docker compose --env-file .env -f infra/docker/docker-compose.yml -f infra/docker/docker-compose.prod.yml"
```

1. **Start.** On the dashboard, set the engines to AUTO as you normally run
   them (paper is fine; LIVE is not needed for this test). Note the UTC time:

   ```bash
   date -u +%Y-%m-%dT%H:%M:%SZ
   ```

   If any copy targets are set, keep them; copying is then exercised too.

2. **Leave.** Close every browser tab of the dashboard. Turn mobile data off on
   the phone. Do not log in anywhere.

3. **Wait.** At least 2 hours; overnight is better. The longer the window,
   the more market-dependent duties get a chance to happen.

4. **Measure.** Reconnect over SSH only (not the dashboard) and run:

   ```bash
   $C run --rm decision-engine python -m yonixalpha_core.tools.acceptance_247 --since <the time from step 1>
   ```

   Keep the output: add `> acceptance_unattended.txt 2>&1`.

5. **Reopen the dashboard.** Check:
   - System Health: every service green;
   - Fresh Token Observation and the EVM token lists: launches from the
     unattended hours;
   - Positions: entries and exits in that window, with PROFIT / LOSS;
   - Copy page: target trades in that window, if a target traded.
   These are the rows the report counted.

6. **Restart and reconcile.** Note the time again, then restart the stack:

   ```bash
   date -u +%Y-%m-%dT%H:%M:%SZ
   $C restart   # every container; scripts/deploy.sh only recreates changed ones
   ```

   A reboot of the droplet (`sudo reboot`) is the stronger test; the
   containers must come back by themselves (restart policy).

   Wait 15 minutes, then run the report from the time just noted:

   ```bash
   $C run --rm decision-engine python -m yonixalpha_core.tools.acceptance_247 --since <the restart time>
   ```

   Expected:
   - every service shows one restart;
   - every heartbeat is OK;
   - discovery is ACTIVE again;
   - on LIVE, "LIVE wallet reconciled at" is after the restart;
   - EVM scan ranges skipped during the restart are listed, and backfilled.

   On LIVE also check the log line:

   ```bash
   $C logs --since 20m paper-trading | grep live.reconciled
   ```

   Open positions must still be open and marked: "OPEN POSITIONS now", with
   a recent oldest mark.

7. **Report.** Send both outputs back.

## What is tested in code (not a substitute for the server run)

- `tests/test_acceptance_247.py`, on the real schema:
  - an hour with steady Solana discovery is ACTIVE;
  - a BSC trade feed with a 20-minute silence is GAP (21 minutes, measured);
  - a chain with no events is FAIL;
  - copying with no event is NO EVENT;
  - a closed position is ACTIVE;
  - a restart is counted and a stale heartbeat is caught;
  - history from before the window is counted;
  - PASS only without FAIL and with every service alive.
- Restart and reconciliation in the workers:
  - `test_restart_with_a_signed_order_resolves_it_without_rebuying`;
  - `test_worker_reconciles_before_processing_and_then_reports_ready`;
  - `test_reconcile_flags_missing_tokens_and_records_unknown_holdings_once`;
  - data-evm `test_restart_restores_announced_contracts`;
  - the re-scan in `test_pipeline_discovery_safety_entry_exit`;
  - `test_backlog_reorg_and_resume_handling` (streams).

## Status

NOT VERIFIED until the procedure above has run on the server and both
reports are back. Nothing in this repository can prove continuity on the
real deployment.
