"""Native Pump.fun bonding-curve and PumpSwap (pump-amm) trade instructions.

Built to the official protocol: pump-fun/pump-public-docs (IDLs, the April
fee-recipient upgrade, holder-reward / cashback / mayhem notes) and the
official SDKs @pump-fun/pump-sdk 2.0.0 and @pump-fun/pump-swap-sdk 1.20.0.
tests/test_pump_tx_sdk_parity.py checks every instruction built here
byte-for-byte (program, account order, signer/writable flags, data)
against what those SDKs build for the same inputs
(tests/fixtures/pump_sdk/generate.cjs).

Bonding curve (as the SDK's default `buyInstructions` / `sellInstructions`):
  buy(amount, max_sol_cost, track_volume=Some(true))  — 16 IDL accounts +
    [bonding-curve-v2 (ro), buyback fee recipient (w)] = 18
  sell(amount, min_sol_output)                         — 14 + [user volume
    accumulator (w) for cashback coins,] bonding-curve-v2, buyback = 16/17
PumpSwap (the SDK's `buyInstructions` / `sellInstructions`):
  [extend_account if the pool account is shorter than 300 bytes]
  buy: WSOL ATA (idempotent) + transfer max_quote_in + sync_native, base ATA
    (idempotent), buy(base_out, max_quote_in, Some(true)) with remaining
    [cashback: WSOL ATA of the user volume accumulator,] [pool-v2 when the
    pool has a coin creator,] buyback recipient, its WSOL ATA; close WSOL.
  sell: base ATA stays; WSOL ATA (idempotent), sell(base_in, min_quote_out)
    with remaining [cashback: accumulator WSOL ATA + accumulator,]
    [pool-v2,] buyback recipient, its WSOL ATA; close WSOL.

Nothing here reads the network: callers pass decoded on-chain state
(venue.py fetches it). Every amount bound is explicit.
"""

import random
import struct
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

from solders.instruction import AccountMeta, Instruction
from solders.pubkey import Pubkey

from yonixalpha_core.solana.codec import BorshReader, TruncatedData

PUMP = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMP_AMM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
PUMP_FEE = "pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ"
SYSTEM = "11111111111111111111111111111111"
TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
ATA_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
WSOL = "So11111111111111111111111111111111111111112"
DEFAULT_PUBKEY = "11111111111111111111111111111111"

# Anchor discriminators (official IDLs).
BUY = bytes([102, 6, 61, 18, 1, 218, 235, 234])
SELL = bytes([51, 230, 133, 164, 1, 127, 131, 173])
EXTEND_ACCOUNT = bytes([234, 102, 194, 203, 150, 72, 62, 229])
GLOBAL_DISC = bytes([167, 232, 232, 177, 200, 108, 114, 127])
GLOBAL_CONFIG_DISC = bytes([149, 8, 156, 202, 160, 252, 176, 217])
POOL_ACCOUNT_NEW_SIZE = 300  # pump-swap-sdk: shorter pools get extend_account first

# @pump-fun/pump-sdk bondingCurve.ts CURRENT_FEE_RECIPIENTS_FOR_BUYBACK — the
# 8 recipients announced in docs/BREAKING_FEE_RECIPIENT.md.
CURVE_BUYBACK_FEE_RECIPIENTS = (
    "5YxQFdt3Tr9zJLvkFccqXVUwhdTWJQc1fFg2YPbxvxeD", "9M4giFFMxmFGXtc3feFzRai56WbBqehoSeRE5GK7gf7",
    "GXPFM2caqTtQYC2cJ5yJRi9VDkpsYZXzYdwYpGnLmtDL", "3BpXnfJaUTiwXnJNe7Ej1rcbzqTTQUvLShZaWazebsVR",
    "5cjcW9wExnJJiqgLjq7DEG75Pm6JBgE1hNv4B2vHXUW6", "EHAAiTxcdDwQ3U4bU6YcMsQGaekdzLS3B5SmYo46kJtL",
    "5eHhjP8JaYkz83CWwvGU2uMUXefd3AazWGx4gpcuEEYD", "A7hAgCzFw14fejgCp387JUJRMNyz4j89JKnhtKU8piqW",
)


def _pk(s: str) -> Pubkey:
    return Pubkey.from_string(s)


