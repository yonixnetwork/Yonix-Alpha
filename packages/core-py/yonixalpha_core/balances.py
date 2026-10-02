"""The YonixAlpha Trading Wallet, balance by balance (master §56-58).

One trading wallet with two underlying accounts that are never derived from
each other:
  Solana account  its own ed25519 key (WALLET_PRIVATE_KEY) - SOL
  EVM account     one secp256k1 key / address for BSC (BNB) and Robinhood
                  Chain (ETH) (EVM_WALLET_ADDRESS / EVM_WALLET_PRIVATE_KEY)
Only public addresses leave the server.

Every chain shows, for LIVE and for PAPER (never mixed):
  Total            native coin held (paper: cash + open positions at their mark)
  Reserved         committed and not spendable (live: pending buy orders;
                   paper: open positions at their mark)
  Available        Total - Reserved
  Gas reserve      native coin kept back for fees, never traded
  Trading balance  Available - Gas reserve: what a new entry may use
A value that cannot be read is None with the reason, never 0.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

CURRENCY = {"solana": "SOL", "bsc": "BNB", "robinhood": "ETH"}
ACCOUNT = {"solana": "Solana account", "bsc": "EVM account", "robinhood": "EVM account"}
STALE_SECONDS = 180


@dataclass
class BalanceRow:
    chain: str
    mode: str  # LIVE | PAPER
    status: str  # LIVE / STALE / NOT_CONFIGURED / UNAVAILABLE / PAPER
    total: Decimal | None = None
    reserved: Decimal | None = None
    gas_reserve: Decimal | None = None
    address: str | None = None
    at: str | None = None
    usd_rate: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def available(self) -> Decimal | None:
        if self.total is None or self.reserved is None:
            return None
        return max(Decimal(0), self.total - self.reserved)

    @property
    def trading_balance(self) -> Decimal | None:
        if self.available is None or self.gas_reserve is None:
            return None
        return max(Decimal(0), self.available - self.gas_reserve)

    def to_dict(self) -> dict[str, Any]:
        def usd(v: Decimal | None) -> str | None:
            return None if v is None or self.usd_rate is None else str((v * Decimal(self.usd_rate)).quantize(Decimal("0.01")))

        vals = {"total": self.total, "available": self.available, "reserved": self.reserved,
                "gas_reserve": self.gas_reserve, "trading_balance": self.trading_balance}
        return {"chain": self.chain, "mode": self.mode, "account": ACCOUNT[self.chain], "currency": CURRENCY[self.chain],
                "status": self.status, "address": self.address, "at": self.at,
                **{k: (str(v) if v is not None else None) for k, v in vals.items()},
                "usd": {k: usd(v) for k, v in vals.items()}, "usd_rate": self.usd_rate, "notes": self.notes}


def freshness(at: str | None, now: datetime) -> str:
    if not at:
        return "UNAVAILABLE"
    age = (now - datetime.fromisoformat(at)).total_seconds()
    return "LIVE" if age <= STALE_SECONDS else "STALE"


def solana_live(wallet: dict[str, Any] | None, min_sol_reserve: Decimal, pending_buys: Decimal, now: datetime,
                usd_rate: str | None) -> BalanceRow:
    if not wallet:
        return BalanceRow("solana", "LIVE", "UNAVAILABLE", gas_reserve=min_sol_reserve, usd_rate=usd_rate,
                          notes=["no wallet sync yet (the order worker reads the wallet from the chain)"])
    return BalanceRow("solana", "LIVE", freshness(wallet.get("at"), now), total=Decimal(wallet["sol"]),
                      reserved=pending_buys, gas_reserve=min_sol_reserve, address=wallet.get("pubkey"),
                      at=wallet.get("at"), usd_rate=usd_rate,
                      notes=["reserved = pending LIVE buy orders", "gas reserve = live min_sol_reserve (fees and exits)"])


def evm_live(chain: str, address: str | None, synced: dict[str, Any] | None, gas_reserve: Decimal, now: datetime,
             usd_rate: str | None) -> BalanceRow:
    if not address:
        return BalanceRow(chain, "LIVE", "NOT_CONFIGURED", gas_reserve=gas_reserve, usd_rate=usd_rate,
                          notes=["set EVM_WALLET_ADDRESS (server .env): one address serves BSC and Robinhood Chain"])
    if not synced:
        return BalanceRow(chain, "LIVE", "UNAVAILABLE", gas_reserve=gas_reserve, address=address, usd_rate=usd_rate,
                          notes=["no balance synced yet (data-evm reads it every minute)"])
    return BalanceRow(chain, "LIVE", freshness(synced.get("at"), now), total=Decimal(synced["balance"]),
                      reserved=Decimal(0), gas_reserve=gas_reserve, address=address, at=synced.get("at"),
                      usd_rate=usd_rate, notes=["watch-only: EVM LIVE execution is locked, so nothing is reserved by orders"])


def paper(chain: str, cash: Decimal, open_value: Decimal, gas_reserve: Decimal | None, usd_rate: str | None,
          note: str | None = None) -> BalanceRow:
    row = BalanceRow(chain, "PAPER", "PAPER", total=cash + open_value, reserved=open_value, gas_reserve=gas_reserve,
                     usd_rate=usd_rate, notes=["reserved = open paper positions at their latest mark"])
    if note:
        row.notes.append(note)
    return row
