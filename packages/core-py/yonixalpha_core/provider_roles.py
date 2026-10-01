"""Provider roles and plan health (master upgrade §48-53).

Roles (§49). Each RPC endpoint may be given roles; a request prefers the
endpoints holding the role of its method:

  DISCOVERY        finding launches and trades (EVM eth_getLogs)
  MARKET_DATA      reserves, quotes, accounts, supply, blocks
  EXECUTION        building and sending transactions, fees, simulation
  CONFIRMATION     receipts, signature statuses, transactions by hash
  HISTORICAL_DATA  past-block reads, signature history (archive)
  WALLET_DATA      balances, token accounts, nonces

An endpoint without roles serves every role (the default, so nothing
changes until roles are set). When no endpoint holds a method's role the
request still goes to any usable endpoint, counted as a role fallback:
roles steer traffic, they never stop it.

Plan health (§53). Built from what services observed on real requests
(capability matrix, refused methods, 429s, the eth_getLogs span an endpoint
actually serves) plus the configuration. Each finding names the provider,
the plan the operator recorded for it ("not stated" otherwise), the
capability, the observed limitation, its impact and a recommendation.
UPGRADE_REQUIRED is only raised on an observed limitation or on a
production path with nothing but public endpoints, never guessed from a
provider's name.
"""

from __future__ import annotations

from typing import Any

DISCOVERY, MARKET_DATA, EXECUTION, CONFIRMATION, HISTORICAL, WALLET = (
    "DISCOVERY", "MARKET_DATA", "EXECUTION", "CONFIRMATION", "HISTORICAL_DATA", "WALLET_DATA")
ROLES = (DISCOVERY, MARKET_DATA, EXECUTION, CONFIRMATION, HISTORICAL, WALLET)
UPGRADE, CONFIGURATION, INFO = "UPGRADE_REQUIRED", "CONFIGURATION", "INFO"

SOLANA_METHOD_ROLES = {
    "sendTransaction": EXECUTION, "simulateTransaction": EXECUTION, "getLatestBlockhash": EXECUTION,
    "getRecentPrioritizationFees": EXECUTION, "getFeeForMessage": EXECUTION,
    "getSignatureStatuses": CONFIRMATION, "getTransaction": CONFIRMATION,
    "getSignaturesForAddress": HISTORICAL, "getBlock": HISTORICAL,
    "getBalance": WALLET, "getTokenAccountsByOwner": WALLET, "getTokenAccountBalance": WALLET,
}
EVM_METHOD_ROLES = {
    "eth_getLogs": DISCOVERY,
    "eth_sendRawTransaction": EXECUTION, "eth_estimateGas": EXECUTION, "eth_gasPrice": EXECUTION,
    "eth_maxPriorityFeePerGas": EXECUTION, "eth_feeHistory": EXECUTION,
    "eth_getTransactionReceipt": CONFIRMATION, "eth_getTransactionByHash": CONFIRMATION,
    "eth_getBalance": WALLET, "eth_getTransactionCount": WALLET,
}
_LATEST = (None, "latest", "pending", "safe", "finalized")


def solana_role(method: str) -> str:
    return SOLANA_METHOD_ROLES.get(method, MARKET_DATA)


_BLOCK_ARG = {"eth_call": 1, "eth_getBalance": 1, "eth_getTransactionCount": 1, "eth_getCode": 1,
              "eth_getStorageAt": 2}


def evm_role(method: str, params: list | None = None) -> str:
    """A read at a numbered (past) block is HISTORICAL_DATA: it needs archive state."""
    i = _BLOCK_ARG.get(method)
    tag = (params or [None] * 3)[i] if i is not None and params and len(params) > i else None
    if isinstance(tag, str) and tag.startswith("0x") and tag not in _LATEST:
        return HISTORICAL
    return EVM_METHOD_ROLES.get(method, MARKET_DATA)


def parse_roles(v: Any) -> tuple[tuple[str, ...], list[str]]:
    vals = [str(x).upper() for x in (v or [])]
    bad = [x for x in vals if x not in ROLES]
    return tuple(dict.fromkeys(x for x in vals if x in ROLES)), ([f"unknown roles {bad}; one of {list(ROLES)}"] if bad else [])


def prefer(endpoints: list, role: str, stats: dict | None = None) -> list:
    """Endpoints holding `role` (or no roles at all), in the given order;
    every endpoint when none does (counted in stats[role])."""
    holders = [e for e in endpoints if not getattr(e, "roles", ()) or role in e.roles]
    if holders:
        return holders
    if stats is not None and endpoints:
        stats[role] = stats.get(role, 0) + 1
    return endpoints


# --- plan health -------------------------------------------------------------------------------------

def _finding(severity: str, chain: str, provider: str, plan: str | None, capability: str, observed: str,
             impact: str, recommendation: str, **evidence: Any) -> dict[str, Any]:
    return {"severity": severity, "chain": chain, "provider": provider, "current_plan": plan or "not stated",
            "capability": capability, "observed": observed, "impact": impact, "recommendation": recommendation,
            "evidence": evidence}


