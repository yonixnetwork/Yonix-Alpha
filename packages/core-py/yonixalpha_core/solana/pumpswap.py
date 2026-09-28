"""PumpSwap (pump.fun's AMM, program pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA)
for migrated Pump.fun tokens.

From the official pump-public-docs (PUMP_SWAP_README.md, idl/pump_amm.json,
idl/pump.json `migrate` accounts):
- a Pump.fun coin that graduates gets the canonical pool, PDA
  ["pool", u16 0, pool_authority, mint, WSOL] under PumpSwap, where
  pool_authority = PDA ["pool-authority", mint] under the Pump program;
- quotes use effective quote reserves = quote vault balance +
  Pool.virtual_quote_reserves; base reserves are the base vault balance;
- fees depend on market-cap tiers (fee program) and on the coin (buyback,
  cashback, holder rewards), so the fee actually charged is read from the
  pool's own latest BuyEvent/SellEvent, derived from the amounts the trader
  paid/received. Without an event the pool is not treated as priced.

Verified against real mainnet data from pump-public-docs (PUMP_SWAP_README's
example pool): pool_authority, canonical_pool and both vault ATAs derive to
the documented addresses (tests/test_pumpswap.py).

Trade events also carry the trader's wallet, which gives migrated tokens the
same wallet-level flow analysis as bonding-curve tokens — without it the
gate cannot run its manipulation checks (NO_WALLET_DATA).
"""

import base64
import json
import struct
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from solders.pubkey import Pubkey

from yonixalpha_core.safety.liquidity import ConstantProductModel
from yonixalpha_core.solana.codec import BorshReader, TruncatedData
from yonixalpha_core.solana.flow import Trade
from yonixalpha_core.solana.rpc import RpcUnsupportedTransactionVersionError, get_transaction_params

PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMP_AMM_PROGRAM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
WSOL_MINT = "So11111111111111111111111111111111111111112"
POOL_DISC = bytes([241, 154, 109, 4, 17, 177, 109, 188])
BUY_EVENT_DISC = bytes([103, 244, 82, 31, 44, 245, 119, 119])
SELL_EVENT_DISC = bytes([62, 47, 55, 10, 165, 3, 220, 42])
LAMPORTS = Decimal(1_000_000_000)
TRADE_CACHE_TTL = 3600


class PoolUnavailable(Exception):
    pass


def pool_authority(mint: str) -> str:
    return str(Pubkey.find_program_address([b"pool-authority", bytes(Pubkey.from_string(mint))],
                                           Pubkey.from_string(PUMP_PROGRAM))[0])


def canonical_pool(mint: str) -> str:
    seeds = [b"pool", (0).to_bytes(2, "little"), bytes(Pubkey.from_string(pool_authority(mint))),
             bytes(Pubkey.from_string(mint)), bytes(Pubkey.from_string(WSOL_MINT))]
    return str(Pubkey.find_program_address(seeds, Pubkey.from_string(PUMP_AMM_PROGRAM))[0])


@dataclass(frozen=True)
class PoolAccount:
    index: int
    creator: str
    base_mint: str
    quote_mint: str
    lp_mint: str
    base_vault: str
    quote_vault: str
    lp_supply: int
    coin_creator: str | None
    virtual_quote_reserves: int
    is_mayhem_mode: bool = False
    is_cashback_coin: bool = False


def decode_pool(data: bytes) -> PoolAccount:
    if data[:8] != POOL_DISC:
        raise TruncatedData("not a PumpSwap Pool account")
    r = BorshReader(data, 8)
    r.u8()  # bump
    index = struct.unpack("<H", r._take(2))[0]
    creator, base_mint, quote_mint, lp_mint = r.pubkey(), r.pubkey(), r.pubkey(), r.pubkey()
    base_vault, quote_vault = r.pubkey(), r.pubkey()
    lp_supply = r.u64()
    coin_creator = virtual = None
    mayhem = cashback = False
    if r.remaining >= 32:
        coin_creator = r.pubkey()
    if r.remaining >= 2 + 16:
        mayhem = r.bool()
        cashback = r.bool()
        virtual = int.from_bytes(r._take(16), "little", signed=True)
    return PoolAccount(index, creator, base_mint, quote_mint, lp_mint, base_vault, quote_vault, lp_supply,
                       coin_creator, virtual or 0, mayhem, cashback)


