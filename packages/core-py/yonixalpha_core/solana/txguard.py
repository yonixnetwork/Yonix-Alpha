"""Inspects a transaction before our wallet signs it — whoever built it
(PumpPortal, the native Pump builder, or Jupiter's swap API). Fail-closed:
anything not positively understood is a violation and nothing is signed.

The checks depend on the execution venue, because each venue has its own
legitimate layout (setup, compute-budget, ATA and cleanup instructions are
normal); what never changes is that the transaction must do exactly the
trade that was requested and nothing else with our funds.

Every venue:
- our wallet is the fee payer and the only signer;
- every invoked program is on the venue's allowlist, and program ids are
  static keys;
- SOL leaving the wallet by top-level System transfers (platform fee, tips)
  is at most `max_fee_transfer_lamports`, except wrapping into our own WSOL
  account, which counts against the buy budget;
- no top-level SPL Token instruction can move, approve away, or change the
  authority of our tokens; accounts may only be closed back to our wallet;
- associated token accounts are created only for our wallet, paid by it;
- the compute-budget priority fee is at most `max_priority_fee_lamports`.

Pump bonding curve / PumpSwap (`venue` PUMP_BONDING_CURVE / PUMP_AMM, or
None = either, for PumpPortal-built transactions):
- exactly one top-level trade instruction, a known buy or sell of the
  expected side on the expected program (official IDL discriminators);
- its accounts at the positions the program reads them from: our wallet as
  the user, the expected mint, our own token account for it, the mint's
  bonding-curve PDA / the expected pool, WSOL as the quote;
- its own bounds: a buy spends at most `max_sol_in_lamports` (and asks for
  at least `min_tokens_out` when given); a sell sells at most
  `max_tokens_in` and requires at least `min_sol_out_lamports`.

Jupiter (`venue` JUPITER_ROUTE):
- exactly one Jupiter v6 exact-in route instruction (route /
  shared_accounts_route; token-ledger and exact-out routes are refused);
- our wallet as the transfer authority, source and destination accounts
  our own, the expected mints, in_amount within the budget, slippage within
  `max_slippage_bps`, no platform fee, and the worst-case output
  (quoted_out less slippage) at least the required minimum.

Instructions nested inside the Pump / Jupiter programs (CPI) can't be
inspected; their bounds are the instruction arguments checked above, which
those programs enforce on-chain.
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
JUPITER = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"
WSOL = "So11111111111111111111111111111111111111112"
COMMON_PROGRAMS = {SYSTEM, COMPUTE_BUDGET, TOKEN, TOKEN_2022, ATA}
ALLOWED_PROGRAMS = COMMON_PROGRAMS | {PUMP, PUMP_AMM, PUMP_FEE}  # Pump venues
JUPITER_PROGRAMS = COMMON_PROGRAMS | {JUPITER}

PUMP_BONDING_CURVE, PUMP_AMM_VENUE, JUPITER_ROUTE = "PUMP_BONDING_CURVE", "PUMP_AMM", "JUPITER_ROUTE"
VENUE_PROGRAM = {PUMP_BONDING_CURVE: PUMP, PUMP_AMM_VENUE: PUMP_AMM}


def _d(*b: int) -> bytes:
    return bytes(b)


# (program, discriminator) -> (side, kind). Discriminators from the official IDLs
# (pump-fun/pump-public-docs idl/pump.json, idl/pump_amm.json).
# kind "max_in": args (amount_out u64, max_in u64); "exact_in": (spend u64, min_out u64);
# "sell": (amount_in u64, min_out u64).
BUY_V2 = _d(184, 23, 238, 97, 103, 197, 211, 61)
SELL_V2 = _d(93, 246, 130, 60, 231, 233, 64, 178)
BUY_EXACT_QUOTE_IN_V2 = _d(194, 171, 28, 70, 104, 77, 91, 47)
TRADE_IX = {
    (PUMP, _d(102, 6, 61, 18, 1, 218, 235, 234)): ("buy", "max_in"),  # buy
    (PUMP, BUY_V2): ("buy", "max_in"),  # buy_v2
    (PUMP, _d(56, 252, 116, 8, 158, 223, 205, 95)): ("buy", "exact_in"),  # buy_exact_sol_in
    (PUMP, BUY_EXACT_QUOTE_IN_V2): ("buy", "exact_in"),  # buy_exact_quote_in_v2
    (PUMP, _d(51, 230, 133, 164, 1, 127, 131, 173)): ("sell", "sell"),  # sell
    (PUMP, SELL_V2): ("sell", "sell"),  # sell_v2
    (PUMP_AMM, _d(102, 6, 61, 18, 1, 218, 235, 234)): ("buy", "max_in"),  # buy
    (PUMP_AMM, _d(198, 46, 21, 82, 180, 217, 232, 112)): ("buy", "exact_in"),  # buy_exact_quote_in
    (PUMP_AMM, _d(51, 230, 133, 164, 1, 127, 131, 173)): ("sell", "sell"),  # sell
}
# Where each trade instruction reads its critical accounts (official IDLs).
_CURVE_V1 = {"mint": 2, "curve": 3, "user_ata": 5, "user": 6}
_CURVE_V2 = {"mint": 1, "quote_mint": 2, "curve": 10, "user": 13, "user_ata": 14, "user_quote_ata": 15}
_AMM = {"pool": 0, "user": 1, "mint": 3, "quote_mint": 4, "user_ata": 5, "user_quote_ata": 6}
TRADE_ACCOUNTS = {
    (PUMP, disc): (_CURVE_V2 if disc in (BUY_V2, SELL_V2, BUY_EXACT_QUOTE_IN_V2) else _CURVE_V1)
    for (prog, disc) in TRADE_IX if prog == PUMP
} | {(PUMP_AMM, disc): _AMM for (prog, disc) in TRADE_IX if prog == PUMP_AMM}
AUX_IX = {  # user-level bookkeeping that may accompany a trade
    _d(94, 6, 202, 115, 255, 96, 232, 183),  # init_user_volume_accumulator
    _d(86, 31, 192, 87, 163, 87, 79, 238),  # sync_user_volume_accumulator
    _d(234, 102, 194, 203, 150, 72, 62, 229),  # extend_account
}
# Jupiter v6 exact-in routes (jup-ag/jupiter-cpi idl.json) -> positions of
# the accounts that decide where funds come from and go.
JUPITER_ROUTES = {
    _d(229, 23, 203, 151, 122, 227, 173, 42): ("route", {"authority": 1, "source": 2, "destination": 3, "destination_mint": 5}),
    _d(193, 32, 155, 51, 65, 214, 156, 129): ("shared_accounts_route",
                                              {"authority": 2, "source": 3, "destination": 6, "source_mint": 7, "destination_mint": 8}),
}
JUPITER_REFUSED = {
    _d(150, 86, 71, 116, 167, 93, 14, 104): "route_with_token_ledger (input amount not bounded in the instruction)",
    _d(230, 121, 143, 80, 119, 159, 106, 170): "shared_accounts_route_with_token_ledger (input amount not bounded)",
    _d(176, 209, 105, 168, 154, 125, 69, 62): "shared_accounts_exact_out_route (exact-out was not requested)",
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
    venue: str | None = None  # PUMP_BONDING_CURVE | PUMP_AMM | JUPITER_ROUTE; None = either Pump venue
    pool: str | None = None  # expected PumpSwap pool, when known
    min_tokens_out: int | None = None  # buy: the least the trade may accept
    max_slippage_bps: int | None = None  # Jupiter


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
    venue: str | None = None
    instructions: list[dict[str, Any]] = field(default_factory=list)  # program + first data bytes, for diagnosis

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


def ata(owner: str, mint: str, token_program: str = TOKEN) -> str:
    return str(Pubkey.find_program_address(
        [bytes(Pubkey.from_string(owner)), bytes(Pubkey.from_string(token_program)), bytes(Pubkey.from_string(mint))],
        Pubkey.from_string(ATA))[0])


def bonding_curve(mint: str) -> str:
    return str(Pubkey.find_program_address([b"bonding-curve", bytes(Pubkey.from_string(mint))], Pubkey.from_string(PUMP))[0])


def inspect(tx: VersionedTransaction, exp: GuardExpectation, loaded: list[str] | None = None) -> GuardReport:
    """`loaded`: the addresses the message's lookup tables resolve to
    (writable then read-only, the runtime's order); without them any account
    that comes from a table is unknown and can't satisfy a check."""
    r = GuardReport(ok=False, venue=exp.venue or "PUMP")
    msg = tx.message
    static = [str(k) for k in msg.account_keys]
    lookups = list(getattr(msg, "address_table_lookups", []) or [])
    keys = static + list(loaded or [])
    v = r.violations
    jupiter = exp.venue == JUPITER_ROUTE
    allowed = JUPITER_PROGRAMS if jupiter else ALLOWED_PROGRAMS

    if not static or static[0] != exp.wallet:
        v.append(f"fee payer is {static[0] if static else None}, expected our wallet")
    if msg.header.num_required_signatures != 1:
        v.append(f"{msg.header.num_required_signatures} signers required; only our wallet may sign")
    if exp.mint not in keys:
        v.append("trade mint is not among the transaction's accounts")
    wsol_ata = ata(exp.wallet, WSOL)
    token_atas = {ata(exp.wallet, exp.mint), ata(exp.wallet, exp.mint, TOKEN_2022)}
    own = {exp.wallet, wsol_ata} | token_atas

    def key(i: int) -> str | None:
        return keys[i] if i < len(keys) else None  # None = from a lookup table we could not resolve

    trades: list[tuple] = []
    routes: list[tuple] = []
    for ix in msg.instructions:
        prog = static[ix.program_id_index] if ix.program_id_index < len(static) else None
        data = bytes(ix.data)
        accts = list(ix.accounts)
        r.instructions.append({"program": prog, "data": data[:8].hex(), "accounts": len(accts)})
        if prog is None:
            v.append("program id loaded from a lookup table")
            continue
        r.programs.append(prog)
        if prog not in allowed:
            v.append(f"program {prog} is not allowed for {r.venue}")
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
            elif len(accts) >= 3 and key(accts[2]) != exp.wallet:
                v.append(f"associated token account created for another owner ({key(accts[2])})")
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
            trades.append((prog, disc, kind, a1, a2, [key(i) for i in accts]))
        elif prog == JUPITER:
            disc = data[:8]
            if disc in JUPITER_REFUSED:
                v.append(f"Jupiter {JUPITER_REFUSED[disc]} refused")
            elif disc in JUPITER_ROUTES:
                routes.append((disc, data, [key(i) for i in accts]))
            else:
                v.append(f"unknown Jupiter instruction {disc.hex()} (not an exact-in route this guard can verify)")
        # PUMP_FEE is only reached via CPI in practice; top-level calls carry no user funds.

    if jupiter:
        _check_jupiter(r, exp, routes, own, wsol_ata, token_atas)
    else:
        _check_pump(r, exp, trades, wsol_ata, token_atas)

    if r.compute_unit_price_micro is not None:
        units = r.compute_unit_limit if r.compute_unit_limit is not None else 200_000
        r.priority_fee_lamports = units * r.compute_unit_price_micro // 1_000_000
    if r.priority_fee_lamports > exp.max_priority_fee_lamports:
        v.append(f"priority fee {r.priority_fee_lamports} lamports exceeds limit {exp.max_priority_fee_lamports}")
    if r.fee_transfers_lamports > exp.max_fee_transfer_lamports:
        v.append(f"{r.fee_transfers_lamports} lamports transferred out as fees, limit {exp.max_fee_transfer_lamports}")
    if lookups:
        r.programs.append(f"+{len(lookups)} lookup table(s)" + ("" if loaded else " (unresolved)"))
    r.ok = not v
    return r


def _check_pump(r: GuardReport, exp: GuardExpectation, trades: list, wsol_ata: str, token_atas: set[str]) -> None:
    v = r.violations
    if len(trades) != 1:
        v.append(f"expected exactly one trade instruction, found {len(trades)}")
        return
    prog, disc, (side, kind), a1, a2, acct_keys = trades[0]
    r.trade = {"program": prog, "side": side, "kind": kind, "arg1": a1, "arg2": a2}
    want = VENUE_PROGRAM.get(exp.venue or "")
    if want is not None and prog != want:
        v.append(f"trade runs on {prog}, expected the {exp.venue} program {want}")
    if side != exp.side:
        v.append(f"transaction is a {side}, expected a {exp.side}")
    pos = TRADE_ACCOUNTS[(prog, disc)]
    if len(acct_keys) <= max(pos.values()):
        v.append(f"trade instruction has {len(acct_keys)} accounts, too few for its layout")
    else:
        at = {name: acct_keys[i] for name, i in pos.items()}
        if at["user"] != exp.wallet:
            v.append(f"trade user is {at['user']}, expected our wallet")
        if at["mint"] != exp.mint:
            v.append(f"trade mint is {at['mint']}, expected {exp.mint}")
        if at["user_ata"] not in token_atas:
            v.append(f"token account {at['user_ata']} is not our wallet's account for the mint")
        if "curve" in at and at["curve"] != bonding_curve(exp.mint):
            v.append(f"bonding curve {at['curve']} is not the mint's bonding curve")
        if "quote_mint" in at and at["quote_mint"] != WSOL:
            v.append(f"quote mint {at['quote_mint']} is not SOL")
        if "user_quote_ata" in at and at["user_quote_ata"] != wsol_ata:
            v.append("quote token account is not our wallet's WSOL account")
        if exp.pool and "pool" in at and at["pool"] != exp.pool:
            v.append(f"pool {at['pool']} is not the expected pool {exp.pool}")
    if side == "buy":
        spend = a2 if kind == "max_in" else a1
        min_out = a1 if kind == "max_in" else a2
        r.trade["max_sol_in"] = spend
        limit = exp.max_sol_in_lamports
        if limit is None or spend > limit:
            v.append(f"buy may spend {spend} lamports, limit {limit}")
        if limit is not None and r.wrap_lamports > limit:
            v.append(f"wraps {r.wrap_lamports} lamports, limit {limit}")
        if exp.min_tokens_out is not None and min_out < exp.min_tokens_out:
            v.append(f"buy accepts {min_out} tokens, minimum {exp.min_tokens_out}")
    elif side == "sell":
        if exp.max_tokens_in is None or a1 > exp.max_tokens_in:
            v.append(f"sells {a1} tokens, allowed {exp.max_tokens_in}")
        if exp.min_sol_out_lamports is not None and a2 < exp.min_sol_out_lamports:
            v.append(f"accepts as little as {a2} lamports, floor {exp.min_sol_out_lamports}")


def _check_jupiter(r: GuardReport, exp: GuardExpectation, routes: list, own: set[str], wsol_ata: str,
                   token_atas: set[str]) -> None:
    v = r.violations
    if len(routes) != 1:
        v.append(f"expected exactly one Jupiter route instruction, found {len(routes)}")
        return
    disc, data, acct_keys = routes[0]
    name, pos = JUPITER_ROUTES[disc]
    if len(data) < 8 + 19:
        v.append("truncated Jupiter route instruction")
        return
    # Every exact-in route ends with in_amount u64, quoted_out_amount u64, slippage_bps u16, platform_fee_bps u8.
    in_amount, quoted_out, slippage_bps, platform_fee_bps = struct.unpack("<QQHB", data[-19:])
    min_out = quoted_out * (10_000 - min(slippage_bps, 10_000)) // 10_000
    r.trade = {"program": JUPITER, "instruction": name, "side": exp.side, "in_amount": in_amount,
               "quoted_out_amount": quoted_out, "slippage_bps": slippage_bps, "platform_fee_bps": platform_fee_bps,
               "min_out": min_out}
    if len(acct_keys) <= max(pos.values()):
        v.append(f"Jupiter route has {len(acct_keys)} accounts, too few for {name}")
        return
    at = {k: acct_keys[i] for k, i in pos.items()}
    if any(val is None for val in at.values()):
        v.append("a Jupiter route account comes from a lookup table that was not resolved")
        return
    if at["authority"] != exp.wallet:
        v.append(f"Jupiter transfer authority is {at['authority']}, expected our wallet")
    if platform_fee_bps != 0:
        v.append(f"Jupiter platform fee {platform_fee_bps} bps; none was requested")
    if exp.max_slippage_bps is not None and slippage_bps > exp.max_slippage_bps:
        v.append(f"slippage {slippage_bps} bps exceeds limit {exp.max_slippage_bps}")
    if exp.side == "buy":
        if at["source"] != wsol_ata:
            v.append("Jupiter source is not our wallet's WSOL account")
        if at.get("source_mint", WSOL) != WSOL:
            v.append(f"Jupiter source mint {at.get('source_mint')} is not SOL")
        if at["destination_mint"] != exp.mint:
            v.append(f"Jupiter destination mint {at['destination_mint']}, expected {exp.mint}")
        if at["destination"] not in token_atas:
            v.append("Jupiter destination is not our wallet's token account")
        limit = exp.max_sol_in_lamports
        if limit is None or in_amount > limit:
            v.append(f"Jupiter swaps {in_amount} lamports in, limit {limit}")
        if limit is not None and r.wrap_lamports > limit:
            v.append(f"wraps {r.wrap_lamports} lamports, limit {limit}")
        if exp.min_tokens_out is not None and min_out < exp.min_tokens_out:
            v.append(f"worst-case output {min_out} tokens, minimum {exp.min_tokens_out}")
    else:
        if at["source"] not in token_atas:
            v.append("Jupiter source is not our wallet's token account")
        if at.get("source_mint", exp.mint) != exp.mint:
            v.append(f"Jupiter source mint {at.get('source_mint')}, expected {exp.mint}")
        if at["destination_mint"] != WSOL:
            v.append(f"Jupiter destination mint {at['destination_mint']} is not SOL")
        if at["destination"] not in (wsol_ata, exp.wallet):
            v.append("Jupiter destination is not our wallet")
        if exp.max_tokens_in is None or in_amount > exp.max_tokens_in:
            v.append(f"Jupiter sells {in_amount} tokens, allowed {exp.max_tokens_in}")
        if exp.min_sol_out_lamports is not None and min_out < exp.min_sol_out_lamports:
            v.append(f"worst-case output {min_out} lamports, floor {exp.min_sol_out_lamports}")
