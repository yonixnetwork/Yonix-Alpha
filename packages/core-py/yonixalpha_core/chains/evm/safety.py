"""EVM token safety and sellability.

A token is never assumed sellable because it can be bought: the check
quotes a buy of the probe amount AND a sell of exactly the tokens that buy
returns, on the launchpad's own contracts (or the DEX it migrated to). It
also reads the token contract itself: code present, EIP-1967 proxy slots
(an upgradeable token can change its rules after entry), owner, and the
launchpad's own tax / restriction fields.

Verdicts: PASS, WARN, FAIL, UNKNOWN. UNKNOWN (an RPC or API that could not
answer) is never read as safe; automatic entries require PASS (WARN only
when the operator allows it). Honeypot.is (BSC) is enrichment: a positive
honeypot flag fails the token, a clean result never passes it on its own.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import httpx

from yonixalpha_core.chains.base import SafetyReport, TokenState
from yonixalpha_core.chains.evm import dex
from yonixalpha_core.chains.evm.rpc import EvmRpcError, EvmRpcUnavailableError
from yonixalpha_core.chains.evm.settings import ChainTradingSettings, EvmTradingSettings

EIP1967_IMPLEMENTATION = "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"
EIP1967_BEACON = "0xa3f0ad74e5423aebfd80d3ef4346578335a9a72aeaee59ff6cb3582b35133d50"
HONEYPOT_URL = "https://api.honeypot.is/v2/IsHoneypot"
PASS, WARN, FAIL, UNKNOWN = "PASS", "WARN", "FAIL", "UNKNOWN"


def _f(level: str, code: str, message: str, source: str, **detail: Any) -> dict[str, Any]:
    return {"level": level, "code": code, "message": message, "source": source, **detail}


def verdict_of(findings: list[dict[str, Any]]) -> str:
    levels = {f["level"] for f in findings}
    for v in (FAIL, UNKNOWN, WARN):
        if v in levels:
            return v
    return PASS


async def _contract_checks(adapter, token: str, out: list[dict[str, Any]]) -> None:
    rpc = adapter.rpc
    try:
        code = await rpc.get_code(token)
    except EvmRpcUnavailableError as exc:
        out.append(_f(UNKNOWN, "TOKEN_CODE_UNAVAILABLE", str(exc)[:160], "eth_getCode"))
        return
    if not code or code == "0x":
        out.append(_f(FAIL, "NO_CONTRACT_CODE", "no contract code at the token address", "eth_getCode"))
        return
    for name, slot in (("implementation", EIP1967_IMPLEMENTATION), ("beacon", EIP1967_BEACON)):
        try:
            v = await rpc.get_storage_at(token, slot)
        except EvmRpcUnavailableError as exc:
            out.append(_f(UNKNOWN, "PROXY_SLOT_UNAVAILABLE", str(exc)[:160], "eth_getStorageAt"))
            continue
        if v and int(v, 16) != 0:
            out.append(_f(FAIL, "UPGRADEABLE_TOKEN", f"EIP-1967 {name} slot is set: the token's code can be replaced",
                          "eth_getStorageAt", slot_value=v))
    try:
        (owner,) = await dex.call(rpc, token, "owner()", ["address"])
    except EvmRpcError:
        owner = None  # no owner() function
    except EvmRpcUnavailableError as exc:
        out.append(_f(UNKNOWN, "OWNER_UNAVAILABLE", str(exc)[:160], "owner()"))
        return
    try:  # Odyssey launch tokens: max-wallet limits while limitsActive()
        (limited,) = await dex.call(rpc, token, "limitsActive()", ["bool"])
        if limited:
            (mw,) = await dex.call(rpc, token, "maxWallet()", ["uint256"])
            out.append(_f(WARN, "WALLET_LIMITS_ACTIVE", f"transfers are limited: max wallet {mw}", "limitsActive()",
                          max_wallet=str(mw)))
    except (EvmRpcError, EvmRpcUnavailableError):
        pass  # no such function (or unreadable): not a restriction this check can see
    launchpad_contracts = {a.lower() for a in adapter.spec.contracts.values()}
    if owner and int(owner, 16) != 0 and owner.lower() not in launchpad_contracts:
        out.append(_f(WARN, "TOKEN_HAS_OWNER", f"token owner {owner} is not the launchpad and not renounced", "owner()",
                      owner=owner))


def _state_checks(adapter, state: TokenState, cs: ChainTradingSettings, s: EvmTradingSettings,
                  out: list[dict[str, Any]], head: int | None) -> None:
    src = state.source
    ex = state.extra or {}
    if ex.get("native_quote") is False:
        out.append(_f(FAIL, "NON_NATIVE_QUOTE", "quote asset is not the chain's native coin (unsupported)", src))
    if state.stage in ("KILLED", "INVALID", "IN_DUEL", "STAGED"):
        out.append(_f(FAIL, "NOT_TRADABLE_STATUS", f"launchpad status {state.stage}", src))
    if ex.get("extension_enabled"):
        out.append(_f(FAIL, "LAUNCHPAD_EXTENSION", f"token extension {ex.get('extension_id')} enabled", src))
    if adapter.spec.key == "pons_v2" and state.progress is not None and state.progress >= s.max_curve_progress_pons_v2:
        out.append(_f(FAIL, "NEAR_UNSUPPORTED_GRADUATION",
                      f"curve {state.progress:.0%} complete; graduation moves it to Uniswap V4, which is not tradable here", src))
    for side, bps, cap in (("buy", state.buy_tax_bps, cs.max_buy_tax_bps), ("sell", state.sell_tax_bps, cs.max_sell_tax_bps)):
        if bps is None:
            out.append(_f(WARN if state.stage != "DEX" else "INFO", f"{side.upper()}_TAX_UNREAD",
                          f"{side} tax not readable from the launchpad; the quote round trip is the measure", src))
        elif bps > cap:
            out.append(_f(FAIL, f"{side.upper()}_TAX_TOO_HIGH", f"{side} tax {bps} bps > limit {cap} bps", src, bps=bps))
    reb = ex.get("restrictions_end_block")
    if reb is not None and head is not None and head < int(reb):
        out.append(_f(WARN, "LAUNCH_RESTRICTIONS_ACTIVE",
                      f"launch restrictions (same-block, max wallet, buy caps) until block {reb}", src))


async def _round_trip(adapter, token: str, probe: int, cs: ChainTradingSettings, out: list[dict[str, Any]]) -> dict:
    buy = await adapter.quote_buy(token, probe)
    rt: dict[str, Any] = {"probe_in": str(probe), "buy": {"ok": buy.ok, "out": str(buy.amount_out), "source": buy.source,
                                                          "error": buy.error, "exact": buy.exact}}
    if not buy.ok:
        lvl = UNKNOWN if (buy.error or "").startswith("RPC unavailable") else FAIL
        out.append(_f(lvl, "BUY_QUOTE_FAILED", buy.error or "buy quote failed", buy.source))
        return rt
    sell = await adapter.quote_sell(token, buy.amount_out)
    rt["sell"] = {"ok": sell.ok, "out": str(sell.amount_out), "source": sell.source, "error": sell.error, "exact": sell.exact}
    if not sell.ok:
        if (sell.error or "").startswith("RPC unavailable"):
            out.append(_f(UNKNOWN, "SELL_QUOTE_UNAVAILABLE", sell.error or "", sell.source))
        else:
            out.append(_f(FAIL, "NOT_SELLABLE", f"a sell of the bought amount does not quote: {sell.error}", sell.source))
        rt["sellable"] = False if not (sell.error or "").startswith("RPC unavailable") else None
        return rt
    loss_bps = int((1 - Decimal(sell.amount_out) / Decimal(probe)) * 10_000)
    rt.update(sellable=True, round_trip_loss_bps=loss_bps, returned=str(sell.amount_out))
    if loss_bps > cs.max_round_trip_loss_bps:
        out.append(_f(FAIL, "ROUND_TRIP_LOSS_TOO_HIGH",
                      f"buy then sell returns {sell.amount_out / probe:.1%} of the probe (loss {loss_bps} bps > "
                      f"{cs.max_round_trip_loss_bps} bps)", sell.source, loss_bps=loss_bps))
    if not (buy.exact and sell.exact):
        out.append(_f("INFO", "QUOTE_COMPUTED_LOCALLY", "a quote was computed from reserves with the contract's "
                      "published formula rather than returned by the contract", f"{buy.source} / {sell.source}"))
    return rt


# Four.meme plain-buy simulation (X Mode) -> finding level. X Mode is the
# documented revert "A"; another revert is a WARN until server evidence shows
# the simulation matches real buys; a node without state overrides cannot
# simulate (INFO, stated); no RPC is UNKNOWN as everywhere else.
PLAIN_BUY_FINDINGS = {
    "X_MODE": (FAIL, "FOURMEME_X_MODE", "X Mode token: only the signed X Mode buy works and YonixAlpha does not "
               "implement it, so it cannot be entered"),
    "PLAIN_BUY_REVERTS": (WARN, "PLAIN_BUY_REVERTS", "a simulated plain buy of the probe reverts"),
    "NOT_SIMULATED": ("INFO", "PLAIN_BUY_NOT_SIMULATED", "the node refused the buy simulation (no eth_call state "
                      "override?); X Mode is not ruled out"),
    "UNAVAILABLE": (UNKNOWN, "PLAIN_BUY_UNAVAILABLE", "no RPC endpoint answered the buy simulation"),
    "PLAIN_BUY_OK": ("INFO", "PLAIN_BUY_SIMULATED", "a plain buy of the probe goes through (not X Mode)"),
}


async def _plain_buy(adapter, token: str, probe: int, out: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not hasattr(adapter, "plain_buy"):
        return None
    res = await adapter.plain_buy(token, probe)
    if res["status"] in PLAIN_BUY_FINDINGS:
        level, code, message = PLAIN_BUY_FINDINGS[res["status"]]
        out.append(_f(level, code, message + (f": {res['reason']}" if res.get("reason") else ""), res["source"],
                      plain_buy=res["status"]))
    return res


async def honeypot_is(token: str, client: httpx.AsyncClient | None = None) -> dict[str, Any]:
    """Honeypot.is (BSC). Returns {"available": bool, "is_honeypot": bool|None, ...}."""
    own = client is None
    client = client or httpx.AsyncClient(timeout=8.0)
    try:
        r = await client.get(HONEYPOT_URL, params={"address": token, "chainID": 56})
        if r.status_code != 200:
            return {"available": False, "detail": f"HTTP {r.status_code}"}
        body = r.json()
        hp = (body.get("honeypotResult") or {}).get("isHoneypot")
        sim = body.get("simulationResult") or {}
        return {"available": hp is not None, "is_honeypot": hp, "buy_tax": sim.get("buyTax"), "sell_tax": sim.get("sellTax")}
    except (httpx.HTTPError, ValueError) as exc:
        return {"available": False, "detail": f"{type(exc).__name__}"}
    finally:
        if own:
            await client.aclose()


async def check(adapter, token: str, probe: int, settings: EvmTradingSettings, state: TokenState | None = None,
                http: httpx.AsyncClient | None = None) -> tuple[SafetyReport, dict[str, Any]]:
    """Full check. Returns the report and the round-trip detail (for the
    token page and the entry decision)."""
    chain = adapter.spec.chain.value
    cs = settings.chain(chain)
    findings: list[dict[str, Any]] = []
    sources = [adapter.spec.key]
    if not adapter.spec.supports_trading:
        findings.append(_f(FAIL, "OBSERVE_ONLY_VENUE", "trading is not supported on this venue", adapter.spec.key))
    if state is None:
        try:
            state = await adapter.get_token_state(token)
        except EvmRpcUnavailableError as exc:
            findings.append(_f(UNKNOWN, "STATE_UNAVAILABLE", str(exc)[:160], adapter.spec.key))
        except Exception as exc:  # noqa: BLE001 - a revert / unknown token is a real answer
            findings.append(_f(FAIL, "STATE_UNREADABLE", f"{type(exc).__name__}: {str(exc)[:140]}", adapter.spec.key))
    head = None
    try:
        head = await adapter.rpc.block_number()
    except EvmRpcUnavailableError:
        pass
    if state is not None:
        _state_checks(adapter, state, cs, settings, findings, head)
    await _contract_checks(adapter, token, findings)
    rt = await _round_trip(adapter, token, probe, cs, findings) if adapter.spec.supports_trading else {}
    if adapter.spec.supports_trading and state is not None and state.stage == "CURVE":
        pb = await _plain_buy(adapter, token, probe, findings)
        if pb is not None:
            rt["plain_buy"] = pb
    if chain == "bsc" and settings.honeypot_is_enabled:
        hp = await honeypot_is(token, http)
        sources.append("honeypot.is")
        if not hp.get("available"):
            findings.append(_f("INFO", "HONEYPOT_IS_UNAVAILABLE", "Honeypot.is did not answer (enrichment only)",
                               "honeypot.is", detail=hp.get("detail")))
        elif hp.get("is_honeypot"):
            findings.append(_f(FAIL, "HONEYPOT_IS_FLAGGED", "Honeypot.is flags this token as a honeypot", "honeypot.is", **hp))
        else:
            findings.append(_f("INFO", "HONEYPOT_IS_CLEAN", "Honeypot.is did not flag it (enrichment, not proof)",
                               "honeypot.is", **hp))
    report = SafetyReport(token=token, verdict=verdict_of(findings), findings=findings, sellable=rt.get("sellable"),
                          buy_tax_bps=state.buy_tax_bps if state else None,
                          sell_tax_bps=state.sell_tax_bps if state else None,
                          sources=sources, at=datetime.now(timezone.utc))
    return report, rt
