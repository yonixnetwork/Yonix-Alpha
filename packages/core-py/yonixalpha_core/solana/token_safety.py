"""Turns Solana RPC responses into the safety gate's TokenProgramInfo and
HolderInfo. Response shapes follow the Agave account-decoder's jsonParsed
output (anza-xyz/agave account-decoder-client-types/src/token.rs): a mint
is {"mintAuthority", "freezeAuthority", "supply", "decimals",
"extensions": [{"extension": <camelCase tag>, "state": {...}}]}.
"""

from datetime import datetime
from decimal import Decimal
from typing import Any

from yonixalpha_core.safety.models import HolderInfo, Observation, TokenProgramInfo

SPL_TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"


class UnexpectedShape(Exception):
    pass


def parse_mint_account(account_info: dict[str, Any] | None, observed_at: datetime, source: str) -> TokenProgramInfo:
    """`account_info` is getAccountInfo's `value` with encoding=jsonParsed.
    Raises UnexpectedShape rather than returning a partially-filled object
    — a missing authority field must never read as "no authority"."""
    if not account_info:
        raise UnexpectedShape("mint account not found")
    owner = account_info.get("owner")
    data = account_info.get("data")
    if owner not in (SPL_TOKEN_PROGRAM, TOKEN_2022_PROGRAM):
        raise UnexpectedShape(f"account owner {owner} is not a token program")
    if not isinstance(data, dict) or data.get("parsed", {}).get("type") != "mint":
        raise UnexpectedShape("account data is not a jsonParsed mint")
    info = data["parsed"].get("info", {})
    for key in ("mintAuthority", "freezeAuthority", "decimals", "supply"):
        if key not in info:
            raise UnexpectedShape(f"mint info missing {key}")

    result = TokenProgramInfo(
        observation=Observation(source, observed_at),
        token_program=owner,
        mint_authority=info["mintAuthority"],
        freeze_authority=info["freezeAuthority"],
        decimals=int(info["decimals"]),
        supply_raw=int(info["supply"]),
    )
    for ext in info.get("extensions", []) or []:
        tag = ext.get("extension")
        state = ext.get("state") or {}
        if not tag:
            continue
        result.extensions.append(tag)
        if tag == "transferFeeConfig":
            newer = state.get("newerTransferFee") or {}
            older = state.get("olderTransferFee") or {}
            # Both schedules can apply depending on epoch; take the higher one
            # rather than guessing which is active.
            fees = [int(x.get("transferFeeBasisPoints", 0)) for x in (newer, older) if x]
            result.transfer_fee_bps = max(fees) if fees else None
            result.transfer_fee_authority = state.get("transferFeeConfigAuthority")
        elif tag == "transferHook":
            result.transfer_hook_program = state.get("programId")
            if not state.get("programId"):
                # A hook extension with no program set is inert.
                result.extensions.remove(tag)
                result.extensions.append("transferHookUnset")
        elif tag == "permanentDelegate":
            result.permanent_delegate = state.get("delegate")
            if not state.get("delegate"):
                result.extensions.remove(tag)
                result.extensions.append("permanentDelegateUnset")
        elif tag == "defaultAccountState":
            result.default_account_state = state.get("accountState")
        elif tag == "pausableConfig":
            result.paused = bool(state.get("paused"))
        elif tag == "unparseableExtension":
            result.unparseable_extension = True
    return result


# Pump.fun Mayhem mode (create_v2 with is_mayhem_mode): part of the supply
# sits in a vault of the Mayhem program (address from the official pump IDL,
# create_v2.mayhem_program_id), which trades it as an automated agent. Its
# SOL vault PDA (seed "sol-vault") is the vault authority.
MAYHEM_PROGRAM = "MAyhSmzXzV1pTf7LsNkrNwkWKTo4ougAJ1PPg47MD4e"


