"""Transaction builders, one per execution provider. Each returns an
UNSIGNED transaction plus what the guard needs to verify it; none of them
signs, sends, or can widen the bounds of the order.

  native_pump  Pump bonding curve / PumpSwap instructions built here from
               on-chain state (pump_tx.py, byte-identical to the official
               SDKs). No platform fee. Default for Pump venues.
  pumpportal   PumpPortal trade-local (third party). Its transactions have
               carried programs the guard does not allow (an unverified
               "Arbitrage Bot" program, FAdo9NCw…, with no Pump trade
               instruction) — those are refused before signing.
  jupiter      Jupiter Swap API /swap for non-Pump routes; lookup tables are
               resolved so every account the guard checks is known.
"""

import base64
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.hash import Hash
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import VersionedTransaction

from yonixalpha_core.solana import pump_tx as p
from yonixalpha_core.solana.pumpportal import PumpPortalClient, TradeRequest
from yonixalpha_core.solana.txguard import GuardExpectation
from yonixalpha_core.solana.venue import JUPITER_ROUTE, PUMP_AMM_VENUE, PUMP_BONDING_CURVE, Venue

LAMPORTS = Decimal(1_000_000_000)
FEE_MARGIN_BPS = 100  # added to the configured fee rates when sizing a buy: fewer tokens requested, never more SOL
CU_LIMIT = {PUMP_BONDING_CURVE: 200_000, PUMP_AMM_VENUE: 350_000}
CONFIG_TTL_SECONDS = 60
ALT_ADDRESSES_OFFSET = 56  # address lookup table: 56-byte meta, then 32-byte addresses


class BuildError(Exception):
    pass


@dataclass
class BuiltTx:
    tx: VersionedTransaction
    provider: str
    loaded: list[str] | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    last_valid_block_height: int | None = None


def unsigned(msg) -> VersionedTransaction:
    return VersionedTransaction.populate(msg, [Signature.default()])


def buy_lamports(req: TradeRequest) -> int:
    if not req.denominated_in_sol:
        raise BuildError("buys are sized in SOL")
    return int(Decimal(req.amount) * LAMPORTS)


def sell_raw(req: TradeRequest, decimals: int | None) -> int:
    if decimals is None:
        raise BuildError("token decimals unknown")
    return int(Decimal(req.amount) * Decimal(10) ** decimals)


def priority_ixs(req: TradeRequest, units: int) -> list:
    lamports = int(req.priority_fee_sol * LAMPORTS)
    micro = lamports * 1_000_000 // units if lamports else 0
    return [set_compute_unit_limit(units), set_compute_unit_price(micro)]