def pda(program: str, *seeds: bytes) -> str:
    return str(Pubkey.find_program_address(list(seeds), _pk(program))[0])


def ata(owner: str, mint: str, token_program: str = TOKEN) -> str:
    return pda(ATA_PROGRAM, bytes(_pk(owner)), bytes(_pk(token_program)), bytes(_pk(mint)))


# --- PDAs (pump-sdk pda.ts / pump-swap-sdk pda.ts and the IDL seeds) --------
def global_pda() -> str:
    return pda(PUMP, b"global")


def bonding_curve_pda(mint: str) -> str:
    return pda(PUMP, b"bonding-curve", bytes(_pk(mint)))


def bonding_curve_v2_pda(mint: str) -> str:
    return pda(PUMP, b"bonding-curve-v2", bytes(_pk(mint)))


def creator_vault_pda(creator: str) -> str:
    return pda(PUMP, b"creator-vault", bytes(_pk(creator)))


def event_authority_pda(program: str) -> str:
    return pda(program, b"__event_authority")


def global_volume_accumulator_pda(program: str = PUMP) -> str:
    return pda(program, b"global_volume_accumulator")


def user_volume_accumulator_pda(user: str, program: str = PUMP) -> str:
    return pda(program, b"user_volume_accumulator", bytes(_pk(user)))


def fee_config_pda(program: str) -> str:
    return pda(PUMP_FEE, b"fee_config", bytes(_pk(program)))


def amm_global_config_pda() -> str:
    return pda(PUMP_AMM, b"global_config")


def pool_v2_pda(base_mint: str) -> str:
    return pda(PUMP_AMM, b"pool-v2", bytes(_pk(base_mint)))


def coin_creator_vault_authority_pda(coin_creator: str) -> str:
    return pda(PUMP_AMM, b"creator_vault", bytes(_pk(coin_creator)))


# --- On-chain configuration accounts ----------------------------------------
@dataclass(frozen=True)
class PumpGlobal:
    fee_recipient: str
    fee_recipients: tuple[str, ...]
    reserved_fee_recipient: str | None
    reserved_fee_recipients: tuple[str, ...]
    buyback_fee_recipients: tuple[str, ...]
    fee_basis_points: int = 0
    creator_fee_basis_points: int = 0

    def fee_recipient_for(self, mayhem: bool, rng: random.Random | None = None) -> str:
        """pump-sdk fees.ts getFeeRecipient."""
        r = rng or random
        if mayhem:
            pool = [x for x in (self.reserved_fee_recipient, *self.reserved_fee_recipients) if x and x != DEFAULT_PUBKEY]
        else:
            pool = [x for x in (self.fee_recipient, *self.fee_recipients) if x and x != DEFAULT_PUBKEY]
        if not pool:
            raise ValueError("Global account lists no fee recipient")
        return r.choice(pool)


def decode_pump_global(data: bytes) -> PumpGlobal:
    """Pump `Global` (IDL field order). Fields past the ones needed are not read."""
    if data[:8] != GLOBAL_DISC:
        raise TruncatedData("not a pump Global account")
    r = BorshReader(data, 8)
    r.bool()  # initialized
    r.pubkey()  # authority
    fee_recipient = r.pubkey()
    for _ in range(4):
        r.u64()  # initial_* reserves, token_total_supply
    fee_bps = r.u64()
    r.pubkey()  # withdraw_authority
    r.bool()  # enable_migrate
    r.u64()  # pool_migration_fee
    creator_fee_bps = r.u64()
    fee_recipients = tuple(r.pubkey() for _ in range(7))
    r.pubkey()  # set_creator_authority
    r.pubkey()  # admin_set_creator_authority
    reserved = reserved_list = None
    buyback: tuple[str, ...] = ()
    if r.remaining >= 1 + 32 + 32 + 1 + 32 * 7:
        r.bool()  # create_v2_enabled
        r.pubkey()  # whitelist_pda
        reserved = r.pubkey()
        r.bool()  # mayhem_mode_enabled
        reserved_list = tuple(r.pubkey() for _ in range(7))
        if r.remaining >= 1 + 32 * 8:
            r.bool()  # is_cashback_enabled
            buyback = tuple(r.pubkey() for _ in range(8))
    return PumpGlobal(fee_recipient, fee_recipients, reserved, reserved_list or (), buyback, fee_bps, creator_fee_bps)