SOLANA_REQUIRED = {
    "sendTransaction": ("sendTransaction", EXECUTION,
                        "AUTOMATIC SELL LATENCY MAY BE LIMITED BY CURRENT RPC PLAN: live orders cannot be sent through "
                        "this endpoint", "a plan or provider that accepts sendTransaction (Helius, QuickNode, Triton)"),
    "simulateTransaction": ("simulateTransaction", EXECUTION, "transactions cannot be simulated before sending",
                            "a plan that allows simulateTransaction"),
    "getProgramAccounts": ("getProgramAccounts", MARKET_DATA, "holder / pool scans are refused",
                           "a plan with getProgramAccounts (paid tiers of most providers)"),
    "getTokenLargestAccounts": ("getTokenLargestAccounts", MARKET_DATA, "holder concentration cannot be read",
                                "a plan that serves getTokenLargestAccounts"),
    "getSignaturesForAddress": ("signature history", HISTORICAL, "creator / wallet history cannot be read",
                                "an archive-capable plan"),
    "getTransaction": ("transaction lookups", CONFIRMATION, "fills and confirmations cannot be parsed",
                       "a plan that serves getTransaction"),
}


def solana_findings(providers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """`providers`: the RPC page listing rows (label, name, plan, health from
    services' acks: capabilities, refused / unsupported methods, counters)."""
    out: list[dict[str, Any]] = []
    enabled = [p for p in providers if p.get("enabled")]
    for p in enabled:
        name, plan = p.get("name") or p.get("label"), p.get("plan")
        if p.get("auth_failed_now"):
            out.append(_finding(CONFIGURATION, "solana", name, plan, "authentication", "the provider refuses the key "
                                "(HTTP 401/403 on every method)", "the endpoint is skipped",
                                "check the API key in the URL or the provider dashboard", services=p.get("auth_failed_now")))
        refused = set(p.get("refused_methods") or []) | set(p.get("unsupported_methods") or [])
        for method, cap in (p.get("capabilities") or {}).items():
            if (cap or {}).get("status") in ("FORBIDDEN", "UNSUPPORTED"):
                refused.add(method)
        for method in sorted(refused):
            if method in SOLANA_REQUIRED:
                capability, role, impact, rec = SOLANA_REQUIRED[method]
                out.append(_finding(UPGRADE, "solana", name, plan, capability, f"{method} refused or not served",
                                    impact, rec, role=role))
        done = (p.get("successes") or 0) + (p.get("failures") or 0)
        rl = p.get("rate_limited_count") or 0
        if done >= 200 and rl / done >= 0.05:
            out.append(_finding(UPGRADE, "solana", name, plan, "throughput", f"{rl} HTTP 429 in {done} requests "
                                f"({rl / done:.0%})", "requests wait or move to slower endpoints",
                                "a higher request-per-second plan, or spread roles over several providers",
                                rate_limited_by_method=p.get("rate_limited_by_method")))
    if enabled and all(p.get("provider_type") == "public" or "api.mainnet-beta.solana.com" in (p.get("rpc_url") or "")
                       for p in enabled):
        out.append(_finding(UPGRADE, "solana", "all endpoints", None, "production RPC", "only public endpoints are "
                            "enabled", "public endpoints rate-limit hard and refuse heavy methods",
                            "add a keyed provider (Helius, QuickNode, Triton, Alchemy)"))
    for p in providers:
        if p.get("provider_type") == "helius":
            caps = p.get("capabilities") or {}
            ts = (caps.get("transactionSubscribe") or {}).get("status")
            if ts in ("FORBIDDEN", "UNSUPPORTED"):
                out.append(_finding(UPGRADE, "solana", p.get("name") or p.get("label"), p.get("plan"),
                                    "transactionSubscribe (enhanced WebSocket)", f"probe: {ts}",
                                    "transaction streaming falls back to logs / polling",
                                    "a Helius plan that includes enhanced WebSockets (check helius.dev/pricing)"))
    return out


EVM_RECOMMEND_LOGS = ("a plan that serves eth_getLogs over at least 2000 blocks (researched 2026-10-01: Alchemy Pay As "
                      "You Go has no block limit, QuickNode paid plans 10,000 blocks; free tiers 5-10 blocks)")


def evm_findings(chain: str, endpoints: list[dict[str, Any]], live: dict | None) -> list[dict[str, Any]]:
    """`endpoints`: rpc_registry.endpoints rows (label, name, source, plan,
    enabled); `live`: data-evm's last EvmRpc.health() for the chain."""
    out: list[dict[str, Any]] = []
    enabled = [e for e in endpoints if e.get("enabled")]
    by_host = {}
    for h in (live or {}).get("endpoints") or []:
        by_host.setdefault(h.get("url"), h)
    from yonixalpha_core.redact import redact_url

    for e in enabled:
        h = by_host.get(redact_url(e.get("url")))
        if not h:
            continue
        name, plan = e.get("name") or e["label"], e.get("plan")
        span = h.get("logs_span")
        if "eth_getLogs" in (h.get("unsupported_methods") or []):
            out.append(_finding(UPGRADE, chain, name, plan, "eth_getLogs (discovery)", "eth_getLogs refused",
                                "launch and trade discovery cannot use this endpoint", EVM_RECOMMEND_LOGS, role=DISCOVERY))
        elif span is not None and span < 100:
            out.append(_finding(UPGRADE, chain, name, plan, "eth_getLogs block range", f"served {span} blocks per "
                                "request (learned from real requests)", "discovery needs many requests per pass and "
                                "falls behind when launches are busy", EVM_RECOMMEND_LOGS, role=DISCOVERY))
        done = (h.get("ok") or 0) + (h.get("errors") or 0)
        rl = h.get("rate_limited") or 0
        if done >= 200 and rl / max(done, 1) >= 0.05:
            out.append(_finding(UPGRADE, chain, name, plan, "throughput", f"{rl} HTTP 429 in {done} requests",
                                "discovery and quotes wait on cooldowns", "a higher-throughput plan or another provider"))
    if enabled and all(e.get("source") == "public" for e in enabled):
        out.append(_finding(UPGRADE, chain, "all endpoints", None, "production RPC",
                            "only the chain's public endpoints are enabled", "public RPC must not be the only "
                            "production path (master §51): rate limits and log ranges are tight",
                            "add a keyed provider in RPC / Data Providers (Alchemy, QuickNode, Chainstack)"))
    # Robinhood Chain's sequencer feed is public and needs no provider WSS; BSC
    # pending transactions need one (master §9).
    if chain != "robinhood" and not any(e.get("ws_url") for e in enabled):
        out.append(_finding(INFO, chain, "all endpoints", None, "WebSocket / pending transactions",
                            "no WSS endpoint configured", "the pending-transaction stream is off: copy trading "
                            "uses confirmed trades (polling discovery is unaffected)",
                            "add a WSS URL to a provider whose plan streams full pending transactions"))
    return out


STREAM_RECOMMEND_PENDING = ("a provider plan that streams full pending transaction bodies "
                            "(eth_subscribe newPendingTransactions, true), or an own BSC full node")


def stream_findings(chain: str, reports: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """`reports`: data-evm's last StreamStats.report() per source for the
    chain (evm.streams). Only observed states raise findings."""
    out: list[dict[str, Any]] = []
    pending = reports.get("pending_tx")
    if pending and pending.get("state") in ("REFUSED", "LIMITED"):
        observed = ("the provider refuses the pending-transaction subscription" if pending["state"] == "REFUSED"
                    else "the provider streams transaction hashes only")
        out.append(_finding(UPGRADE, chain, pending.get("url") or "WSS endpoint", None,
                            "mempool / pending transactions", observed,
                            "copy-target transactions are seen only once confirmed in a block",
                            STREAM_RECOMMEND_PENDING, detail=pending.get("detail")))
    feed = reports.get("sequencer_feed")
    if feed and (feed.get("unverified") or 0) >= 10 and (feed.get("unverified") or 0) >= (feed.get("messages") or 0):
        out.append(_finding(CONFIGURATION, chain, feed.get("url") or "sequencer feed", None, "sequencer feed signature",
                            f"{feed['unverified']} messages failed the sequencer signature check "
                            f"({feed.get('messages') or 0} accepted)", "failing messages are dropped: the stream "
                            "sees nothing from them", "check the feed URL; if the sequencer key rotated, the signer "
                            "list in evm.streams needs the new batch poster (SequencerInbox.isBatchPoster)"))
    if feed:
        if feed.get("state") == "WRONG_CHAIN":
            out.append(_finding(CONFIGURATION, chain, feed.get("url") or "sequencer feed", None, "sequencer feed",
                                feed.get("last_error") or "the feed serves another chain", "the feed is not used",
                                "set the Robinhood Chain feed URL in the stream settings"))
        elif feed.get("fallback_active"):
            out.append(_finding(INFO, chain, feed.get("url") or "sequencer feed", None, "sequencer feed",
                                "the primary feed failed repeatedly; the delayed feed is in use", "transactions are seen later than on the primary feed",
                                "nothing to buy: the primary feed is retried automatically"))
        elif feed.get("state") == "RECONNECTING" and (feed.get("failures_in_row") or 0) >= 3:
            out.append(_finding(CONFIGURATION, chain, feed.get("url") or "sequencer feed", None, "sequencer feed",
                                f"{feed['failures_in_row']} failed connections in a row: {feed.get('last_error') or 'unknown'}",
                                "copy trading uses confirmed trades only", "check outbound WSS access from the server"))
    return out
