"""Byte-exact synthetic pump.fun data for tests: Borsh-encoded events laid
out per the official IDL, a bonding curve that moves its reserves the way
the program does, and a fake RPC serving the jsonParsed/base64 shapes the
assembler reads. Test support only — nothing in the runtime imports it."""

import base64
import struct
from datetime import datetime, timedelta
from decimal import Decimal

from yonixalpha_core.safety.models import AccountState
from yonixalpha_core.solana import pump_stream
from yonixalpha_core.solana.codec import DEFAULT_PUBKEY, b58decode, b58encode
from yonixalpha_core.solana.pumpfun import BONDING_CURVE_ACCOUNT, CREATE_EVENT, TRADE_EVENT

MINT = b58encode(bytes([7]) * 32)
CURVE = b58encode(bytes([8]) * 32)
CREATOR = b58encode(bytes([9]) * 32)
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
VIRTUAL_SOL0 = 30_000_000_000
VIRTUAL_TOKEN0 = 1_073_000_000_000_000
REAL_TOKEN_OFFSET = 279_900_000_000_000
SUPPLY = 1_000_000_000_000_000


def pk(address: str) -> bytes:
    return b58decode(address).rjust(32, b"\x00")


def u64(v: int) -> bytes:
    return struct.pack("<Q", v)


def i64(v: int) -> bytes:
    return struct.pack("<q", v)


def s(text: str) -> bytes:
    raw = text.encode()
    return struct.pack("<I", len(raw)) + raw


def b(v: bool) -> bytes:
    return b"\x01" if v else b"\x00"


def wallet(i: int) -> str:
    return b58encode(bytes([100 + i]) * 32)


def create_event(created_at: datetime, mint: str = MINT, curve: str = CURVE, symbol: str = "PIPE") -> bytes:
    body = s("Pipeline Coin") + s(symbol) + s("https://x/m.json") + pk(mint) + pk(curve) + pk(CREATOR) + pk(CREATOR)
    return CREATE_EVENT + body + i64(int(created_at.timestamp()))


class Curve:
    def __init__(self, mint: str = MINT):
        self.mint = mint
        self.vsol, self.vtok = VIRTUAL_SOL0, VIRTUAL_TOKEN0

    def trade(self, user: str, ts: datetime, sol: int, is_buy: bool) -> bytes:
        if is_buy:
            tokens = self.vtok * sol // (self.vsol + sol)
            self.vsol += sol
            self.vtok -= tokens
        else:
            tokens = self.vtok * sol // (self.vsol - sol)
            self.vsol -= sol
            self.vtok += tokens
        body = pk(self.mint) + u64(sol) + u64(tokens) + b(is_buy) + pk(user) + i64(int(ts.timestamp()))
        body += u64(self.vsol) + u64(self.vtok) + u64(self.vsol - VIRTUAL_SOL0) + u64(self.vtok - REAL_TOKEN_OFFSET)
        body += pk(CREATOR) + u64(95) + u64(0) + pk(CREATOR) + u64(30) + u64(0)
        body += b(False) + u64(0) + u64(0) + u64(0) + i64(0) + s("buy" if is_buy else "sell") + b(False)
        body += u64(0) + u64(0) + u64(5) + u64(0) + struct.pack("<I", 0) + pk(DEFAULT_PUBKEY)
        return TRADE_EVENT + body

    def account(self, complete: bool = False) -> bytes:
        return (BONDING_CURVE_ACCOUNT + u64(self.vtok) + u64(self.vsol) + u64(self.vtok - REAL_TOKEN_OFFSET)
                + u64(self.vsol - VIRTUAL_SOL0) + u64(SUPPLY) + b(complete) + pk(CREATOR))

    def price(self, decimals: int = 6) -> Decimal:
        return (Decimal(self.vsol) / Decimal(10**9)) / (Decimal(self.vtok) / Decimal(10) ** decimals)


def logs_of(*events: bytes) -> list[str]:
    return ["Program 6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P invoke [1]"] + [
        "Program data: " + base64.b64encode(e).decode() for e in events
    ]


