# Robinhood Chain sequencer feed frames (test fixture)

`frames.jsonl` holds the first 12 lines of `tests/frames.jsonl` from
github.com/chainstacklabs/robinhood-chain-sequencer-feed at commit 8ea0972
(2026-09-25): real broadcast frames captured from
`wss://feed.mainnet.chain.robinhood.com`, each message signed by the
sequencer (`signatureV2`). Copyright the repository's authors, licensed under
the Apache License 2.0 (`LICENSE-APACHE-2.0`). Unmodified.

Used by `tests/test_evm_streams.py` to check the decoder, the sender recovery
and the feed signature check against real mainnet data. No code from that
repository is used.
