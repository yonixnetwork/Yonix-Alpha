# Local historical backtests

**Decision (operator, 2026-10-17):**
- Historical backtests run **locally**, on your own computer, on downloaded public candles.
- They **never run on the production VPS**.
- The dashboard's backtest feature stays disabled until this local workflow is ready and verified (section 7).

In code (`packages/crypto-py/yonixalpha_crypto/backtest/policy.py`):

| Switch | Value | Effect |
|---|---|---|
| `SERVER_BACKTESTS_ENABLED` | `False` | The queue worker refuses to start. The API refuses to queue a batch (HTTP 409). The "backtests" portfolio switch cannot be turned on. The dashboard has no "New batch" form. |
| `RESULT_IMPORT_ENABLED` | `False` | A results bundle can be imported only into a database on your own computer (localhost), for verification. Never into production. |

Changing either switch is a code change that goes through a reviewed PR. It is not a dashboard setting.

The local runner uses exactly the same code as everything else:
- the 20-strategy registry;
- the backtest engine;
- trade management, metrics and validation;
- one shared run function, `backtest/run.py`.

A local result is therefore computed exactly as the dashboard expects it.

Nothing in a backtest is evidence of future profit. A "candidate" verdict
only means "worth paper / exchange-demo testing". Live trading stays locked.

## 1. One-time setup on your computer

You need Python 3.11 or newer and git. On Windows, use PowerShell and replace
`source .venv/bin/activate` with `.venv\Scripts\Activate.ps1`.

```
git clone https://github.com/yonixnetwork/Yonix-Alpha.git && cd Yonix-Alpha
python -m venv .venv && source .venv/bin/activate
pip install -e packages/crypto-py
```

Use the same commit as the server, so the code versions match (section 6):
`git checkout <commit>`.

## 2. Configure a batch

```
python -m yonixalpha_crypto.backtest.local example-config > batch.json
```

Edit `batch.json`:

