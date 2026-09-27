"""pump.fun program decoding, from the official pump-fun/pump-public-docs
IDL (idl/pump.json) and PUMP_PROGRAM_README.md.

Discriminators are Anchor's sha256("event:<Name>")[:8] /
sha256("account:<Name>")[:8]; tests recompute them so a typo here can't
survive. Layouts are the IDL's current field order. pump.fun appends
fields over time, so every decoder reads fields in order and stops at the
end of the data: an older, shorter event decodes to the fields it has.
Required fields missing means "not this event", never a guess.
"""

import base64
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from yonixalpha_core.safety.liquidity import ConstantProductModel
from yonixalpha_core.solana.codec import DEFAULT_PUBKEY, BorshReader, TruncatedData

PUMP_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMPSWAP_PROGRAM_ID = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
WSOL_MINT = "So11111111111111111111111111111111111111112"
LAMPORTS_PER_SOL = Decimal(1_000_000_000)

CREATE_EVENT = bytes([27, 114, 169, 77, 222, 235, 99, 118])
TRADE_EVENT = bytes([189, 219, 127, 211, 78, 230, 97, 238])
COMPLETE_EVENT = bytes([95, 114, 97, 156, 212, 46, 152, 8])
MIGRATION_EVENT = bytes([189, 233, 93, 185, 92, 148, 234, 148])
BONDING_CURVE_ACCOUNT = bytes([23, 183, 248, 55, 96, 216, 172, 96])
# Anchor's EVENT_IX_TAG (u64 0x1d9acb512ea545e4, little-endian): prefixes
# event data emitted through a self-CPI (emit_cpi!) instead of logs.
EVENT_IX_TAG = bytes([228, 69, 165, 46, 81, 203, 154, 29])

PROGRAM_DATA_PREFIX = "Program data: "


def _read_fields(r: BorshReader, spec: list[tuple[str, str]], required: int) -> dict[str, Any] | None:
    out: dict[str, Any] = {}
    for i, (name, kind) in enumerate(spec):
        try:
            if kind == "shareholders":
                count = r.u8() | (r.u8() << 8) | (r.u8() << 16) | (r.u8() << 24)
                if count > 1000:
                    raise ValueError("implausible shareholder count")
                items = []
                for _ in range(count):
                    addr = r.pubkey()
                    share = r.u8() | (r.u8() << 8)
                    items.append({"address": addr, "share_bps": share})
                out[name] = items
            else:
                out[name] = getattr(r, kind)()
        except TruncatedData:
            if i < required:
                return None
            break
    return out


_CREATE_FIELDS = [
    ("name", "string"), ("symbol", "string"), ("uri", "string"), ("mint", "pubkey"),
    ("bonding_curve", "pubkey"), ("user", "pubkey"), ("creator", "pubkey"), ("timestamp", "i64"),
    ("virtual_token_reserves", "u64"), ("virtual_sol_reserves", "u64"), ("real_token_reserves", "u64"),
    ("token_total_supply", "u64"), ("token_program", "pubkey"), ("is_mayhem_mode", "bool"),
    ("is_cashback_enabled", "bool"), ("quote_mint", "pubkey"), ("virtual_quote_reserves", "u64"),
    ("creator_fee_bps", "u64"), ("is_holder_reward", "bool"),
]
_TRADE_FIELDS = [
    ("mint", "pubkey"), ("sol_amount", "u64"), ("token_amount", "u64"), ("is_buy", "bool"), ("user", "pubkey"),
    ("timestamp", "i64"), ("virtual_sol_reserves", "u64"), ("virtual_token_reserves", "u64"),
    ("real_sol_reserves", "u64"), ("real_token_reserves", "u64"), ("fee_recipient", "pubkey"),
    ("fee_basis_points", "u64"), ("fee", "u64"), ("creator", "pubkey"), ("creator_fee_basis_points", "u64"),
    ("creator_fee", "u64"), ("track_volume", "bool"), ("total_unclaimed_tokens", "u64"),
    ("total_claimed_tokens", "u64"), ("current_sol_volume", "u64"), ("last_update_timestamp", "i64"),
    ("ix_name", "string"), ("mayhem_mode", "bool"), ("cashback_fee_basis_points", "u64"), ("cashback", "u64"),
    ("buyback_fee_basis_points", "u64"), ("buyback_fee", "u64"), ("shareholders", "shareholders"),
    ("quote_mint", "pubkey"), ("quote_amount", "u64"), ("virtual_quote_reserves", "u64"),
    ("real_quote_reserves", "u64"), ("holder_rewards_bps", "u64"), ("holder_rewards", "u64"),
]
_COMPLETE_FIELDS = [("user", "pubkey"), ("mint", "pubkey"), ("bonding_curve", "pubkey"), ("timestamp", "i64"),
                    ("quote_mint", "pubkey")]
_MIGRATION_FIELDS = [("user", "pubkey"), ("mint", "pubkey"), ("mint_amount", "u64"), ("sol_amount", "u64"),
                     ("pool_migration_fee", "u64"), ("bonding_curve", "pubkey"), ("timestamp", "i64"),
                     ("pool", "pubkey"), ("quote_mint", "pubkey")]

