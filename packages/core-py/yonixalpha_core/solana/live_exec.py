"""Live Solana execution: resolve the venue from chain state → build with
the provider for that venue → guard → re-check the venue (a token that
migrated meanwhile is rebuilt for its new venue, never sent as a stale
curve trade) → sign → persist signature → simulate → send → confirm →
read the actual fill from the confirmed transaction.

Every stage is recorded on the outcome (TRANSACTION_BUILT,
TRANSACTION_GUARD_PASSED, TRANSACTION_SIGNED, SIMULATED,
TRANSACTION_SUBMITTED, TRANSACTION_CONFIRMED, FILL_VERIFIED, or the stage
that failed), each with its time and details; none implies the next.

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

from yonixalpha_core.execution_analysis import own_trade_event
from yonixalpha_core.logging import get_logger
from yonixalpha_core.solana import venue as venues
from yonixalpha_core.solana.rpc import get_transaction_params, with_priority
from yonixalpha_core.solana.txversion import UnsupportedTransactionLayout, require_version
from yonixalpha_core.solana.pumpportal import PumpPortalClient, PumpPortalError, TradeRequest
from yonixalpha_core.solana.tx_builders import BuildError, JupiterBuilder, NativePumpBuilder, PumpPortalBuilder
from yonixalpha_core.solana.txguard import GuardExpectation, inspect
from yonixalpha_core.solana.wallet import LiveWallet

log = get_logger("core.live_exec")

LAMPORTS = Decimal(1_000_000_000)
CONFIRM_TIMEOUT_SECONDS = 75  # a blockhash stays valid ~60-90 s
REBROADCAST_SECONDS = 3
POLL_SECONDS = 1.0
MAX_VENUE_REBUILDS = 2  # a venue that keeps changing between build and sign is not traded
BUILDERS = ("native", "pumpportal")


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
    stage: str | None = None  # the stage that decided the outcome
    stages: list[dict[str, Any]] = field(default_factory=list)
    venue: dict[str, Any] | None = None
    provider: str | None = None
    unsigned_tx: str | None = None  # base64, kept only when the guard refused it (for diagnosis; unsigned)
    trade_event: dict[str, Any] | None = None  # our decoded Pump/PumpSwap trade event (execution analysis)
    seen: dict[str, Any] | None = None  # status seen before confirmation (slot, confirmationStatus)
    rpc_calls: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        f = self.fill
        return {
            "status": self.status, "signature": self.signature, "error": self.error, "sent": self.sent,
            "guard": self.guard, "logs": self.logs[-30:], "stage": self.stage, "stages": self.stages,
            "venue": self.venue, "provider": self.provider, "unsigned_tx": self.unsigned_tx,
            "trade_event": self.trade_event, "rpc_calls": self.rpc_calls[-40:],
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
    # Our own transactions are v0 (MessageV0); anything else was not built
    # here, so its layout is not assumed.
    require_version(tx, ("legacy", 0))
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


class _RpcRecorder:
    """Times every RPC call the executor makes (method, endpoint, ms), for
    the execution latency trace. Behaviour is unchanged."""

    def __init__(self, inner: Any):
        self._inner = inner
        self.calls: list[dict[str, Any]] = []

    async def call(self, method: str, params: list | None = None) -> Any:
        started, t0 = time.time(), time.monotonic()
        ok = False
        try:
            result = await self._inner.call(method, params)
            ok = True
            return result
        finally:
            if len(self.calls) >= 200:  # lookups between orders (reconciliation) never grow it unbounded
                del self.calls[:100]
            self.calls.append({"method": method, "at": round(started, 3), "ms": int((time.monotonic() - t0) * 1000),
                               "ok": ok, "endpoint": getattr(self._inner, "active_label", None)})

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class SolanaLiveExecutor:
    def __init__(self, rpc, pumpportal: PumpPortalClient | None, wallet: LiveWallet,
                 confirm_timeout: float = CONFIRM_TIMEOUT_SECONDS, sleep=asyncio.sleep, clock=time.monotonic,
                 jupiter=None, builder: str = "native"):
        # Trade execution is critical traffic: never shed behind background work.
        self.rpc, self.pp, self.wallet = _RpcRecorder(with_priority(rpc, "critical")), pumpportal, wallet
        self.confirm_timeout, self._sleep, self._clock = confirm_timeout, sleep, clock
        self.jupiter = jupiter
        self.builder = builder  # "native" | "pumpportal" (Pump venues only); set per order from live settings
        self.native = NativePumpBuilder(self.rpc)
        self._pp_builder = PumpPortalBuilder(pumpportal) if pumpportal is not None else None
        self._jup_builder = JupiterBuilder(jupiter, self.rpc) if jupiter is not None else None

    def _builder_for(self, venue: venues.Venue):
        if venue.kind == venues.JUPITER_ROUTE:
            return self._jup_builder
        if self.builder == "pumpportal" and self._pp_builder is not None:
            return self._pp_builder
        return self.native

    async def _resolve(self, req: TradeRequest) -> venues.Venue:
        amount_raw = None
        try:
            amount_raw = int(Decimal(req.amount) * LAMPORTS) if req.action == "buy" else None
        except Exception:  # noqa: BLE001
            pass
        v = await venues.resolve(self.rpc, req.mint, side=req.action, amount_raw=amount_raw, jupiter=self.jupiter,
                                 slippage_bps=int(Decimal(req.slippage_pct) * 100))
        if req.action == "sell" and v.kind == venues.NO_EXECUTABLE_ROUTE and self.jupiter is not None and v.decimals is not None:
            # Sells are sized in tokens; quote Jupiter with the raw amount now that decimals are known.
            raw = int(Decimal(req.amount) * Decimal(10) ** v.decimals)
            v = await venues.resolve(self.rpc, req.mint, side="sell", amount_raw=raw, jupiter=self.jupiter,
                                     slippage_bps=int(Decimal(req.slippage_pct) * 100))
        return v

    async def execute(self, req: TradeRequest, exp: GuardExpectation,
                      on_signed: Callable[[str], Awaitable[None]]) -> ExecOutcome:
        stages: list[dict[str, Any]] = []
        calls = self.rpc.calls = []

        def stage(name: str, **detail: Any) -> None:
            stages.append({"stage": name, "at": time.time(), **detail})

        def fail(stage_name: str, error: str, **kw: Any) -> ExecOutcome:
            stage(stage_name, error=error)
            return ExecOutcome("FAILED", error=error, stage=stage_name, stages=stages, rpc_calls=calls, **kw)

        if req.public_key != self.wallet.pubkey or exp.wallet != self.wallet.pubkey:
            return fail("REQUEST_REJECTED", "request is not for the configured wallet")

        venue = None
        built = report = None
        for attempt in range(MAX_VENUE_REBUILDS + 1):
            venue = await self._resolve(req)
            stage("VENUE_RESOLVED", venue=venue.kind, reason=venue.reason, slot=venue.slot)
            if venue.kind == venues.RPC_UNAVAILABLE:
                return fail("RPC_UNAVAILABLE", venue.reason, venue=venue.summary())
            if not venue.executable:
                return fail("NO_EXECUTABLE_ROUTE", f"{venue.kind}: {venue.reason}", venue=venue.summary())
            builder = self._builder_for(venue)
            if builder is None:
                return fail("NO_EXECUTABLE_ROUTE", f"no provider configured for {venue.kind}", venue=venue.summary())
            exp.venue = venue.kind
            try:
                built = await builder.build(req, venue, exp, self.wallet.pubkey)
            except (BuildError, PumpPortalError) as exc:
                return fail("TRANSACTION_BUILD_FAILED", f"{builder.name}: {exc}", venue=venue.summary(), provider=builder.name)
            except Exception as exc:  # noqa: BLE001 - provider/RPC failure while building
                return fail("TRANSACTION_BUILD_FAILED", f"{builder.name}: {type(exc).__name__}: {str(exc)[:200]}",
                            venue=venue.summary(), provider=builder.name)
            stage("TRANSACTION_BUILT", provider=built.provider, **{k: v for k, v in built.detail.items() if k != "route"})
            report = inspect(built.tx, exp, built.loaded)
            if not report.ok:
                stage("TRANSACTION_GUARD_REJECTED", violations=report.violations)
                return ExecOutcome("FAILED", error="transaction guard refused to sign: " + "; ".join(report.violations),
                                   guard=report.to_dict(), stage="TRANSACTION_GUARD_REJECTED", stages=stages, rpc_calls=calls,
                                   venue=venue.summary(), provider=built.provider,
                                   unsigned_tx=base64.b64encode(bytes(built.tx)).decode())
            stage("TRANSACTION_GUARD_PASSED", programs=report.programs)
            if venue.kind == venues.JUPITER_ROUTE:
                break
            # Migration race: the venue must still be the one the transaction was built for.
            again = await venues.resolve(self.rpc, req.mint)
            if again.kind == venue.kind:
                break
            stage("VENUE_CHANGED", before=venue.kind, after=again.kind, reason=again.reason)
            if attempt == MAX_VENUE_REBUILDS:
                return fail("VENUE_UNSTABLE", f"venue changed from {venue.kind} to {again.kind} while building; not sent",
                            venue=again.summary(), provider=built.provider)
        assert built is not None and report is not None and venue is not None

        signed = VersionedTransaction(built.tx.message, [self.wallet.keypair])
        signature = str(signed.signatures[0])
        wire = base64.b64encode(bytes(signed)).decode()
        await on_signed(signature)
        stage("TRANSACTION_SIGNED", signature=signature)
        common = {"guard": report.to_dict(), "stages": stages, "venue": venue.summary(), "provider": built.provider,
                  "rpc_calls": calls}

        try:
            sim = await self.rpc.call("simulateTransaction", [wire, {"encoding": "base64", "sigVerify": True,
                                                                     "commitment": "confirmed"}])
        except Exception as exc:  # noqa: BLE001
            stage("SIMULATION_UNAVAILABLE", error=type(exc).__name__)
            return ExecOutcome("FAILED", signature, error=f"simulation unavailable: {type(exc).__name__}",
                               stage="SIMULATION_UNAVAILABLE", **common)
        sim_value = (sim or {}).get("value") or {}
        if sim_value.get("err") is not None:
            stage("SIMULATION_FAILED", error=str(sim_value.get("err"))[:300])
            return ExecOutcome("FAILED", signature, error=f"simulation failed: {sim_value.get('err')}",
                               logs=list(sim_value.get("logs") or []), stage="SIMULATION_FAILED", **common)
        stage("SIMULATED", units=sim_value.get("unitsConsumed"))

        outcome = await self._send_and_confirm(wire, signature, req.mint, report.to_dict(), stage)
        outcome.stages, outcome.venue, outcome.provider = stages, venue.summary(), built.provider
        outcome.rpc_calls = calls
        return outcome

    async def _send_and_confirm(self, wire: str, signature: str, mint: str, guard: dict, stage=None) -> ExecOutcome:
        stage = stage or (lambda *a, **k: None)
        start = self._clock()
        last_send = None
        sent = False
        seen_recorded = False
        while self._clock() - start < self.confirm_timeout:
            if last_send is None or self._clock() - last_send >= REBROADCAST_SECONDS:
                # Re-sending the SAME signed transaction is idempotent: one
                # signature can land at most once, so this never buys twice.
                try:
                    await self.rpc.call("sendTransaction", [wire, {"encoding": "base64", "skipPreflight": True,
                                                                   "maxRetries": 0}])
                    if not sent:
                        stage("TRANSACTION_SUBMITTED", endpoint=getattr(self.rpc, "active_label", None),
                              after_ms=round((self._clock() - start) * 1000))
                    sent = True
                except Exception as exc:  # noqa: BLE001 - keep polling; the tx may still land
                    log.warning("live.send_failed", signature=signature, error=type(exc).__name__)
                last_send = self._clock()
            outcome = await self.lookup(signature, mint)
            if outcome.seen is not None and not seen_recorded:
                seen_recorded = True
                stage("TRANSACTION_SEEN", **outcome.seen)
            if outcome.status != "PENDING":
                outcome.guard, outcome.sent = guard, sent
                self._record_final(outcome, stage)
                return outcome
            await self._sleep(POLL_SECONDS)
        # Not seen before the blockhash could have expired: final check with history.
        outcome = await self.lookup(signature, mint)
        outcome.guard, outcome.sent = guard, sent
        if outcome.status == "PENDING":
            outcome.status = "EXPIRED"
            outcome.error = f"not confirmed within {self.confirm_timeout:.0f}s; blockhash expired, it can no longer land"
        self._record_final(outcome, stage)
        return outcome

    @staticmethod
    def _record_final(outcome: ExecOutcome, stage) -> None:
        f = outcome.fill
        if outcome.status == "CONFIRMED" and f is not None:
            stage("TRANSACTION_CONFIRMED", signature=outcome.signature, slot=f.slot, block_time=f.block_time)
            stage("FILL_VERIFIED", token_change_raw=f.token_change_raw, sol_change_lamports=f.sol_change_lamports,
                  fee_lamports=f.fee_lamports)
            outcome.stage = "FILL_VERIFIED"
        else:
            outcome.stage = "TRANSACTION_" + outcome.status  # TRANSACTION_FAILED | TRANSACTION_EXPIRED
            stage(outcome.stage, error=outcome.error)

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
        seen = {"slot": status.get("slot"), "status": status.get("confirmationStatus")}
        if status.get("confirmationStatus") not in ("confirmed", "finalized"):
            return ExecOutcome("PENDING", signature, seen=seen)
        try:
            tx = await self.rpc.call("getTransaction", get_transaction_params(signature))
        except Exception as exc:  # noqa: BLE001
            return ExecOutcome("PENDING", signature, error=f"transaction fetch failed: {type(exc).__name__}")
        if not tx:
            return ExecOutcome("PENDING", signature)
        if (tx.get("meta") or {}).get("err") is not None:
            return ExecOutcome("FAILED", signature, error=f"transaction failed on chain: {tx['meta']['err']}",
                               logs=list((tx.get("meta") or {}).get("logMessages") or []))
        try:
            fill = parse_fill(tx, self.wallet.pubkey, mint)
        except (KeyError, ValueError, IndexError, UnsupportedTransactionLayout) as exc:
            return ExecOutcome("FAILED", signature, error=f"confirmed but fill unreadable: {exc}")
        logs = list((tx.get("meta") or {}).get("logMessages") or [])
        try:
            event = own_trade_event(logs, self.wallet.pubkey, mint)
        except Exception:  # noqa: BLE001 - diagnostics only; the fill above is what counts
            event = None
        return ExecOutcome("CONFIRMED", signature, fill=fill, logs=logs, trade_event=event, seen=seen)


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
