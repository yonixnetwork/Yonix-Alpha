"""Live Solana execution: build → guard → sign → persist signature →
simulate → send → confirm → read the actual fill from the confirmed
transaction.

A trade is only reported CONFIRMED when the transaction is confirmed on
chain without error AND its balance changes show the wallet actually
received tokens (buy) or SOL (sell). The fill (tokens, SOL, network fee)
comes from the transaction's pre/post balances, never from what was
requested. The signature is handed to `on_signed` (which persists it)
before the transaction is sent, so a crash between send and confirmation
can be reconciled later by looking the signature up.
"""

import asyncio
import base64
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Awaitable, Callable

from solders.transaction import VersionedTransaction

from yonixalpha_core.logging import get_logger
from yonixalpha_core.solana.pumpportal import PumpPortalClient, PumpPortalError, TradeRequest
from yonixalpha_core.solana.txguard import GuardExpectation, inspect
from yonixalpha_core.solana.wallet import LiveWallet

log = get_logger("core.live_exec")

LAMPORTS = Decimal(1_000_000_000)
CONFIRM_TIMEOUT_SECONDS = 75  # a blockhash stays valid ~60-90 s
REBROADCAST_SECONDS = 3
POLL_SECONDS = 1.0


@dataclass
class Fill:
    sol_change_lamports: int  # wallet SOL change incl. network fee (negative on buys)
    token_change_raw: int  # wallet token change for the mint (positive on buys)
    fee_lamports: int
    token_decimals: int | None
    slot: int | None
    block_time: int | None


@dataclass
class ExecOutcome:
    status: str  # CONFIRMED | FAILED | EXPIRED | PENDING
    signature: str | None = None
    fill: Fill | None = None
    error: str | None = None
    guard: dict[str, Any] | None = None
    logs: list[str] = field(default_factory=list)
    sent: bool = False

    def to_dict(self) -> dict[str, Any]:
        f = self.fill
        return {
            "status": self.status, "signature": self.signature, "error": self.error, "sent": self.sent,
            "guard": self.guard, "logs": self.logs[-30:],
            "fill": None if f is None else {
                "sol_change_lamports": f.sol_change_lamports, "token_change_raw": f.token_change_raw,
                "fee_lamports": f.fee_lamports, "token_decimals": f.token_decimals, "slot": f.slot, "block_time": f.block_time,
            },
        }


def _key_list(tx: dict) -> list[str]:
    keys = tx.get("transaction", {}).get("message", {}).get("accountKeys", []) or []
    out = [k["pubkey"] if isinstance(k, dict) else k for k in keys]
    loaded = (tx.get("meta") or {}).get("loadedAddresses") or {}
    return out + list(loaded.get("writable", []) or []) + list(loaded.get("readonly", []) or [])


def parse_fill(tx: dict, wallet: str, mint: str) -> Fill:
    """Actual balance changes of `wallet` in a confirmed transaction
    (getTransaction, encoding jsonParsed)."""
    meta = tx.get("meta") or {}
    keys = _key_list(tx)
    if wallet not in keys:
        raise ValueError("wallet not in transaction accounts")
    i = keys.index(wallet)
    sol = int(meta["postBalances"][i]) - int(meta["preBalances"][i])

    def amount(entries) -> tuple[int, int | None]:
        total, dec = 0, None
        for b in entries or []:
            if b.get("mint") == mint and b.get("owner") == wallet:
                ta = b.get("uiTokenAmount") or {}
                total += int(ta.get("amount", "0"))
                dec = ta.get("decimals", dec)
        return total, dec

    pre, d1 = amount(meta.get("preTokenBalances"))
    post, d2 = amount(meta.get("postTokenBalances"))
    return Fill(sol_change_lamports=sol, token_change_raw=post - pre, fee_lamports=int(meta.get("fee", 0)),
                token_decimals=d2 if d2 is not None else d1, slot=tx.get("slot"), block_time=tx.get("blockTime"))