_EVENTS = {
    CREATE_EVENT: ("create", _CREATE_FIELDS, 6),
    TRADE_EVENT: ("trade", _TRADE_FIELDS, 10),
    COMPLETE_EVENT: ("complete", _COMPLETE_FIELDS, 4),
    MIGRATION_EVENT: ("migration", _MIGRATION_FIELDS, 8),
}


def decode_event(data: bytes) -> tuple[str, dict[str, Any]] | None:
    """Decodes one Anchor event payload (8-byte discriminator + Borsh body).
    Also accepts the self-CPI form, which prefixes EVENT_IX_TAG. Returns
    (kind, fields) or None for unknown events or undecodable bodies."""
    if data[:8] == EVENT_IX_TAG:
        data = data[8:]
    entry = _EVENTS.get(data[:8])
    if entry is None:
        return None
    kind, spec, required = entry
    try:
        fields = _read_fields(BorshReader(data, 8), spec, required)
    except ValueError:
        return None
    if fields is None:
        return None
    return kind, fields


def decode_log_events(logs: list[str]) -> list[tuple[str, dict[str, Any]]]:
    """Every pump.fun event found in a transaction's "Program data:" log
    lines. Non-pump payloads fail discriminator lookup and are skipped."""
    out = []
    for line in logs:
        if not line.startswith(PROGRAM_DATA_PREFIX):
            continue
        try:
            raw = base64.b64decode(line[len(PROGRAM_DATA_PREFIX):], validate=True)
        except (ValueError, TypeError):
            continue
        decoded = decode_event(raw)
        if decoded:
            out.append(decoded)
    return out


def is_sol_quoted(fields: dict[str, Any]) -> bool:
    """True for SOL-paired coins. Events predating the quote_mint field are
    SOL-paired (custom pairs came later), so an absent field reads as SOL."""
    quote = fields.get("quote_mint")
    return quote is None or quote in (DEFAULT_PUBKEY, WSOL_MINT)


def total_fee_bps(trade: dict[str, Any]) -> int | None:
    """Every fee a trader pays on the SOL leg of a trade, per the event's own
    bps fields: protocol + creator, exactly as the official SDK's getFee
    (@pump-fun/pump-sdk fees.ts). buyback_fee_basis_points is NOT added: it
    is Global.buyback_basis_points (<= 10_000), the share of the protocol
    fee routed to the buyback recipient, not a charge on the trade — adding
    it read a 5000 split as a 50% fee. Cashback is ignored (a rebate, and
    deprecated), which keeps the estimate on the conservative side."""
    if "fee_basis_points" not in trade:
        return None
    return int(trade.get("fee_basis_points", 0)) + int(trade.get("creator_fee_basis_points", 0))


@dataclass(frozen=True)
class BondingCurveState:
    virtual_token_reserves: int
    virtual_quote_reserves: int
    real_token_reserves: int
    real_quote_reserves: int
    token_total_supply: int
    complete: bool
    creator: str | None = None
    quote_mint: str | None = None

    @property
    def sol_quoted(self) -> bool:
        return self.quote_mint is None or self.quote_mint in (DEFAULT_PUBKEY, WSOL_MINT)

    def price_sol(self, decimals: int) -> Decimal:
        """SOL per whole token at the curve's current marginal price."""
        return (Decimal(self.virtual_quote_reserves) / LAMPORTS_PER_SOL) / (
            Decimal(self.virtual_token_reserves) / Decimal(10) ** decimals
        )

    def real_liquidity_sol(self) -> Decimal:
        return Decimal(self.real_quote_reserves) / LAMPORTS_PER_SOL

    def model(self, decimals: int, fee_bps: int) -> ConstantProductModel:
        return ConstantProductModel(
            quote_reserve=Decimal(self.virtual_quote_reserves) / LAMPORTS_PER_SOL,
            token_reserve=Decimal(self.virtual_token_reserves) / Decimal(10) ** decimals,
            fee_bps=Decimal(fee_bps),
            real_quote_reserve=self.real_liquidity_sol(),
        )


_CURVE_FIELDS = [
    ("virtual_token_reserves", "u64"), ("virtual_quote_reserves", "u64"), ("real_token_reserves", "u64"),
    ("real_quote_reserves", "u64"), ("token_total_supply", "u64"), ("complete", "bool"), ("creator", "pubkey"),
    ("is_mayhem_mode", "bool"), ("is_cashback_coin", "bool"), ("quote_mint", "pubkey"),
]


def decode_bonding_curve(data: bytes) -> BondingCurveState | None:
    """Decodes a BondingCurve account's raw data (getAccountInfo, base64).
    Returns None if the discriminator doesn't match or the six original
    fields are missing."""
    if data[:8] != BONDING_CURVE_ACCOUNT:
        return None
    fields = _read_fields(BorshReader(data, 8), _CURVE_FIELDS, 6)
    if fields is None:
        return None
    return BondingCurveState(
        virtual_token_reserves=fields["virtual_token_reserves"],
        virtual_quote_reserves=fields["virtual_quote_reserves"],
        real_token_reserves=fields["real_token_reserves"],
        real_quote_reserves=fields["real_quote_reserves"],
        token_total_supply=fields["token_total_supply"],
        complete=fields["complete"],
        creator=fields.get("creator"),
        quote_mint=fields.get("quote_mint"),
    )