def _mayhem_authorities() -> frozenset[str]:
    try:
        from solders.pubkey import Pubkey

        pda = Pubkey.find_program_address([b"sol-vault"], Pubkey.from_string(MAYHEM_PROGRAM))[0]
        return frozenset({str(pda), MAYHEM_PROGRAM})
    except Exception:  # noqa: BLE001 - labelling only; classification below still works
        return frozenset({MAYHEM_PROGRAM})


MAYHEM_AUTHORITIES = _mayhem_authorities()


def owner_kind(owner: str) -> str:
    """"wallet" for an address on the ed25519 curve (a keypair someone
    holds), "protocol_agent" for the Mayhem agent's vault authority,
    "program" for any other program-derived address (pools, vaults,
    lockers: controlled by a program, not directly by a person)."""
    if owner in MAYHEM_AUTHORITIES:
        return "protocol_agent"
    try:
        from solders.pubkey import Pubkey

        return "wallet" if Pubkey.from_string(owner).is_on_curve() else "program"
    except Exception:  # noqa: BLE001 - an unparseable owner is treated as a wallet (the stricter reading)
        return "wallet"


def parse_holders(
    largest_accounts: list[dict[str, Any]],
    account_owners: dict[str, str | None],
    supply_raw: int,
    excluded_owners: set[str],
    creator: str | None,
    observed_at: datetime,
    source: str,
) -> HolderInfo:
    """`largest_accounts` is getTokenLargestAccounts' value ({address,
    amount}); `account_owners` maps each token-account address to its owner
    wallet (from getMultipleAccounts jsonParsed). Token accounts owned by
    `excluded_owners` — the bonding curve or pool — are dropped: they hold
    liquidity, not a holder's position. Balances are aggregated per owner,
    since one wallet can hold several token accounts.

    top1/top10 are shares of total supply held by wallets. Program-owned
    accounts are summed separately (program_controlled_share, and the
    Mayhem agent vault in protocol_agent_share) rather than reported as
    "one wallet holds X%"."""
    if supply_raw <= 0:
        raise UnexpectedShape("supply must be positive")
    per_owner: dict[str, int] = {}
    excluded = 0
    for acct in largest_accounts:
        address = acct.get("address")
        amount = int(acct.get("amount", 0))
        owner = account_owners.get(address)
        if owner is None:
            raise UnexpectedShape(f"owner unknown for token account {address}")
        if owner in excluded_owners:
            excluded += 1
            continue
        per_owner[owner] = per_owner.get(owner, 0) + amount
    supply = Decimal(supply_raw)
    wallets = {o: a for o, a in per_owner.items() if owner_kind(o) == "wallet"}
    programs = {o: a for o, a in per_owner.items() if owner_kind(o) == "program"}
    agent = sum(a for o, a in per_owner.items() if owner_kind(o) == "protocol_agent")
    ranked = sorted(wallets.items(), key=lambda kv: kv[1], reverse=True)
    top1 = Decimal(ranked[0][1]) / supply if ranked else Decimal(0)
    top10 = Decimal(sum(a for _, a in ranked[:10])) / supply
    biggest_program = max(programs.items(), key=lambda kv: kv[1]) if programs else None
    # getTokenLargestAccounts returns at most 20 accounts, so a creator
    # absent from it holds less than the 20th-largest balance; 0 here is a
    # lower bound for that case, not a measured zero.
    creator_share = Decimal(per_owner.get(creator, 0)) / supply if creator else None
    return HolderInfo(
        observation=Observation(source, observed_at),
        top1_share=top1,
        top10_share=top10,
        creator_share=creator_share,
        holders_sampled=len(per_owner),
        excluded_pool_accounts=excluded,
        top1_owner=ranked[0][0] if ranked else None,
        protocol_agent_share=Decimal(agent) / supply,
        program_controlled_share=Decimal(sum(programs.values())) / supply,
        largest_program_owner=biggest_program[0] if biggest_program else None,
        largest_program_share=Decimal(biggest_program[1]) / supply if biggest_program else Decimal(0),
    )
