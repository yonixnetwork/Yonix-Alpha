"""Where historical backtests may run (operator decision, 2026-10-17).

Historical backtests run LOCALLY, on the operator's own computer, on
downloaded public candles (`python -m yonixalpha_crypto.backtest.local`).
They never run on the production VPS: the queue worker refuses to start,
the API refuses to queue a batch, and the "backtests" portfolio switch
cannot be turned on.

Results come back as an exported bundle. Importing a bundle into a
database stays switched off (RESULT_IMPORT_ENABLED = False) until the
local-runner workflow has been verified end to end. Until then a bundle can
only be imported into a LOCAL database (localhost), for verification.
Turning it on is a reviewed code change, not a setting."""

SERVER_BACKTESTS_ENABLED = False
RESULT_IMPORT_ENABLED = False

SERVER_REFUSAL = ("historical backtests run locally on downloaded public candles, never on the production server: "
                  "use `python -m yonixalpha_crypto.backtest.local` on your own computer (docs/LOCAL_BACKTESTS.md)")