class NativePumpBuilder:
    name = "native_pump"

    def __init__(self, rpc, clock=time.monotonic):
        self.rpc, self._clock = rpc, clock
        self._global: tuple[float, p.PumpGlobal] | None = None
        self.cu_limits: dict[str, int] = dict(CU_LIMIT)  # dashboard setting (live_trading), applied per order
        self._config: tuple[float, p.AmmGlobalConfig] | None = None

    async def _account(self, address: str) -> bytes:
        res = await self.rpc.call("getAccountInfo", [address, {"encoding": "base64", "commitment": "confirmed"}])
        value = (res or {}).get("value")
        if not value:
            raise BuildError(f"account {address} not found")
        return base64.b64decode(value["data"][0])

    async def pump_global(self) -> p.PumpGlobal:
        if self._global is None or self._clock() - self._global[0] > CONFIG_TTL_SECONDS:
            self._global = (self._clock(), p.decode_pump_global(await self._account(p.global_pda())))
        return self._global[1]

    async def amm_config(self) -> p.AmmGlobalConfig:
        if self._config is None or self._clock() - self._config[0] > CONFIG_TTL_SECONDS:
            self._config = (self._clock(), p.decode_amm_global_config(await self._account(p.amm_global_config_pda())))
        return self._config[1]

    async def build(self, req: TradeRequest, venue: Venue, exp: GuardExpectation, wallet: str) -> BuiltTx:
        slip = Decimal(req.slippage_pct)
        detail: dict[str, Any] = {"venue": venue.kind}
        if venue.kind == PUMP_BONDING_CURVE:
            c, tp = venue.curve, venue.token_program
            if c is None or tp is None or c.creator is None:
                raise BuildError("bonding curve state incomplete")
            g = await self.pump_global()
            fee_recipient = g.fee_recipient_for(c.is_mayhem_mode)
            buyback = p.random.choice(p.CURVE_BUYBACK_FEE_RECIPIENTS)
            if req.action == "buy":
                size = buy_lamports(req)
                fee_bps = g.fee_basis_points + g.creator_fee_basis_points + FEE_MARGIN_BPS
                tokens = p.curve_buy_tokens(size, c.virtual_token_reserves, c.virtual_quote_reserves, c.real_token_reserves, fee_bps)
                max_cost = min(p.with_slippage_up(size, slip), exp.max_sol_in_lamports or 0)
                if tokens <= 0 or max_cost <= 0:
                    raise BuildError("bonding curve cannot fill this buy")
                trade = [p.ata_create_idempotent(wallet, wallet, venue.mint, tp),
                         p.curve_buy_ix(user=wallet, mint=venue.mint, creator=c.creator, token_program=tp, amount=tokens,
                                        max_sol_cost=max_cost, fee_recipient=fee_recipient, buyback_fee_recipient=buyback)]
                detail.update(tokens_out=tokens, max_sol_cost=max_cost, fee_bps_assumed=fee_bps)
                exp.min_tokens_out = tokens
            else:
                raw = sell_raw(req, venue.decimals)
                fee_bps = g.fee_basis_points + g.creator_fee_basis_points + FEE_MARGIN_BPS
                expected = p.curve_sell_sol(raw, c.virtual_token_reserves, c.virtual_quote_reserves, fee_bps)
                min_out = exp.min_sol_out_lamports or p.with_slippage_down(expected, slip)
                trade = [p.curve_sell_ix(user=wallet, mint=venue.mint, creator=c.creator, token_program=tp, amount=raw,
                                         min_sol_output=min_out, fee_recipient=fee_recipient, buyback_fee_recipient=buyback,
                                         cashback=c.is_cashback_coin)]
                detail.update(tokens_in=raw, min_sol_out=min_out, expected_sol_out=expected)
        elif venue.kind == PUMP_AMM_VENUE:
            pool, tp = venue.pool, venue.token_program
            if pool is None or tp is None or not venue.pool_base_reserve or not venue.pool_quote_reserve:
                raise BuildError("PumpSwap pool state incomplete")
            cfg = await self.amm_config()
            protocol = cfg.protocol_fee_recipient_for(pool.is_mayhem_mode)
            buyback = cfg.buyback_fee_recipient()
            fee_bps = cfg.lp_fee_basis_points + cfg.protocol_fee_basis_points + cfg.coin_creator_fee_basis_points + FEE_MARGIN_BPS
            if req.action == "buy":
                size = buy_lamports(req)
                base_out = p.amm_buy_base_out(size, venue.pool_base_reserve, venue.pool_quote_reserve, fee_bps)
                max_in = min(p.with_slippage_up(size, slip), exp.max_sol_in_lamports or 0)
                if base_out <= 0 or max_in <= 0:
                    raise BuildError("pool cannot fill this buy")
                trade = p.amm_buy_ixs(user=wallet, pool=pool, base_token_program=tp, base_out=base_out, max_quote_in=max_in,
                                      protocol_fee_recipient=protocol, buyback_fee_recipient=buyback)
                detail.update(tokens_out=base_out, max_sol_cost=max_in, fee_bps_assumed=fee_bps)
                exp.min_tokens_out = base_out
            else:
                raw = sell_raw(req, venue.decimals)
                expected = p.amm_sell_quote_out(raw, venue.pool_base_reserve, venue.pool_quote_reserve, fee_bps)
                min_out = exp.min_sol_out_lamports or p.with_slippage_down(expected, slip)
                trade = p.amm_sell_ixs(user=wallet, pool=pool, base_token_program=tp, base_in=raw, min_quote_out=min_out,
                                       protocol_fee_recipient=protocol, buyback_fee_recipient=buyback)
                detail.update(tokens_in=raw, min_sol_out=min_out, expected_sol_out=expected)
            exp.pool = pool.pool
        else:
            raise BuildError(f"native builder does not trade {venue.kind}")
        blockhash = await self.rpc.call("getLatestBlockhash", [{"commitment": "confirmed"}])
        value = (blockhash or {}).get("value") or {}
        if not value.get("blockhash"):
            raise BuildError("no recent blockhash")
        msg = MessageV0.try_compile(Pubkey.from_string(wallet), priority_ixs(req, self.cu_limits.get(venue.kind, CU_LIMIT[venue.kind])) + trade, [],
                                    Hash.from_string(value["blockhash"]))
        # Native transactions pay no platform fee: nothing may leave the wallet as one.
        exp.max_fee_transfer_lamports = 0
        detail["blockhash_slot"] = ((blockhash or {}).get("context") or {}).get("slot")  # diagnostics only
        detail["compute_unit_limit"] = self.cu_limits.get(venue.kind, CU_LIMIT[venue.kind])
        return BuiltTx(unsigned(msg), self.name, None, detail, value.get("lastValidBlockHeight"))


