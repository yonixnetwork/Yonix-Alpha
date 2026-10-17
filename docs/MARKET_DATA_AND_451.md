# Binance HTTP 451, public market data, and strategy testing without a Binance key (2026-10-17)

Live trading stays locked (`LIVE_ORDERS_UNLOCKED = False`). Nothing here uses
a VPN, a proxy, a false location, another host to get around a block, or a
demo-to-live fallback.

## 1. The HTTP 451: what the code proves, and what the server must confirm

### What the code shows (inspected in this repository)

| Question | Answer from the code |
|---|---|
| Where did the request start? | In the **API container on the server**, never in the browser. The dashboard calls `POST /api/crypto/connections/{id}/test` (`apps/api/app/api/routes/crypto_connections.py`), which runs `connections.test()` (`packages/crypto-py/yonixalpha_crypto/private/connections.py`). The exchange therefore sees the server's IP address (DigitalOcean NYC3, United States). |
| Which host? | The fixed host of the connection's environment (`private/environments.py`). Binance **Demo Mode** = `https://demo-api.binance.com`; Binance **Testnet** = `https://testnet.binance.vision`; LIVE = `https://api.binance.com`. No setting can change these hosts, and a failed environment never falls back to another one. |
| Which endpoint returned 451? | Before this change, the test's **first** request was the signed `GET /api/v3/account` on that host. So the 451 answered the account request. The old test could not show whether the host's public endpoints are blocked too. |
| Did Binance send it, or our application? | **Binance.** Nothing in this repository produces the words "restricted location" or "Eligibility"; the only match is a test fixture that imitates Binance. The message shown is Binance's own response body, passed through `ExchangeRequestError` with the HTTP status. |
| How was it shown? | As connection status `FAILED`, with Binance's text. Nothing separated a location block from a key problem. |

### What still needs evidence from the server (read-only)

Each command below only reads information or sends a public, keyless request.

1. **Which environment the failing connection uses**, and its stored result:
   ```
   cd /opt/yonixalpha && docker compose --env-file .env -f infra/docker/docker-compose.yml -f infra/docker/docker-compose.prod.yml exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "select exchange, environment, label, status, checked_at, left(last_error, 160) from crypto.api_connections order by checked_at desc nulls last"'
   ```
2. **The recorded test events:**
   ```
   cd /opt/yonixalpha && docker compose --env-file .env -f infra/docker/docker-compose.yml -f infra/docker/docker-compose.prod.yml exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "select at, message, detail from crypto.events where source = '"'"'connections'"'"' order by at desc limit 10"'
   ```
3. **Each Binance host's public time endpoint from the server.** This sends no key and changes nothing.
   ```
   for u in https://demo-api.binance.com https://testnet.binance.vision https://api.binance.com https://data-api.binance.vision https://api.binance.us; do echo "$u $(curl -s -o /dev/null -w '%{http_code}' $u/api/v3/time)"; done
   ```

**How to read the answers:**
- If the **public** endpoint of the demo or testnet host also answers 451, with no key sent, the block depends on the server's location and not on the account.
- If the public endpoint answers 200 and only the signed request answers 451, the restriction is tied to the account or key.

The new connection test (section 2) records exactly this split on every run.

**Remaining uncertainty until those outputs arrive:**
- which of Demo Mode or Testnet was selected;
- whether the public endpoints of that host are blocked too.

A 451 for `api.binance.com` from this server was already observed on 2026-10-15.

## 2. Connection test: four separate results

`connections.test()` now runs these checks in order. Each one is stored on the connection (`crypto.api_connections.checks`, migration `c0005`) and shown on the Connections page.

| Check | What is sent | What it proves |
|---|---|---|
| Public market data | Unsigned `GET /api/v3/time` (Bybit: `/v5/market/time`) to the environment's own host. **No key.** | Only that the server can reach that host. It does **not** prove the key, its permissions or the account's eligibility. |
| Account authentication | Signed account read (`GET /api/v3/account`; Bybit wallet balance). | The key and signature work. It also reads the key's permissions and a balance summary. |
| Practice order validation | Binance practice environments only: `POST /api/v3/order/test` (MARKET BUY for 10 USDT of BTCUSDT). The exchange validates it; **nothing is placed**. Bybit has no test-order endpoint, so this check is "not checked". | That the order path accepts the key. |
| Live orders | Nothing is sent. | Always **LOCKED** in code. On a LIVE connection no order request is ever sent, not even a test order. |

**If the public check finds the host unusable** (451, 403, timeout or unreachable):
- the signed checks are skipped, so no key is sent to a host that refuses the server;
- the connection is marked `BLOCKED` or `UNREACHABLE`.

**How failures are classified** (`private/diagnose.py`): by HTTP status and the exchange's own error code, never by the wording alone.