| Key | Meaning |
|---|---|
| `exchange` | Whose candles: `binance_us`, `binance` (Binance.com) or `bybit` |
| `symbols` | e.g. `["BTC/USDT", "ETH/USDT", "SOL/USDT"]`. Pair strategies (meta_muse) use the first as the lead and the last as the traded symbol. |
| `interval` | `15m`, `1h`, `4h` or `1d` |
| `start`, `end` | UTC period, e.g. two years. At most 40,000 candles per symbol. |
| `strategies` | `"all"` (the 20 modules) or a list of ids |
| `costs` | `taker_fee_bps`, `maker_fee_bps`, `spread_bps` (full spread; half is paid on each fill), `slippage_bps`, `max_participation` (share of the signal bar's volume one order may take), `funding_bps_per_day`, `min_notional`. Each must stay inside its allowed range. |
| `costs_note` | Where the fee numbers come from (copied into the report) |
| `risk` | `mode` (`PERCENT` or `FIXED`), `value`, `starting_equity`, optional `max_position_quote` |
| `optimize` | `true` = a 3 x 3 parameter grid chosen on the training period only, plus walk-forward |
| `params` | Optional, per strategy, e.g. `{"sma_cross": {"fast": 30}}`. Unknown keys and out-of-range values are refused. Runs with non-default parameters are flagged in the verdict. |
| `exits` | Optional exit plans per strategy (default: each module's own) |

## 3. Get the candles

Option A: download them from the exchange's public klines API (no key).
```
python -m yonixalpha_crypto.backtest.local fetch --config batch.json --data data
```

Option B: if the exchange's API is not reachable from where you are, use
Binance's public bulk files from https://data.binance.vision.
1. Download the spot klines ZIP files from `data/spot/monthly/klines/<SYMBOL>/<interval>/`.
2. Convert them, one symbol at a time:
   ```
   python -m yonixalpha_crypto.backtest.local convert --symbol BTC/USDT --interval 1h --data data --exchange binance BTCUSDT-1h-*.zip
   ```
3. Set `"exchange": "binance"` in `batch.json`, because these are Binance.com candles.

The converter reads both timestamp formats (milliseconds before 2025,
microseconds since) and removes duplicate rows.

Do not use a VPN or proxy to reach a blocked exchange. Use a source that
is reachable as it is.

Each symbol gets two files:
- `data/<exchange>_<SYMBOL>_<interval>.csv`, the closed candles;
- `.meta.json`, the provenance: source, host or the downloaded file names with their checksums, download time, row count, first and last candle, and the CSV's CRC32.

## 4. Check, then run

```
python -m yonixalpha_crypto.backtest.local plan --config batch.json --data data
python -m yonixalpha_crypto.backtest.local run --config batch.json --data data --out results --jobs 2
```

`plan` lists the runs, says whether each candle file is present, and estimates the time.

Measured on synthetic candles of the real size:
- about 15 seconds per run for 17,520 hourly candles (two years) with the grid on;
- under 100 MB of memory.

The full batch is 58 runs (20 strategies on three symbols, plus the pair strategy once), so about 15 minutes on one core. `--jobs N` uses N cores and gives the same result.

`run` writes two files to `results/`:
- `bundle-<code>-<checksum>.json`: the complete, machine-readable result;
- `bundle-<code>-<checksum>.md`: the human-readable report.

## 5. What every bundle and report contains

| Part | Content |
|---|---|
| Data provenance | Per symbol: source and host (or downloaded file names and checksums), candles used / expected, coverage %, number of gaps and examples, first and last candle, CSV checksum. A pair strategy also records its lead symbol. |
| Costs | Taker and maker fees, spread, slippage, volume participation cap, funding, minimum order value, plus your note on where the fees come from. |
| Risk | Mode, value, starting equity and position cap. |
| Configuration | The full batch configuration. Per run: parameters, exit plan, parameter source (defaults or config), module version, effective costs and risk. |
| Code version | `code_version` (a checksum of every file that decides a result), the git commit, Python version and platform. |
| Test periods | Per run, the exact train / validation / out-of-sample dates and candle counts. Also the walk-forward folds and the grid. |
| Results | Per run: the full study (metrics for each period, walk-forward, sensitivity, buy-and-hold benchmark, out-of-sample equity curve and trade sample), the verdict tier and its reasons, and a summary. The report ranks by tier, then out-of-sample expectancy (R), then drawdown. **Win rate is never used to rank.** |
| `content_crc` | Checksum of everything above except when and where it was produced. The same code and the same candle files always give the same `content_crc`. |

## 6. Verify (reproducibility)

```
python -m yonixalpha_crypto.backtest.local verify --bundle results/bundle-....json --data data
```

`verify` checks that:
1. the bundle has not been edited (checksum);
2. it was made with the code you have (otherwise it names the commit to check out);
3. the candle files are unchanged.

It then re-runs everything and compares the result. It prints
`REPRODUCED` only if every run matches exactly.

## 7. Import, and the verification that must come first

Importing stays off for production. Until the workflow is verified, a bundle can be imported only into a local database:

```
export DATABASE_URL=postgresql+asyncpg://USER:PASSWORD@localhost:5432/yonixalpha_local
python -m yonixalpha_crypto.migrate upgrade
python -m yonixalpha_crypto.backtest.local import --bundle results/bundle-....json
```

The second command creates the crypto tables in that local database. On Windows, use `$env:DATABASE_URL="..."` instead of `export`.

The import refuses:
- a bundle whose checksum does not match;
- a bundle made with different code or a different strategy module version;
- a summary that does not match its result;
- a bundle that was already imported.

Imported runs show up in the Strategy Center, on strategy pages and on the Backtests page of a local copy of the dashboard, with all the metadata above.

**"Ready and verified" means all of these are done and recorded:**
1. One real batch has run locally on real public candles, and its report has been read.
2. `verify` printed `REPRODUCED` for that bundle. Ideally it is also checked on a second computer, or after a fresh `pip install`.
3. The bundle has been imported into a local database. The dashboard figures match the report, and the provenance, costs, code version and periods are visible on each run.
4. Data coverage is at least 98% for every symbol, or every gap is explained.
5. The operator approved enabling production import.

Only then does a reviewed PR set `RESULT_IMPORT_ENABLED = True`, so a bundle can be imported on the server:
1. copy it there with `scp`;
2. run it with `docker compose ... exec -T api python -m yonixalpha_crypto.backtest.local import --bundle /path/in/container`.

An import is a small database write, not a backtest. `SERVER_BACKTESTS_ENABLED` stays `False`.

## 8. Troubleshooting

| Message | Meaning |
|---|---|
| `no candle file ...` | Run `fetch` or `convert` for that symbol first. The file name includes the exchange, symbol and interval. |
| `made with code src-..., this code is src-...` | Check out the commit recorded in `runner.git_commit`, then verify again. |
| `candle file changed since the run` | The CSV was re-downloaded or edited. Use the original file, or run a new batch. |
| `importing results is switched off ...` | Expected until section 7 is complete. Import into a localhost database only. |
| a run is `FAILED` with "not enough bars" | The period is too short for that strategy's longest indicator plus 200 bars. |
