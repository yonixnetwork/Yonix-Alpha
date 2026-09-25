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
    since one wallet can hold several token accounts."""
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
    ranked = sorted(per_owner.values(), reverse=True)
    supply = Decimal(supply_raw)
    top1 = Decimal(ranked[0]) / supply if ranked else Decimal(0)
    top10 = Decimal(sum(ranked[:10])) / supply
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
    )