| Status or code | Result |
|---|---|
| 451 | `geo_restricted`. The message says plainly that this is the exchange's location restriction, not a key or password problem. |
| 403 | `forbidden` |
| 429, 418 | `rate_limited` |
| 401, or Binance -2014 / -2015 / -1022, or Bybit 10003 / 10004 / 10005 / 33004 | `auth_failed` (status `AUTH_FAILED`) |
| No answer | `timeout` or `unreachable` |
| A body that is not JSON | `malformed` |
| 5xx | `exchange_error` |

Messages carry the host, status and code, never a key (tested).

## 3. Public market data without a private key

### Already working

Paper trading on **Binance.US public market data** (`api.binance.us`). The server reached it with HTTP 200 on 2026-10-16 and 2026-10-17.
- Paper trading needs no key and no demo account.
- Binance.US is a separate exchange with its own prices, listings and fees (0 / 2 bps), labelled as such everywhere.

### New: Binance's market-data-only host

The new provider is `binance_data`, at `https://data-api.binance.vision`.

**From Binance's FAQ ("market_data_only.md"):**
- "These URLs do not require any authentication (i.e. The API key is not necessary) and serve only public market data."
- Endpoints include `/api/v3/klines`, `/ticker/price`, `/ticker/bookTicker`, `/exchangeInfo`, `/time` and `/ping`.

**What the code does:**
- It accepts only GET requests on those public paths, refuses any signature or key parameter or header before sending (`publichttp.check_request`), and has no account environment, so a key can never be sent there (tested).
- The worker's health check probes it from the server, like the other exchanges. It is shown as healthy only after a real 200 from the deployment.
- The local runner can download candles from it.
- It is **not selectable for strategies** (`registry.SPOT_EXCHANGES` does not include it). The terms question below comes first.

**Terms: an operator decision.**
- Binance's Vision Dataset Terms say they are "supplemental to … and subject to the Binance Terms of Use". The 451 cites the Eligibility section of those Terms of Use.
- The datasets are licensed CC BY-NC-SA 4.0. Permitted uses include "algorithmic historical backtesting for purely personal non-production research". Section 4.2 excludes "live proprietary trading execution".
- **Local historical backtesting on downloaded Binance public data is within those terms.**
- **Using Binance public data for paper trading from a US-located server is not clearly permitted.** It is left off until you decide. Enabling it means adding `binance_data` to `registry.SPOT_EXCHANGES`, a one-line, reviewed change, and only if the server's health check shows HTTP 200.

### Historical downloads (local only)

- **Monthly spot kline files** from `https://data.binance.vision/data/spot/monthly/klines/<SYMBOL>/<interval>/<SYMBOL>-<interval>-<YYYY-MM>.zip`.
- **Verify each file** with its `.CHECKSUM` file: `sha256sum -c <file>.zip.CHECKSUM` (macOS: `shasum -a 256 -c ...`).
- **Load them** with `python -m yonixalpha_crypto.backtest.local convert ...`:
  - it reads millisecond timestamps (before 2025) and microsecond timestamps (from 2025);
  - it removes duplicate rows;
  - it records the file names and checksums as provenance.
- **Alternatively, `fetch`** with `"exchange": "binance_data"` (or `binance_us`) in `batch.json` downloads from the public API.

**How the runner treats candles:**
- only closed candles are stored;
- gaps are counted and reported, and coverage below 98% lowers a "candidate" verdict;
- incomplete candles are never treated as completed bars.

## 4. The four execution modes (separate by construction)

| Mode | Data | Orders | Needs a Binance key or demo account? |
|---|---|---|---|
| Historical backtest | Local candle files (downloaded public data) | Simulated (`backtest.engine`) | No. It runs on your computer and never on the server (`backtest.policy`). |
| Paper (DEMO) | Public live market data (Binance.US today) | Simulated (`demo.py`): virtual balance, fees, slippage, stop loss, take profit, trailing stop, partial exits, trade history | No. The signing client is never built in this mode (tested). |
| Exchange demo | The practice account's host | Real practice orders through the executor | Yes: an eligible practice account that is reachable from the server. Shown as "exchange demo unavailable" when its connection is blocked. |
| Live | Real account | Real orders | Locked in code. A failed demo never falls back to live. |

## 5. Strategy Center

**Statuses** come only from stored facts:
- backtest pending, backtest running, backtest completed, backtest failed (the run itself failed);
- paper trading active;
- exchange demo active, exchange demo unavailable (with the connection's own error, e.g. the 451);
- rejected / disabled.

A poor result is a *completed* backtest whose verdict is "failed".

**Metrics are shown only when stored:**
- out-of-sample net PnL, win rate, profit factor, max drawdown, trades;
- fees plus slippage;
- test period and data source;
- per-strategy paper open and closed positions with realized PnL;
- the paper account balances.

## 6. Deployment and rollback

- **Deploy:** api, web and crypto-trading (targeted, as before). Migration `c0005` (one nullable column) runs automatically at API start. Memecoin stays paused; `deploy.sh` does not start it.
- **Rollback, code only:**
  - Redeploy the previous main commit. The extra column is harmless to the old code.
  - Optionally, while the new code is still running: `docker compose ... exec -T api python -m yonixalpha_crypto.migrate downgrade c0004` drops only that column.
