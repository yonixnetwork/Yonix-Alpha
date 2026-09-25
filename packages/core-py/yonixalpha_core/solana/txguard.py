"""Inspects a transaction built by a third party (PumpPortal) before our
wallet signs it. Fail-closed: anything not positively understood is a
violation and the transaction is not signed.

Checked:
- our wallet is the fee payer and the only signer;
- every invoked program is on the allowlist (System, Compute Budget, SPL
  Token, Token-2022, Associated Token, Pump, PumpSwap, Pump fee program);
- Pump / PumpSwap instructions are a known buy or sell of the expected
  side (or one of three user-level bookkeeping instructions), and their
  own bounds hold: a buy can spend at most `max_sol_in_lamports`, a sell
  sells at most `max_tokens_in` and requires at least `min_sol_out_lamports`;
- the trade targets the expected mint (it must be a static account key);
- SOL leaving the wallet by top-level System transfers (platform fee, tips)
  is at most `max_fee_transfer_lamports`, except wrapping into our own
  WSOL account, which counts against the buy budget;
- no top-level SPL Token instruction can move, approve away, or change the
  authority of our tokens; accounts may only be closed back to our wallet;
- the compute-budget priority fee is at most `max_priority_fee_lamports`.
Instructions nested inside the Pump programs (CPI) can't be inspected here;
their bounds are the instruction arguments checked above, which the
programs enforce on-chain.
"""

import struct
from dataclasses import dataclass, field
from typing import Any

from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

SYSTEM = "11111111111111111111111111111111"
COMPUTE_BUDGET = "ComputeBudget111111111111111111111111111111"
TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
ATA = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
PUMP = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMP_AMM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
PUMP_FEE = "pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ"
WSOL = "So11111111111111111111111111111111111111112"
ALLOWED_PROGRAMS = {SYSTEM, COMPUTE_BUDGET, TOKEN, TOKEN_2022, ATA, PUMP, PUMP_AMM, PUMP_FEE}


def _d(*b: int) -> bytes:
    return bytes(b)


# (program, discriminator) -> (side, kind). Discriminators from the official IDLs
# (pump-fun/pump-public-docs idl/pump.json, idl/pump_amm.json).
# kind "max_in": args (amount_out u64, max_in u64); "exact_in": (spend u64, min_out u64);
# "sell": (amount_in u64, min_out u64).
TRADE_IX = {
    (PUMP, _d(102, 6, 61, 18, 1, 218, 235, 234)): ("buy", "max_in"),  # buy
    (PUMP, _d(184, 23, 238, 97, 103, 197, 211, 61)): ("buy", "max_in"),  # buy_v2
    (PUMP, _d(56, 252, 116, 8, 158, 223, 205, 95)): ("buy", "exact_in"),  # buy_exact_sol_in
    (PUMP, _d(194, 171, 28, 70, 104, 77, 91, 47)): ("buy", "exact_in"),  # buy_exact_quote_in_v2
    (PUMP, _d(51, 230, 133, 164, 1, 127, 131, 173)): ("sell", "sell"),  # sell
    (PUMP, _d(93, 246, 130, 60, 231, 233, 64, 178)): ("sell", "sell"),  # sell_v2
    (PUMP_AMM, _d(102, 6, 61, 18, 1, 218, 235, 234)): ("buy", "max_in"),  # buy
    (PUMP_AMM, _d(198, 46, 21, 82, 180, 217, 232, 112)): ("buy", "exact_in"),  # buy_exact_quote_in
    (PUMP_AMM, _d(51, 230, 133, 164, 1, 127, 131, 173)): ("sell", "sell"),  # sell
}
AUX_IX = {  # user-level bookkeeping that may accompany a trade
    _d(94, 6, 202, 115, 255, 96, 232, 183),  # init_user_volume_accumulator
    _d(86, 31, 192, 87, 163, 87, 79, 238),  # sync_user_volume_accumulator
    _d(234, 102, 194, 203, 150, 72, 62, 229),  # extend_account
}
# SPL Token instruction tags allowed at top level: InitializeAccount(1),
# CloseAccount(9, destination must be our wallet), SyncNative(17),
# InitializeAccount3(18), InitializeImmutableOwner(22).
TOKEN_SAFE_TAGS = {1, 9, 17, 18, 22}


