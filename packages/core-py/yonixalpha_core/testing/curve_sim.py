"""A constant-product pump.fun curve from the standard opening reserves that
produces stream Trades (decimals-free, fee-less: fees are applied by the
code under test). Test support only: nothing in the runtime imports it."""

from datetime import datetime, timedelta, timezone

from yonixalpha_core.solana.flow import Trade

T0 = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
VSOL0, VTOK0 = 30_000_000_000, 1_073_000_000_000_000
LAMPORTS = 1_000_000_000


class Curve:
    def __init__(self, t0: datetime = T0) -> None:
        self.t0 = t0
        self.vs, self.vt = VSOL0, VTOK0
        self.k = VSOL0 * VTOK0
        self.trades: list[Trade] = []
        self.held: dict[str, int] = {}

    def buy(self, sec: float, who: str, sol: float) -> None:
        lam = int(sol * LAMPORTS)
        nvs = self.vs + lam
        nvt = self.k // nvs
        tok = self.vt - nvt
        self.vs, self.vt = nvs, nvt
        self.held[who] = self.held.get(who, 0) + tok
        self.trades.append(Trade(self.t0 + timedelta(seconds=sec), who, True, lam, tok, self.vs, self.vt))

    def sell(self, sec: float, who: str, share: float = 1.0) -> None:
        tok = int(self.held.get(who, 0) * share)
        if tok <= 0:
            return
        nvt = self.vt + tok
        nvs = self.k // nvt
        lam = self.vs - nvs
        self.vs, self.vt = nvs, nvt
        self.held[who] -= tok
        self.trades.append(Trade(self.t0 + timedelta(seconds=sec), who, False, lam, tok, self.vs, self.vt))