class SolanaLiveExecutor:
    def __init__(self, rpc, pumpportal: PumpPortalClient, wallet: LiveWallet,
                 confirm_timeout: float = CONFIRM_TIMEOUT_SECONDS, sleep=asyncio.sleep, clock=time.monotonic):
        self.rpc, self.pp, self.wallet = rpc, pumpportal, wallet
        self.confirm_timeout, self._sleep, self._clock = confirm_timeout, sleep, clock

    async def execute(self, req: TradeRequest, exp: GuardExpectation,
                      on_signed: Callable[[str], Awaitable[None]]) -> ExecOutcome:
        if req.public_key != self.wallet.pubkey or exp.wallet != self.wallet.pubkey:
            return ExecOutcome("FAILED", error="request is not for the configured wallet")
        try:
            raw = await self.pp.build_transaction(req)
            unsigned = VersionedTransaction.from_bytes(raw)
        except PumpPortalError as exc:
            return ExecOutcome("FAILED", error=str(exc))
        except Exception as exc:  # noqa: BLE001 - malformed bytes
            return ExecOutcome("FAILED", error=f"could not decode transaction: {type(exc).__name__}")

        report = inspect(unsigned, exp)
        if not report.ok:
            return ExecOutcome("FAILED", error="transaction guard refused to sign: " + "; ".join(report.violations),
                               guard=report.to_dict())
        signed = VersionedTransaction(unsigned.message, [self.wallet.keypair])
        signature = str(signed.signatures[0])
        wire = base64.b64encode(bytes(signed)).decode()
        await on_signed(signature)

        try:
            sim = await self.rpc.call("simulateTransaction", [wire, {"encoding": "base64", "sigVerify": True,
                                                                     "commitment": "confirmed"}])
        except Exception as exc:  # noqa: BLE001
            return ExecOutcome("FAILED", signature, error=f"simulation unavailable: {type(exc).__name__}",
                               guard=report.to_dict())
        sim_value = (sim or {}).get("value") or {}
        if sim_value.get("err") is not None:
            return ExecOutcome("FAILED", signature, error=f"simulation failed: {sim_value.get('err')}",
                               guard=report.to_dict(), logs=list(sim_value.get("logs") or []))

        return await self._send_and_confirm(wire, signature, req.mint, report.to_dict())

    async def _send_and_confirm(self, wire: str, signature: str, mint: str, guard: dict) -> ExecOutcome:
        start = self._clock()
        last_send = None
        sent = False
        while self._clock() - start < self.confirm_timeout:
            if last_send is None or self._clock() - last_send >= REBROADCAST_SECONDS:
                try:
                    await self.rpc.call("sendTransaction", [wire, {"encoding": "base64", "skipPreflight": True,
                                                                   "maxRetries": 0}])
                    sent = True
                except Exception as exc:  # noqa: BLE001 - keep polling; the tx may still land
                    log.warning("live.send_failed", signature=signature, error=type(exc).__name__)
                last_send = self._clock()
            outcome = await self.lookup(signature, mint)
            if outcome.status != "PENDING":
                outcome.guard, outcome.sent = guard, sent
                return outcome
            await self._sleep(POLL_SECONDS)
        # Not seen before the blockhash could have expired: final check with history.
        outcome = await self.lookup(signature, mint)
        outcome.guard, outcome.sent = guard, sent
        if outcome.status == "PENDING":
            outcome.status = "EXPIRED"
            outcome.error = f"not confirmed within {self.confirm_timeout:.0f}s; blockhash expired, it can no longer land"
        return outcome

    async def lookup(self, signature: str, mint: str) -> ExecOutcome:
        """Current state of a signature (also used by reconciliation)."""
        try:
            st = await self.rpc.call("getSignatureStatuses", [[signature], {"searchTransactionHistory": True}])
        except Exception as exc:  # noqa: BLE001
            return ExecOutcome("PENDING", signature, error=f"status unavailable: {type(exc).__name__}")
        status = ((st or {}).get("value") or [None])[0]
        if not status:
            return ExecOutcome("PENDING", signature)
        if status.get("err") is not None:
            return ExecOutcome("FAILED", signature, error=f"transaction failed on chain: {status.get('err')}")
        if status.get("confirmationStatus") not in ("confirmed", "finalized"):
            return ExecOutcome("PENDING", signature)
        try:
            tx = await self.rpc.call("getTransaction", [signature, {"encoding": "jsonParsed", "commitment": "confirmed",
                                                                    "maxSupportedTransactionVersion": 0}])
        except Exception as exc:  # noqa: BLE001
            return ExecOutcome("PENDING", signature, error=f"transaction fetch failed: {type(exc).__name__}")
        if not tx:
            return ExecOutcome("PENDING", signature)
        if (tx.get("meta") or {}).get("err") is not None:
            return ExecOutcome("FAILED", signature, error=f"transaction failed on chain: {tx['meta']['err']}",
                               logs=list((tx.get("meta") or {}).get("logMessages") or []))
        try:
            fill = parse_fill(tx, self.wallet.pubkey, mint)
        except (KeyError, ValueError, IndexError) as exc:
            return ExecOutcome("FAILED", signature, error=f"confirmed but fill unreadable: {exc}")
        return ExecOutcome("CONFIRMED", signature, fill=fill, logs=list((tx.get("meta") or {}).get("logMessages") or []))


async def wallet_balances(rpc, owner: str) -> tuple[int, dict[str, dict]]:
    """(SOL lamports, {mint: {amount, decimals, program}}) for reconciliation."""
    lamports = int(((await rpc.call("getBalance", [owner, {"commitment": "confirmed"}])) or {}).get("value", 0))
    tokens: dict[str, dict] = {}
    for program in ("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"):
        res = await rpc.call("getTokenAccountsByOwner", [owner, {"programId": program},
                                                         {"encoding": "jsonParsed", "commitment": "confirmed"}])
        for acc in (res or {}).get("value", []) or []:
            info = ((acc.get("account") or {}).get("data") or {}).get("parsed", {}).get("info", {})
            mint = info.get("mint")
            ta = info.get("tokenAmount") or {}
            if not mint:
                continue
            cur = tokens.setdefault(mint, {"amount": 0, "decimals": ta.get("decimals"), "program": program})
            cur["amount"] += int(ta.get("amount", "0"))
    return lamports, tokens
