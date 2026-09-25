"""PumpPortal (third-party, unofficial Pump.fun API) — local-transaction
trading. (Token discovery does not use PumpPortal: it decodes Pump.fun's own
program logs from our RPC WebSocket.)

Execution uses the **Local Transaction API** (`POST /api/trade-local`): it
returns an unsigned, serialized `VersionedTransaction` that is inspected
(`txguard`), signed with our own wallet and sent through our own RPC. The
Lightning API (custodial wallet held by PumpPortal) is deliberately not used:
the key would not be ours and fills could not be verified against our
wallet. PumpPortal charges a trading fee (0.5% per its published docs) that
appears in the transaction as a SOL transfer; `txguard` bounds it.

Verified against PumpPortal's published README mirror
(github.com/thetateman/Pump-Fun-API) and its current docs page: body fields
publicKey, action, mint, amount, denominatedInSol, slippage (percent),
priorityFee (SOL), pool ('pump' | 'pump-amm' | 'auto' | ...); a 200
response body is the raw transaction bytes. pumpportal.fun itself was not
reachable from the build environment, so behaviour against the live service
is IMPLEMENTED — AWAITING CREDENTIAL VERIFICATION.
"""

from dataclasses import dataclass
from decimal import Decimal

import httpx

TRADE_LOCAL_URL = "https://pumpportal.fun/api/trade-local"
POOLS = ("pump", "pump-amm", "auto")
MAX_TX_BYTES = 1232  # Solana packet limit


class PumpPortalError(Exception):
    pass


@dataclass(frozen=True)
class TradeRequest:
    public_key: str
    action: str  # buy | sell
    mint: str
    amount: str  # SOL (buy, denominated_in_sol) or raw-UI token amount / "100%" (sell)
    denominated_in_sol: bool
    slippage_pct: Decimal
    priority_fee_sol: Decimal
    pool: str

    def body(self) -> dict:
        if self.action not in ("buy", "sell"):
            raise PumpPortalError("action must be buy or sell")
        if self.pool not in POOLS:
            raise PumpPortalError(f"pool must be one of {POOLS}")
        return {
            "publicKey": self.public_key,
            "action": self.action,
            "mint": self.mint,
            "amount": self.amount,
            "denominatedInSol": "true" if self.denominated_in_sol else "false",
            "slippage": float(self.slippage_pct),
            "priorityFee": float(self.priority_fee_sol),
            "pool": self.pool,
        }


class PumpPortalClient:
    def __init__(self, client: httpx.AsyncClient, url: str = TRADE_LOCAL_URL, timeout: float = 15.0):
        self.client, self.url, self.timeout = client, url, timeout

    async def build_transaction(self, req: TradeRequest) -> bytes:
        try:
            resp = await self.client.post(self.url, json=req.body(), timeout=self.timeout)
        except httpx.HTTPError as exc:
            raise PumpPortalError(f"trade-local transport error: {type(exc).__name__}") from exc
        if resp.status_code != 200:
            raise PumpPortalError(f"trade-local HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.content
        if not data or len(data) > MAX_TX_BYTES or data[:1] in (b"{", b"["):
            raise PumpPortalError(f"trade-local returned no transaction ({len(data)} bytes)")
        return data

