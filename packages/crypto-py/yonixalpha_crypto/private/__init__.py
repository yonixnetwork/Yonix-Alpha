"""Exchange accounts: the only part of yonixalpha_crypto that handles API
keys or signs requests. Everything else in the package stays public-only
(publichttp) and a static test keeps it that way.

- environments: the fixed host of each (exchange, environment). DEMO /
  TESTNET keys can only reach demo hosts, LIVE keys only live hosts; there
  is no override and no fallback from one to the other.
- secretstore: keys encrypted at rest (Fernet); never returned once saved.
- livelock: real orders are locked in code (LIVE_ORDERS_UNLOCKED = False)
  and also need every per-connection gate.
- signedhttp: the signing client. A path allowlist per exchange; transfer
  and withdrawal endpoints are not on it, so they can never be called.
- connections: saving, testing (read-only) and describing connections.
"""
