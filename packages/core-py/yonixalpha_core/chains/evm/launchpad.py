"""Common base for EVM launchpad adapters.

One eth_getLogs pass (`scan`) returns the launches, trades and migrations a
launchpad emitted in a block range; each adapter only says which contracts
emit which events and how a decoded event maps onto the normalized
Launch / TradeEvent records. Logs are accepted only from the launchpad's
own contracts (or, for per-token contracts such as Pons V2 curves and V3
pools, from contracts the launchpad itself announced), so a look-alike
event from an unrelated contract can never create a launch or a trade.

A log that matches a known topic but fails to decode is counted in
`decode_errors`, never silently dropped: a changed event layout shows up
as an EVENTS check failure in the verification tool.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from yonixalpha_core.chains.base import Launch, LaunchpadSpec, Quote, TokenState, TradeEvent
from yonixalpha_core.chains.evm.abi import EventSet
from yonixalpha_core.chains.evm.rpc import EvmRpc


@dataclass
class ScanResult:
    from_block: int
    to_block: int
    launches: list[Launch] = field(default_factory=list)
    trades: list[TradeEvent] = field(default_factory=list)
    migrations: list[dict[str, Any]] = field(default_factory=list)
    other: list[dict[str, Any]] = field(default_factory=list)  # tax / quote / extension settings etc.
    logs_seen: int = 0
    decode_errors: list[str] = field(default_factory=list)
    rejected_foreign: int = 0  # matching topic from a contract the launchpad does not own

    def summary(self) -> dict[str, Any]:
        return {"from_block": self.from_block, "to_block": self.to_block, "logs": self.logs_seen,
                "launches": len(self.launches), "trades": len(self.trades), "migrations": len(self.migrations),
                "other": len(self.other), "decode_errors": len(self.decode_errors),
                "rejected_foreign": self.rejected_foreign}


def hexint(v: Any) -> int | None:
    if v is None:
        return None
    return int(v, 16) if isinstance(v, str) else int(v)


class EvmLaunchpad:
    """Subclasses set `spec`, `events` and implement `_emitters()` and
    `_handle(name, args, log, at, result)`."""

    spec: LaunchpadSpec
    events: EventSet
    max_span: int = 2000
    max_addresses: int = 200  # per eth_getLogs request

    def __init__(self, rpc: EvmRpc) -> None:
        self.rpc = rpc
        self._block_ts: dict[int, datetime] = {}

    # --- log plumbing ----------------------------------------------------------------------------

    async def _emitters(self) -> list[str] | None:
        """Addresses whose logs are accepted (None: topic-only query, the
        subclass validates the emitter in _handle)."""
        return [a for k, a in self.spec.contracts.items() if k in self.emitter_keys]

    emitter_keys: tuple[str, ...] = ()

    async def block_time(self, block: int) -> datetime:
        if block not in self._block_ts:
            b = await self.rpc.get_block(block)
            self._block_ts[block] = datetime.fromtimestamp(int(b["timestamp"], 16), tz=timezone.utc)
            if len(self._block_ts) > 5000:
                for k in sorted(self._block_ts)[:1000]:
                    self._block_ts.pop(k, None)
        return self._block_ts[block]

    async def _fetch(self, from_block: int, to_block: int) -> list[dict[str, Any]]:
        emitters = await self._emitters()
        if emitters == []:
            return []  # nothing of this launchpad's to watch yet
        if emitters is None or len(emitters) <= self.max_addresses:
            return await self.rpc.get_logs(emitters, [self.events.topics], from_block, to_block,
                                           max_span=self.max_span)
        out: list[dict[str, Any]] = []
        for i in range(0, len(emitters), self.max_addresses):
            out += await self.rpc.get_logs(emitters[i:i + self.max_addresses], [self.events.topics], from_block,
                                           to_block, max_span=self.max_span)
        return out

    async def scan(self, from_block: int, to_block: int) -> ScanResult:
        res = ScanResult(from_block, to_block)
        logs = await self._fetch(from_block, to_block)
        res.logs_seen = len(logs)
        for log in sorted(logs, key=lambda x: (hexint(x.get("blockNumber")) or 0, hexint(x.get("logIndex")) or 0)):
            if log.get("removed"):
                continue
            try:
                decoded = self.events.decode(log)
            except Exception as exc:  # noqa: BLE001 - recorded, never dropped silently
                res.decode_errors.append(f"{log.get('transactionHash')}:{hexint(log.get('logIndex'))}: {exc}"[:200])
                continue
            if decoded is None:
                continue
            name, args = decoded
            block = hexint(log.get("blockNumber"))
            at = await self.block_time(block) if block is not None else datetime.now(timezone.utc)
            await self._handle(name, args, log, at, res)
        return res

    async def _handle(self, name: str, args: dict[str, Any], log: dict[str, Any], at: datetime,
                      res: ScanResult) -> None:
        raise NotImplementedError

    def _launch(self, log: dict[str, Any], at: datetime, token: str, creator: str | None, **kw: Any) -> Launch:
        return Launch(chain=self.spec.chain, launchpad=self.spec.key, token=token, creator=creator, created_at=at,
                      tx_hash=log.get("transactionHash"), block=hexint(log.get("blockNumber")), **kw)

    def _trade(self, log: dict[str, Any], at: datetime, **kw: Any) -> TradeEvent:
        return TradeEvent(chain=self.spec.chain, launchpad=self.spec.key, at=at, tx_hash=log.get("transactionHash"),
                          log_index=hexint(log.get("logIndex")), block=hexint(log.get("blockNumber")), **kw)

    # --- LaunchpadAdapter ------------------------------------------------------------------------

    async def discover_launches(self, from_block: int, to_block: int) -> list[Launch]:
        return (await self.scan(from_block, to_block)).launches

    async def trades(self, from_block: int, to_block: int) -> list[TradeEvent]:
        return (await self.scan(from_block, to_block)).trades

    async def get_token_state(self, token: str) -> TokenState:
        raise NotImplementedError

    async def quote_buy(self, token: str, quote_in: int) -> Quote:
        raise NotImplementedError

    async def quote_sell(self, token: str, tokens_in: int) -> Quote:
        raise NotImplementedError

    async def detect_migration(self, token: str) -> dict[str, Any] | None:
        return None

    def venue_status(self) -> dict[str, Any]:
        return {"launchpad": self.spec.key, "chain": self.spec.chain.value, "active": self.spec.active,
                "supports_trading": self.spec.supports_trading, "rpc": self.rpc.health()}