@dataclass
class GuardExpectation:
    wallet: str
    mint: str
    side: str  # buy | sell
    max_sol_in_lamports: int | None = None  # buy
    max_tokens_in: int | None = None  # sell (raw units)
    min_sol_out_lamports: int | None = None  # sell
    max_fee_transfer_lamports: int = 0
    max_priority_fee_lamports: int = 0


@dataclass
class GuardReport:
    ok: bool
    violations: list[str] = field(default_factory=list)
    programs: list[str] = field(default_factory=list)
    trade: dict[str, Any] | None = None
    fee_transfers_lamports: int = 0
    wrap_lamports: int = 0
    priority_fee_lamports: int = 0
    compute_unit_limit: int | None = None
    compute_unit_price_micro: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


def ata(owner: str, mint: str, token_program: str = TOKEN) -> str:
    return str(Pubkey.find_program_address(
        [bytes(Pubkey.from_string(owner)), bytes(Pubkey.from_string(token_program)), bytes(Pubkey.from_string(mint))],
        Pubkey.from_string(ATA))[0])


def inspect(tx: VersionedTransaction, exp: GuardExpectation) -> GuardReport:
    r = GuardReport(ok=False)
    msg = tx.message
    keys = [str(k) for k in msg.account_keys]
    lookups = list(getattr(msg, "address_table_lookups", []) or [])
    v = r.violations

    if not keys or keys[0] != exp.wallet:
        v.append(f"fee payer is {keys[0] if keys else None}, expected our wallet")
    if msg.header.num_required_signatures != 1:
        v.append(f"{msg.header.num_required_signatures} signers required; only our wallet may sign")
    if exp.mint not in keys:
        v.append("trade mint is not a static account key")
    own = {exp.wallet, ata(exp.wallet, WSOL), ata(exp.wallet, exp.mint), ata(exp.wallet, exp.mint, TOKEN_2022)}

    def key(i: int) -> str | None:
        return keys[i] if i < len(keys) else None  # None = loaded from an address lookup table

    trades = []
    for ix in msg.instructions:
        prog = key(ix.program_id_index)
        if prog is None:
            v.append("program id loaded from a lookup table")
            continue
        r.programs.append(prog)
        data = bytes(ix.data)
        accts = list(ix.accounts)
        if prog not in ALLOWED_PROGRAMS:
            v.append(f"program {prog} is not allowed")
            continue
        if prog == COMPUTE_BUDGET:
            if data[:1] == b"\x02" and len(data) >= 5:
                r.compute_unit_limit = struct.unpack_from("<I", data, 1)[0]
            elif data[:1] == b"\x03" and len(data) >= 9:
                r.compute_unit_price_micro = struct.unpack_from("<Q", data, 1)[0]
            elif data[:1] not in (b"\x01", b"\x04"):  # heap frame / loaded-accounts limit are harmless
                v.append(f"unknown compute-budget instruction {data[:1].hex()}")
        elif prog == SYSTEM:
            tag = struct.unpack_from("<I", data, 0)[0] if len(data) >= 4 else -1
            if tag == 2 and len(data) >= 12 and len(accts) >= 2:  # transfer
                lamports = struct.unpack_from("<Q", data, 4)[0]
                src, dst = key(accts[0]), key(accts[1])
                if src == exp.wallet and dst != exp.wallet:
                    if dst in own:
                        r.wrap_lamports += lamports
                    elif dst is None:
                        v.append("SOL transfer to an address from a lookup table")
                    else:
                        r.fee_transfers_lamports += lamports
            elif tag == 3 and len(data) >= 4 + 32 + 4:  # createAccountWithSeed (temporary WSOL account)
                base = Pubkey.from_bytes(data[4:36])
                if str(base) != exp.wallet:
                    v.append("createAccountWithSeed with a foreign base")
                seed_len = struct.unpack_from("<Q", data, 36)[0] if len(data) >= 44 else 0
                off = 44 + seed_len
                if len(data) >= off + 8:
                    r.wrap_lamports += struct.unpack_from("<Q", data, off)[0]
            else:
                v.append(f"system instruction {tag} is not allowed")
        elif prog in (TOKEN, TOKEN_2022):
            tag = data[0] if data else -1
            if tag not in TOKEN_SAFE_TAGS:
                v.append(f"token instruction {tag} could move or re-authorize tokens")
            elif tag == 9 and (len(accts) < 2 or key(accts[1]) != exp.wallet):
                v.append("token account closed to a destination other than our wallet")
        elif prog == ATA:
            if accts and key(accts[0]) != exp.wallet:
                v.append("associated token account paid by someone else")
        elif prog in (PUMP, PUMP_AMM):
            disc = data[:8]
            kind = TRADE_IX.get((prog, disc))
            if kind is None:
                if disc not in AUX_IX:
                    v.append(f"unknown {('Pump' if prog == PUMP else 'PumpSwap')} instruction {disc.hex()}")
                continue
            if len(data) < 24:
                v.append("truncated trade instruction")
                continue
            a1, a2 = struct.unpack_from("<QQ", data, 8)
            trades.append((prog, kind, a1, a2, [key(i) for i in accts]))
        # PUMP_FEE is only reached via CPI in practice; top-level calls carry no user funds.

    if len(trades) != 1:
        v.append(f"expected exactly one trade instruction, found {len(trades)}")
    else:
        prog, (side, kind), a1, a2, acct_keys = trades[0]
        r.trade = {"program": prog, "side": side, "kind": kind, "arg1": a1, "arg2": a2}
        if side != exp.side:
            v.append(f"transaction is a {side}, expected a {exp.side}")
        if exp.mint not in acct_keys:
            v.append("trade instruction does not reference the expected mint")
        if side == "buy":
            spend = a2 if kind == "max_in" else a1
            r.trade["max_sol_in"] = spend
            limit = exp.max_sol_in_lamports
            if limit is None or spend > limit:
                v.append(f"buy may spend {spend} lamports, limit {limit}")
            if limit is not None and r.wrap_lamports > limit:
                v.append(f"wraps {r.wrap_lamports} lamports, limit {limit}")
        elif side == "sell":
            if exp.max_tokens_in is None or a1 > exp.max_tokens_in:
                v.append(f"sells {a1} tokens, allowed {exp.max_tokens_in}")
            if exp.min_sol_out_lamports is not None and a2 < exp.min_sol_out_lamports:
                v.append(f"accepts as little as {a2} lamports, floor {exp.min_sol_out_lamports}")

    if r.compute_unit_price_micro is not None:
        units = r.compute_unit_limit if r.compute_unit_limit is not None else 200_000
        r.priority_fee_lamports = units * r.compute_unit_price_micro // 1_000_000
    if r.priority_fee_lamports > exp.max_priority_fee_lamports:
        v.append(f"priority fee {r.priority_fee_lamports} lamports exceeds limit {exp.max_priority_fee_lamports}")
    if r.fee_transfers_lamports > exp.max_fee_transfer_lamports:
        v.append(f"{r.fee_transfers_lamports} lamports transferred out as fees, limit {exp.max_fee_transfer_lamports}")
    if lookups:
        # Allowed only if nothing that matters above came from a table (checked per use).
        r.programs.append(f"+{len(lookups)} lookup table(s)")
    r.ok = not v
    return r
