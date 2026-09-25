# MT5 bridge

Lets YonixAlpha trade forex/CFDs through a MetaTrader 5 account. The official
`MetaTrader5` Python package only works on **Windows**, next to a running,
logged-in MT5 terminal. So this bridge runs on that Windows machine, and
YonixAlpha (on the Linux server) talks to it over HTTP with a bearer token.

```
YonixAlpha server (Linux)                         Windows host
  execution-futures ──HTTPS/WireGuard/SSH──►  mt5-bridge ──► MT5 terminal ──► broker
  decision-engine   (MT5_BRIDGE_URL + TOKEN)  (MT5_LOGIN/PASSWORD/SERVER live here only)
```

## Install (Windows)

1. Install MetaTrader 5 from your broker, log in once, enable
   *Tools → Options → Expert Advisors → Allow algorithmic trading*.
2. Install Python 3.11+ (64-bit), then in this folder:
   `pip install -r requirements.txt`
3. Set environment variables (System Properties → Environment Variables, or a
   service wrapper such as NSSM):

| Variable | Required | Purpose |
|---|---|---|
| `MT5_BRIDGE_TOKEN` | yes | Bearer token, ≥ 32 random characters. The same value goes into YonixAlpha's `.env` as `MT5_BRIDGE_TOKEN`. |
| `MT5_LOGIN` | yes | MT5 account number |
| `MT5_PASSWORD` | yes | MT5 trading password (secret) |
| `MT5_SERVER` | yes | Broker server name, e.g. `XMGlobal-MT5 6` |
| `MT5_TERMINAL_PATH` | no | Path to `terminal64.exe` if several terminals are installed |
| `MT5_BRIDGE_HOST` | no | Default `127.0.0.1` |
| `MT5_BRIDGE_PORT` | no | Default `9100` |
| `MT5_BRIDGE_MAGIC` | no | Magic number marking the bridge's positions (default `20260925`) |
| `MT5_BRIDGE_STATE_PATH` | no | Idempotency state file (default `mt5_bridge_state.json`) |

4. Run: `python -m app.main`

## Connect it to YonixAlpha

Do **not** expose the bridge on the public internet. Put the Windows host and
the YonixAlpha droplet on a private network (WireGuard, Tailscale, or an SSH
reverse tunnel such as `ssh -N -R 9100:127.0.0.1:9100 user@droplet`), then set
in YonixAlpha's `.env`:

```
MT5_BRIDGE_URL=http://<private address>:9100
MT5_BRIDGE_TOKEN=<same token>
```

Then choose venue `mt5` in the Confluence Matrix strategy configuration.

## What it does and refuses

- Market orders by client id (idempotent; a crash after sending is recovered
  from the terminal's deal history), closes only its own positions (magic
  number), stop-loss on its positions via `TRADE_ACTION_SLTP`.
- Rates (`copy_rates_from_pos`) and depth of market (`market_book_get`). If
  the broker publishes no depth of market, `/book` returns 404 and YonixAlpha's
  safety gate will not trade that symbol: depth is never invented from
  bid/ask.
- Refuses symbols whose profit currency differs from the account currency.

## Status

IMPLEMENTED — AWAITING CREDENTIAL VERIFICATION. Tested against an in-memory
fake of the MetaTrader5 API (`tests/fake_mt5.py`); not yet run against a real
terminal.