class PumpPortalBuilder:
    name = "pumpportal"

    def __init__(self, client: PumpPortalClient):
        self.pp = client

    async def build(self, req: TradeRequest, venue: Venue, exp: GuardExpectation, wallet: str) -> BuiltTx:
        if venue.kind not in (PUMP_BONDING_CURVE, PUMP_AMM_VENUE):
            raise BuildError(f"PumpPortal does not trade {venue.kind}")
        # The pool follows the venue found on chain now, not the one the order was created for.
        pool = "pump" if venue.kind == PUMP_BONDING_CURVE else "pump-amm"
        r = TradeRequest(req.public_key, req.action, req.mint, req.amount, req.denominated_in_sol, req.slippage_pct,
                         req.priority_fee_sol, pool)
        raw = await self.pp.build_transaction(r)
        try:
            tx = VersionedTransaction.from_bytes(raw)
        except Exception as exc:  # noqa: BLE001 - malformed bytes
            raise BuildError(f"could not decode transaction: {type(exc).__name__}") from exc
        if venue.kind == PUMP_AMM_VENUE and venue.pool is not None:
            exp.pool = venue.pool.pool
        return BuiltTx(tx, self.name, None, {"pool": pool})


async def resolve_lookup_tables(rpc, tx: VersionedTransaction) -> list[str]:
    """Addresses the message's lookup tables load, in runtime order (every
    table's writable indexes, then every table's read-only indexes)."""
    lookups = list(getattr(tx.message, "address_table_lookups", []) or [])
    if not lookups:
        return []
    res = await rpc.call("getMultipleAccounts", [[str(lk.account_key) for lk in lookups],
                                                 {"encoding": "base64", "commitment": "confirmed"}])
    tables = []
    for acct in (res or {}).get("value") or []:
        if not acct:
            raise BuildError("address lookup table not found")
        data = base64.b64decode(acct["data"][0])[ALT_ADDRESSES_OFFSET:]
        tables.append([str(Pubkey.from_bytes(data[i:i + 32])) for i in range(0, len(data) - len(data) % 32, 32)])
    writable, readonly = [], []
    for lk, table in zip(lookups, tables):
        try:
            writable += [table[i] for i in lk.writable_indexes]
            readonly += [table[i] for i in lk.readonly_indexes]
        except IndexError as exc:
            raise BuildError("lookup table index out of range") from exc
    return writable + readonly


class JupiterBuilder:
    name = "jupiter"

    def __init__(self, jupiter, rpc):
        self.jupiter, self.rpc = jupiter, rpc

    async def build(self, req: TradeRequest, venue: Venue, exp: GuardExpectation, wallet: str) -> BuiltTx:
        if venue.kind != JUPITER_ROUTE or not venue.jupiter_quote:
            raise BuildError("no Jupiter quote for this venue")
        q = venue.jupiter_quote
        lamports = int(req.priority_fee_sol * LAMPORTS)
        b64 = await self.jupiter.swap_transaction(q, wallet, lamports)
        try:
            tx = VersionedTransaction.from_bytes(base64.b64decode(b64))
        except Exception as exc:  # noqa: BLE001
            raise BuildError(f"could not decode Jupiter transaction: {type(exc).__name__}") from exc
        loaded = await resolve_lookup_tables(self.rpc, tx)
        slip_bps = int(Decimal(req.slippage_pct) * 100)
        out = int(q.get("outAmount") or 0)
        exp.max_slippage_bps = slip_bps
        min_out = out * (10_000 - slip_bps) // 10_000
        if exp.side == "buy":
            exp.min_tokens_out = min_out
        elif exp.min_sol_out_lamports is None:
            exp.min_sol_out_lamports = min_out
        exp.max_fee_transfer_lamports = 0  # no platform fee is requested from Jupiter
        return BuiltTx(tx, self.name, loaded, {"quote_out": out, "min_out": min_out, "slippage_bps": slip_bps,
                                               "route": [s.get("swapInfo", {}).get("label") for s in q.get("routePlan", [])]})