@dataclass(frozen=True)
class AmmGlobalConfig:
    protocol_fee_recipients: tuple[str, ...]
    reserved_fee_recipient: str | None
    reserved_fee_recipients: tuple[str, ...]
    buyback_fee_recipients: tuple[str, ...]
    lp_fee_basis_points: int = 0
    protocol_fee_basis_points: int = 0
    coin_creator_fee_basis_points: int = 0

    def protocol_fee_recipient_for(self, mayhem: bool, rng: random.Random | None = None) -> str:
        """pump-swap-sdk fees.ts getFeeRecipient."""
        r = rng or random
        pool = ([x for x in (self.reserved_fee_recipient, *self.reserved_fee_recipients) if x and x != DEFAULT_PUBKEY]
                if mayhem else [x for x in self.protocol_fee_recipients if x != DEFAULT_PUBKEY])
        if not pool:
            raise ValueError("GlobalConfig lists no protocol fee recipient")
        return r.choice(pool)

    def buyback_fee_recipient(self, rng: random.Random | None = None) -> str:
        pool = [x for x in self.buyback_fee_recipients if x != DEFAULT_PUBKEY]
        if not pool:
            raise ValueError("GlobalConfig lists no buyback fee recipient")
        return (rng or random).choice(pool)


def decode_amm_global_config(data: bytes) -> AmmGlobalConfig:
    if data[:8] != GLOBAL_CONFIG_DISC:
        raise TruncatedData("not a PumpSwap GlobalConfig account")
    r = BorshReader(data, 8)
    r.pubkey()  # admin
    lp_bps = r.u64()
    protocol_bps = r.u64()
    r.u8()  # disable_flags
    protocol = tuple(r.pubkey() for _ in range(8))
    creator_bps = r.u64()
    r.pubkey()  # admin_set_coin_creator_authority
    r.pubkey()  # whitelist_pda
    reserved = r.pubkey()
    r.bool()  # mayhem_mode_enabled
    reserved_list = tuple(r.pubkey() for _ in range(7))
    r.bool()  # is_cashback_enabled
    buyback = tuple(r.pubkey() for _ in range(8))
    return AmmGlobalConfig(protocol, reserved, reserved_list, buyback, lp_bps, protocol_bps, creator_bps)