@dataclass(frozen=True)
class PoolTrade:
    at: datetime
    user: str
    is_buy: bool
    base_raw: int
    quote_lamports: int  # SOL paid (buy, incl. fees) or received (sell, after fees)
    pool_base: int  # pool reserves as reported by the event
    pool_quote: int
    fee_bps: int  # total fee actually paid (see decode_trade_event)
    pool: str


def decode_trade_event(data: bytes) -> PoolTrade | None:
    disc = data[:8]
    if disc not in (BUY_EVENT_DISC, SELL_EVENT_DISC):
        return None
    r = BorshReader(data, 8)
    ts = r.i64()
    base_amount = r.u64()
    r.u64()  # max_quote_amount_in / min_quote_amount_out
    r.u64(), r.u64()  # user reserves
    pool_base, pool_quote = r.u64(), r.u64()
    swap_quote = r.u64()  # buy: quote_amount_in (before fees); sell: quote_amount_out (before fees)
    lp_bps = r.u64()
    r.u64()  # lp_fee
    protocol_bps = r.u64()
    r.u64()  # protocol_fee
    r.u64()  # quote_amount_in_with_lp_fee / quote_amount_out_without_lp_fee
    user_quote = r.u64()  # user_quote_amount_in / user_quote_amount_out
    pool, user = r.pubkey(), r.pubkey()
    for _ in range(4):
        r.pubkey()
    r.pubkey()  # coin_creator
    creator_bps = r.u64()
    is_buy = disc == BUY_EVENT_DISC
    # The fee this trader actually paid, from the event's own amounts: buy
    # pays user_quote_amount_in for swap_quote reaching the pool; a sell's
    # swap_quote leaves the pool and user_quote_amount_out arrives. This
    # covers every component (lp, protocol, coin creator, buyback, cashback)
    # without double counting re-routed ones (holder rewards report the
    # creator fee again). The declared lp+protocol+creator bps is a floor.
    declared = int(lp_bps + protocol_bps + creator_bps)
    effective = 0
    if is_buy and user_quote > 0 and user_quote >= swap_quote:
        effective = -(-(user_quote - swap_quote) * 10_000 // user_quote)
    elif not is_buy and swap_quote > 0 and swap_quote >= user_quote:
        effective = -(-(swap_quote - user_quote) * 10_000 // swap_quote)
    # Reserves exactly as the event reports them (the values the program
    # priced this swap against); used for price-response analysis only.
    return PoolTrade(datetime.fromtimestamp(ts, tz=timezone.utc), user, is_buy, base_amount, user_quote,
                     pool_base, pool_quote, max(declared, effective), pool)


def trades_from_logs(logs: list[str], pool: str) -> list[PoolTrade]:
    out = []
    for line in logs or []:
        if not line.startswith("Program data: "):
            continue
        try:
            ev = decode_trade_event(base64.b64decode(line[len("Program data: "):]))
        except (TruncatedData, ValueError, struct.error):
            continue
        if ev is not None and ev.pool == pool:
            out.append(ev)
    return out


def as_flow_trade(t: PoolTrade) -> Trade:
    return Trade(at=t.at, trader=t.user, is_buy=t.is_buy, sol_lamports=t.quote_lamports, token_raw=t.base_raw,
                 virtual_sol=t.pool_quote, virtual_token=t.pool_base)


@dataclass(frozen=True)
class PoolState:
    pool: str
    account: PoolAccount
    base_reserve_raw: int
    quote_reserve_lamports: int  # effective (vault + virtual)
    fee_bps: int | None  # from the latest trade event, None if no trade seen
    decimals: int
    observed_at: datetime
    canonical: bool

    @property
    def price(self) -> Decimal:
        return (Decimal(self.quote_reserve_lamports) / LAMPORTS) / (Decimal(self.base_reserve_raw) / Decimal(10) ** self.decimals)

    @property
    def liquidity_sol(self) -> Decimal:
        return Decimal(self.quote_reserve_lamports) / LAMPORTS

    def model(self) -> ConstantProductModel:
        if self.fee_bps is None:
            raise PoolUnavailable("pool fee unknown (no trade event observed)")
        return ConstantProductModel(
            quote_reserve=Decimal(self.quote_reserve_lamports) / LAMPORTS,
            token_reserve=Decimal(self.base_reserve_raw) / Decimal(10) ** self.decimals,
            fee_bps=Decimal(self.fee_bps),
            real_quote_reserve=Decimal(self.quote_reserve_lamports) / LAMPORTS,
        )


async def fetch_pool(rpc, mint: str, now: datetime, fee_bps: int | None, decimals: int) -> PoolState:
    """Verifies the canonical pool exists, belongs to this mint/WSOL and has
    reserves. Raises PoolUnavailable with the reason otherwise."""
    address = canonical_pool(mint)
    info = await rpc.call("getAccountInfo", [address, {"encoding": "base64", "commitment": "confirmed"}])
    value = (info or {}).get("value")
    if not value:
        raise PoolUnavailable(f"canonical PumpSwap pool {address} does not exist (not migrated, or migrated elsewhere)")
    if value.get("owner") != PUMP_AMM_PROGRAM:
        raise PoolUnavailable("pool account is not owned by PumpSwap")
    acct = decode_pool(base64.b64decode(value["data"][0]))
    if acct.base_mint != mint or acct.quote_mint != WSOL_MINT:
        raise PoolUnavailable("pool mints do not match token/WSOL")
    vaults = await rpc.call("getMultipleAccounts", [[acct.base_vault, acct.quote_vault],
                                                    {"encoding": "jsonParsed", "commitment": "confirmed"}])
    vals = (vaults or {}).get("value") or []
    try:
        base = int(vals[0]["data"]["parsed"]["info"]["tokenAmount"]["amount"])
        quote = int(vals[1]["data"]["parsed"]["info"]["tokenAmount"]["amount"])
    except (IndexError, KeyError, TypeError) as exc:
        raise PoolUnavailable("pool vault balances unreadable") from exc
    if base <= 0 or quote <= 0:
        raise PoolUnavailable("pool has no reserves")
    return PoolState(address, acct, base, quote + acct.virtual_quote_reserves, fee_bps, decimals, now, True)


async def recent_pool_trades(rpc, redis, pool: str, limit: int = 25) -> list[PoolTrade]:
    """Recent PumpSwap trades on `pool` with trader wallets, from
    getSignaturesForAddress + getTransaction. Parsed transactions are cached
    per signature, so re-evaluations only fetch new ones."""
    sigs = await rpc.call("getSignaturesForAddress", [pool, {"limit": limit, "commitment": "confirmed"}]) or []
    out: list[PoolTrade] = []
    for s in sigs:
        if s.get("err") is not None:
            continue
        sig = s["signature"]
        key = f"yx:pumpswap:tx:{sig}"
        cached = await redis.get(key) if redis is not None else None
        if cached is not None:
            rows = json.loads(cached)
        else:
            # Only log messages are read (version-independent), so any version
            # the node can return is accepted.
            try:
                tx = await rpc.call("getTransaction", get_transaction_params(sig, encoding="json"))
            except RpcUnsupportedTransactionVersionError:
                continue  # a version newer than we declare: skipped, never misread
            logs = ((tx or {}).get("meta") or {}).get("logMessages") or []
            rows = [[t.at.isoformat(), t.user, t.is_buy, t.base_raw, t.quote_lamports, t.pool_base, t.pool_quote,
                     t.fee_bps, t.pool] for t in trades_from_logs(logs, pool)]
            if redis is not None:
                await redis.set(key, json.dumps(rows), ex=TRADE_CACHE_TTL)
        for r in rows:
            out.append(PoolTrade(datetime.fromisoformat(r[0]), r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8]))
    return sorted(out, key=lambda t: t.at)
