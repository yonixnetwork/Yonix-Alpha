"""A fake Solana node for execution tests: serves mint / bonding-curve /
PumpSwap-pool / token / Global / GlobalConfig / lookup-table accounts in
their real binary layouts, and answers blockhash, simulate, send and
signature lookups like a node. Global and GlobalConfig are the accounts
Anchor encodes from the official IDLs (fixtures/pump_sdk). Nothing leaves
the process."""

import base64
import json
import struct
from pathlib import Path

from yonixalpha_core.solana import pump_tx as p
from yonixalpha_core.solana.pumpfun import BONDING_CURVE_ACCOUNT
from yonixalpha_core.solana.pumpswap import POOL_DISC, canonical_pool

FIX = json.loads((Path(__file__).parent / "fixtures" / "pump_sdk" / "fixtures.json").read_text())
ALT_PROGRAM = "AddressLookupTab1e1111111111111111111111111"


def pk(a: str) -> bytes:
    return bytes(p._pk(a))


def acct(data: bytes, owner: str) -> dict:
    return {"data": [base64.b64encode(data).decode(), "base64"], "owner": owner, "lamports": 1_000_000, "executable": False}


def mint_account(decimals: int = 6) -> bytes:
    return struct.pack("<I", 0) + bytes(32) + struct.pack("<Q", 10**15) + bytes([decimals, 1]) + struct.pack("<I", 0) + bytes(32)


def curve_account(creator: str, *, vtok=1_000_000_000_000_000, vsol=30_000_000_000, rtok=793_000_000_000_000,
                  rsol=1_000_000_000, complete=False, mayhem=False, cashback=False) -> bytes:
    return (BONDING_CURVE_ACCOUNT + struct.pack("<QQQQQ", vtok, vsol, rtok, rsol, 10**15) + bytes([complete]) + pk(creator)
            + bytes([mayhem, cashback]) + bytes(32))


def pool_account(mint: str, base_vault: str, quote_vault: str, coin_creator: str, size: int = 300) -> bytes:
    body = (POOL_DISC + bytes([255]) + struct.pack("<H", 0) + pk(p.DEFAULT_PUBKEY) + pk(mint) + pk(p.WSOL) + pk(p.DEFAULT_PUBKEY)
            + pk(base_vault) + pk(quote_vault) + struct.pack("<Q", 1000) + pk(coin_creator) + bytes([0, 0]) + bytes(16))
    return body + bytes(max(0, size - len(body)))


def token_account(mint: str, owner: str, amount: int) -> bytes:
    return pk(mint) + pk(owner) + struct.pack("<Q", amount) + bytes(165 - 72)


def lookup_table(addresses: list[str]) -> bytes:
    return bytes(56) + b"".join(pk(a) for a in addresses)


class FakeChain:
    def __init__(self):
        self.accounts: dict[str, dict] = {
            p.global_pda(): acct(bytes.fromhex(FIX["global_account"]["hex"]), p.PUMP),
            p.amm_global_config_pda(): acct(bytes.fromhex(FIX["global_config_account"]["hex"]), p.PUMP_AMM),
        }
        self.calls: list[str] = []
        self.sent: list[str] = []
        self.sim_err = None
        self.status = {"confirmationStatus": "confirmed", "err": None}
        self.pending_polls = 0
        self.fill = None  # (wallet, mint, sol_change, token_change)
        self.on_call = None  # hook(method, params) -> None, e.g. to migrate a token mid-execution
        self.fail: set[str] = set()

    # --- scenario builders ---
    def add_mint(self, mint: str, token_program: str = p.TOKEN, decimals: int = 6) -> None:
        self.accounts[mint] = acct(mint_account(decimals), token_program)

    def add_curve(self, mint: str, creator: str, **kw) -> None:
        self.accounts[p.bonding_curve_pda(mint)] = acct(curve_account(creator, **kw), p.PUMP)

    def add_pool(self, mint: str, coin_creator: str, base=10**15, quote=80_000_000_000, size: int = 300) -> str:
        bv, qv = str(p._pk(p.pda(p.SYSTEM, b"bv", pk(mint)))), str(p._pk(p.pda(p.SYSTEM, b"qv", pk(mint))))
        pool = canonical_pool(mint)
        self.accounts[pool] = acct(pool_account(mint, bv, qv, coin_creator, size), p.PUMP_AMM)
        self.accounts[bv] = acct(token_account(mint, pool, base), p.TOKEN)
        self.accounts[qv] = acct(token_account(p.WSOL, pool, quote), p.TOKEN)
        return pool

    def add_table(self, address: str, addresses: list[str]) -> None:
        self.accounts[address] = acct(lookup_table(addresses), ALT_PROGRAM)

    # --- node ---
    async def call(self, method, params=None):
        self.calls.append(method)
        if self.on_call:
            self.on_call(method, params)
        if method in self.fail:
            raise RuntimeError(f"{method} failed (HTTP 429)")
        if method == "getMultipleAccounts":
            return {"context": {"slot": 1234}, "value": [self.accounts.get(k) for k in params[0]]}
        if method == "getAccountInfo":
            return {"context": {"slot": 1234}, "value": self.accounts.get(params[0])}
        if method == "getLatestBlockhash":
            return {"value": {"blockhash": "EETubP5AKHgjPAhzPAFcb8BAY1hMH639CWCFTqi3hq1k", "lastValidBlockHeight": 999}}
        if method == "simulateTransaction":
            return {"value": {"err": self.sim_err, "logs": ["sim"], "unitsConsumed": 90_000}}
        if method == "sendTransaction":
            self.sent.append(params[0])
            return "sig"
        if method == "getSignatureStatuses":
            if self.pending_polls > 0:
                self.pending_polls -= 1
                return {"value": [None]}
            return {"value": [self.status]}
        if method == "getTransaction":
            wallet, mint, sol, tokens = self.fill
            return {"slot": 77, "blockTime": 1700000000,
                    "transaction": {"message": {"accountKeys": [{"pubkey": wallet}, {"pubkey": mint}]}},
                    "meta": {"err": None, "fee": 5_000, "preBalances": [1_000_000_000, 0], "postBalances": [1_000_000_000 + sol, 0],
                             "preTokenBalances": [],
                             "postTokenBalances": [{"mint": mint, "owner": wallet, "uiTokenAmount": {"amount": str(tokens), "decimals": 6}}],
                             "logMessages": []}}
        raise AssertionError(f"unexpected RPC method {method}")