# --- Quote math (pump-sdk bondingCurve.ts; constant product for PumpSwap) ---
def curve_buy_tokens(sol_in: int, virtual_token: int, virtual_quote: int, real_token: int, total_fee_bps: int) -> int:
    """getBuyTokenAmountFromSolAmount: tokens a buy spending `sol_in`
    lamports (fees included) receives."""
    if sol_in <= 0 or virtual_token <= 0:
        return 0
    net = (sol_in - 1) * 10_000 // (total_fee_bps + 10_000)
    return min(net * virtual_token // (virtual_quote + net), real_token)


def curve_sell_sol(tokens_in: int, virtual_token: int, virtual_quote: int, total_fee_bps: int) -> int:
    """getSellSolAmountFromTokenAmount: lamports a sale of `tokens_in` pays out after fees."""
    if tokens_in <= 0 or virtual_token <= 0:
        return 0
    gross = tokens_in * virtual_quote // (virtual_token + tokens_in)
    return gross - (gross * total_fee_bps + 9_999) // 10_000


def amm_buy_base_out(quote_in: int, base_reserve: int, quote_reserve: int, total_fee_bps: int) -> int:
    """Base tokens for spending `quote_in` (fees taken from the input)."""
    if quote_in <= 0 or base_reserve <= 0 or quote_reserve <= 0:
        return 0
    net = quote_in * 10_000 // (10_000 + total_fee_bps)
    return net * base_reserve // (quote_reserve + net)


def amm_sell_quote_out(base_in: int, base_reserve: int, quote_reserve: int, total_fee_bps: int) -> int:
    if base_in <= 0 or base_reserve <= 0 or quote_reserve <= 0:
        return 0
    gross = base_in * quote_reserve // (base_reserve + base_in)
    return gross - (gross * total_fee_bps + 9_999) // 10_000


def with_slippage_down(amount: int, slippage_pct: Decimal) -> int:
    return int((Decimal(amount) * (1 - slippage_pct / 100)).to_integral_value(ROUND_DOWN))


def with_slippage_up(amount: int, slippage_pct: Decimal) -> int:
    return int((Decimal(amount) * (1 + slippage_pct / 100)).to_integral_value(ROUND_DOWN))


# --- Instructions -------------------------------------------------------------
def _m(key: str, signer: bool = False, writable: bool = False) -> AccountMeta:
    return AccountMeta(_pk(key), signer, writable)


def ata_create_idempotent(payer: str, owner: str, mint: str, token_program: str) -> Instruction:
    return Instruction(_pk(ATA_PROGRAM), bytes([1]), [
        _m(payer, True, True), _m(ata(owner, mint, token_program), writable=True), _m(owner), _m(mint), _m(SYSTEM),
        _m(token_program)])


def system_transfer(src: str, dst: str, lamports: int) -> Instruction:
    return Instruction(_pk(SYSTEM), struct.pack("<IQ", 2, lamports), [_m(src, True, True), _m(dst, writable=True)])


def sync_native(account: str) -> Instruction:
    return Instruction(_pk(TOKEN), bytes([17]), [_m(account, writable=True)])


def close_account(account: str, destination: str, owner: str, token_program: str = TOKEN) -> Instruction:
    return Instruction(_pk(token_program), bytes([9]), [_m(account, writable=True), _m(destination, writable=True),
                                                        _m(owner, signer=True)])


def curve_buy_ix(*, user: str, mint: str, creator: str, token_program: str, amount: int, max_sol_cost: int,
                 fee_recipient: str, buyback_fee_recipient: str) -> Instruction:
    bc = bonding_curve_pda(mint)
    accounts = [
        _m(global_pda()), _m(fee_recipient, writable=True), _m(mint), _m(bc, writable=True),
        _m(ata(bc, mint, token_program), writable=True), _m(ata(user, mint, token_program), writable=True),
        _m(user, True, True), _m(SYSTEM), _m(token_program), _m(creator_vault_pda(creator), writable=True),
        _m(event_authority_pda(PUMP)), _m(PUMP), _m(global_volume_accumulator_pda(PUMP)),
        _m(user_volume_accumulator_pda(user, PUMP), writable=True), _m(fee_config_pda(PUMP)), _m(PUMP_FEE),
        _m(bonding_curve_v2_pda(mint)), _m(buyback_fee_recipient, writable=True),
    ]
    # track_volume: OptionBool(true) is one byte 0x01.
    return Instruction(_pk(PUMP), BUY + struct.pack("<QQ", amount, max_sol_cost) + b"\x01", accounts)


def curve_sell_ix(*, user: str, mint: str, creator: str, token_program: str, amount: int, min_sol_output: int,
                  fee_recipient: str, buyback_fee_recipient: str, cashback: bool) -> Instruction:
    bc = bonding_curve_pda(mint)
    accounts = [
        _m(global_pda()), _m(fee_recipient, writable=True), _m(mint), _m(bc, writable=True),
        _m(ata(bc, mint, token_program), writable=True), _m(ata(user, mint, token_program), writable=True),
        _m(user, True, True), _m(SYSTEM), _m(creator_vault_pda(creator), writable=True), _m(token_program),
        _m(event_authority_pda(PUMP)), _m(PUMP), _m(fee_config_pda(PUMP)), _m(PUMP_FEE),
    ]
    if cashback:
        accounts.append(_m(user_volume_accumulator_pda(user, PUMP), writable=True))
    accounts += [_m(bonding_curve_v2_pda(mint)), _m(buyback_fee_recipient, writable=True)]
    return Instruction(_pk(PUMP), SELL + struct.pack("<QQ", amount, min_sol_output), accounts)


@dataclass(frozen=True)
class AmmPoolInfo:
    pool: str
    base_mint: str
    quote_mint: str
    pool_base_token_account: str
    pool_quote_token_account: str
    coin_creator: str
    is_mayhem_mode: bool
    is_cashback_coin: bool
    account_size: int


def _amm_accounts(user: str, p: AmmPoolInfo, base_tp: str, quote_tp: str, protocol_fee_recipient: str) -> list[AccountMeta]:
    authority = coin_creator_vault_authority_pda(p.coin_creator)
    return [
        _m(p.pool, writable=True), _m(user, True, True), _m(amm_global_config_pda()), _m(p.base_mint), _m(p.quote_mint),
        _m(ata(user, p.base_mint, base_tp), writable=True), _m(ata(user, p.quote_mint, quote_tp), writable=True),
        _m(p.pool_base_token_account, writable=True), _m(p.pool_quote_token_account, writable=True),
        _m(protocol_fee_recipient), _m(ata(protocol_fee_recipient, p.quote_mint, quote_tp), writable=True),
        _m(base_tp), _m(quote_tp), _m(SYSTEM), _m(ATA_PROGRAM), _m(event_authority_pda(PUMP_AMM)), _m(PUMP_AMM),
        _m(ata(authority, p.quote_mint, quote_tp), writable=True), _m(authority),
    ]


def _amm_tail(user: str, p: AmmPoolInfo, quote_tp: str, buyback: str, sell: bool) -> list[AccountMeta]:
    tail = []
    if p.is_cashback_coin:
        tail.append(_m(ata(user_volume_accumulator_pda(user, PUMP_AMM), p.quote_mint, quote_tp), writable=True))
        if sell:
            tail.append(_m(user_volume_accumulator_pda(user, PUMP_AMM), writable=True))
    if p.coin_creator != DEFAULT_PUBKEY:
        tail.append(_m(pool_v2_pda(p.base_mint)))
    tail += [_m(buyback), _m(ata(buyback, p.quote_mint, quote_tp), writable=True)]
    return tail


def _extend(user: str, p: AmmPoolInfo) -> list[Instruction]:
    if p.account_size >= POOL_ACCOUNT_NEW_SIZE:
        return []
    return [Instruction(_pk(PUMP_AMM), EXTEND_ACCOUNT, [_m(p.pool, writable=True), _m(user, True, True), _m(SYSTEM),
                                                        _m(event_authority_pda(PUMP_AMM)), _m(PUMP_AMM)])]


def amm_buy_ixs(*, user: str, pool: AmmPoolInfo, base_token_program: str, base_out: int, max_quote_in: int,
                protocol_fee_recipient: str, buyback_fee_recipient: str) -> list[Instruction]:
    if pool.quote_mint != WSOL:
        raise ValueError("only SOL-quoted PumpSwap pools are supported")
    quote_tp = TOKEN
    wsol_ata = ata(user, WSOL, quote_tp)
    accounts = _amm_accounts(user, pool, base_token_program, quote_tp, protocol_fee_recipient) + [
        _m(global_volume_accumulator_pda(PUMP_AMM)), _m(user_volume_accumulator_pda(user, PUMP_AMM), writable=True),
        _m(fee_config_pda(PUMP_AMM)), _m(PUMP_FEE),
    ] + _amm_tail(user, pool, quote_tp, buyback_fee_recipient, sell=False)
    buy = Instruction(_pk(PUMP_AMM), BUY + struct.pack("<QQ", base_out, max_quote_in) + b"\x01", accounts)
    return _extend(user, pool) + [
        ata_create_idempotent(user, user, WSOL, quote_tp), system_transfer(user, wsol_ata, max_quote_in), sync_native(wsol_ata),
        ata_create_idempotent(user, user, pool.base_mint, base_token_program), buy, close_account(wsol_ata, user, user),
    ]


def amm_sell_ixs(*, user: str, pool: AmmPoolInfo, base_token_program: str, base_in: int, min_quote_out: int,
                 protocol_fee_recipient: str, buyback_fee_recipient: str) -> list[Instruction]:
    if pool.quote_mint != WSOL:
        raise ValueError("only SOL-quoted PumpSwap pools are supported")
    quote_tp = TOKEN
    accounts = _amm_accounts(user, pool, base_token_program, quote_tp, protocol_fee_recipient) + [
        _m(fee_config_pda(PUMP_AMM)), _m(PUMP_FEE),
    ] + _amm_tail(user, pool, quote_tp, buyback_fee_recipient, sell=True)
    sell = Instruction(_pk(PUMP_AMM), SELL + struct.pack("<QQ", base_in, min_quote_out), accounts)
    return _extend(user, pool) + [
        ata_create_idempotent(user, user, WSOL, quote_tp), sell, close_account(ata(user, WSOL, quote_tp), user, user),
    ]