def program_accounts(creator_tokens: int, current_curve: str | None = CURVE, migrated: int = 0) -> list[dict]:
    """getProgramAccounts answer for a creator with `creator_tokens` curves
    (the current one included when `current_curve` is set), the first
    `migrated` of the previous ones complete. Data is the 1-byte slice."""
    rows = []
    previous = creator_tokens - (1 if current_curve else 0)
    for i in range(max(previous, 0)):
        flag = b"\x01" if i < migrated else b"\x00"
        rows.append({"pubkey": b58encode(bytes([60 + i % 150]) * 31 + bytes([i // 150])),
                     "account": {"data": [base64.b64encode(flag).decode(), "base64"]}})
    if current_curve:
        rows.append({"pubkey": current_curve, "account": {"data": [base64.b64encode(b"\x00").decode(), "base64"]}})
    return rows


class FakeRpc:
    def __init__(self, curve: Curve, mint_authority: str | None = None, fail: set[str] | frozenset = frozenset(),
                 funders: dict[str, str] | None = None, busy: set[str] | frozenset = frozenset(),
                 creator_tokens: int = 12):
        """`funders` maps a buyer wallet to the wallet that funded it (the
        buyer then looks fresh); funders have a short history unless listed
        in `busy` (an exchange-like wallet); every other wallet has a long
        history."""
        self.curve, self.mint_authority, self.fail, self.calls = curve, mint_authority, set(fail), []
        self.funders, self.busy = funders or {}, set(busy)
        # Pump.fun tokens created by the creator wallet (getProgramAccounts).
        self.creator_tokens = creator_tokens

    async def call(self, method, params=None):
        self.calls.append(method)
        if method in self.fail:
            raise RuntimeError(f"{method} unavailable")
        if method == "getAccountInfo" and params[1]["encoding"] == "jsonParsed":
            info = {"mintAuthority": self.mint_authority, "freezeAuthority": None, "decimals": 6, "supply": str(SUPPLY)}
            return {"value": {"owner": TOKEN_2022, "data": {"parsed": {"type": "mint", "info": info}}}}
        if method == "getAccountInfo":
            return {"value": {"data": [base64.b64encode(self.curve.account()).decode(), "base64"]}}
        if method == "getProgramAccounts":
            return program_accounts(self.creator_tokens)
        if method == "getTokenLargestAccounts":
            accts = [{"address": "curveATA", "amount": str(SUPPLY * 70 // 100)}]
            accts += [{"address": f"ta{i}", "amount": str(SUPPLY * 2 // 100)} for i in range(12)]
            return {"value": accts}
        if method == "getMultipleAccounts":
            owners = [CURVE] + [wallet(i) for i in range(12)]
            return {"value": [{"data": {"parsed": {"info": {"owner": o}}}} for o in owners]}
        if method == "getSignaturesForAddress":
            addr = params[0]
            if addr in self.funders:
                return [{"signature": f"fund-{addr}", "err": None}]
            if addr in self.funders.values() and addr not in self.busy:
                return [{"signature": f"f-{addr}-{i}", "err": None} for i in range(5)]
            return [{"signature": f"old-{addr}-{i}", "err": None} for i in range(params[1].get("limit", 25))]
        if method == "getTransaction" and str(params[0]).startswith("fund-"):
            buyer = params[0][len("fund-"):]
            ix = {"parsed": {"type": "transfer", "info": {"source": self.funders[buyer], "destination": buyer, "lamports": 10**8}}}
            return {"transaction": {"message": {"instructions": [ix]}}, "meta": {"innerInstructions": []}}
        raise AssertionError(f"unexpected RPC {method}")


async def seed_healthy_launch(redis, now: datetime) -> Curve:
    """A launch created 20 min before `now`: 25 wallets buying over 10
    minutes, accelerating into the last 5, stream heartbeat 2s old."""
    curve = Curve()
    await pump_stream.ingest_logs(redis, logs_of(create_event(now - timedelta(minutes=20))), "sigcreate", now - timedelta(minutes=20))
    events = []
    for i in range(12):
        events.append(curve.trade(wallet(i), now - timedelta(seconds=590 - i * 25), 800_000_000, True))
    for i in range(28):
        buy = i % 7 != 3
        events.append(curve.trade(wallet(i % 25), now - timedelta(seconds=290 - i * 10), 700_000_000 if buy else 300_000_000, buy))
    await pump_stream.ingest_logs(redis, logs_of(*events), "sigtrades", now - timedelta(seconds=2))
    return curve


def empty_account(equity: Decimal = Decimal(10)) -> AccountState:
    return AccountState(equity, equity, 0, Decimal(0), Decimal(0), None, Decimal(0), False)
