"""Returns the rent deposit of the wallet's EMPTY token accounts.

Every buy opens (idempotently) the wallet's token account for the bought
token; its rent (0.00151384 SOL for a 170-byte Token-2022 account, measured
2026-09-28) stays locked in the account until the account is closed. After
a full exit the account holds nothing, and closing it sends the deposit back
to the wallet.

Only accounts that, read from chain right before building, are owned by our
wallet, hold exactly zero tokens, are not frozen, have no other close
authority and no withheld transfer fees are closed. Accounts of tokens with
an open or pending live position are never touched. The transaction holds
nothing but compute-budget instructions and one CloseAccount per account,
each sending the lamports to our own wallet; `txguard.inspect_close_accounts`
refuses anything else before signing.
"""

from dataclasses import asdict, dataclass
from typing import Any

from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.hash import Hash
from solders.message import MessageV0
from solders.pubkey import Pubkey

from yonixalpha_core.solana import pump_tx as p
from yonixalpha_core.solana.txguard import TOKEN, TOKEN_2022, WSOL

MAX_ACCOUNTS_PER_TX = 8
CU_BASE = 1_000
CU_PER_CLOSE = 10_000  # CloseAccount uses a few thousand CU (Token-2022 more); headroom, never a failure
PRIORITY_FEE_LAMPORTS = 10_000  # 0.00001 SOL per reclaim transaction: not time-critical


@dataclass
class TokenAccount:
    account: str
    mint: str
    program: str
    lamports: int
    amount: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


async def scan(rpc, wallet: str, mints: set[str] | None = None,
               exclude_mints: set[str] = frozenset()) -> tuple[list[TokenAccount], list[dict[str, Any]]]:
    """(closable accounts, skipped accounts with the reason) among the
    wallet's token accounts, optionally only those of `mints`."""
    closable: list[TokenAccount] = []
    skipped: list[dict[str, Any]] = []
    for program in (TOKEN, TOKEN_2022):
        res = await rpc.call("getTokenAccountsByOwner", [wallet, {"programId": program},
                                                         {"encoding": "jsonParsed", "commitment": "confirmed"}])
        for acc in (res or {}).get("value") or []:
            a = acc.get("account") or {}
            info = ((a.get("data") or {}).get("parsed") or {}).get("info") or {}
            mint = info.get("mint")
            if mints is not None and mint not in mints:
                continue
            ta = TokenAccount(acc.get("pubkey"), mint, program, int(a.get("lamports") or 0),
                              int((info.get("tokenAmount") or {}).get("amount") or 0))
            why = _refusal(ta, info, wallet, exclude_mints, a.get("owner"))
            if why:
                skipped.append({**ta.to_dict(), "reason": why})
            else:
                closable.append(ta)
    return closable, skipped


def _refusal(ta: TokenAccount, info: dict, wallet: str, exclude_mints: set[str], account_owner: str | None) -> str | None:
    if account_owner is not None and account_owner != ta.program:
        return f"account owned by {account_owner}"
    if info.get("owner") != wallet:
        return "not owned by our wallet"
    if ta.mint == WSOL or info.get("isNative"):
        return "wrapped SOL account"
    if ta.mint in exclude_mints:
        return "a live position for this token is open or pending"
    if ta.amount != 0:
        return f"holds {ta.amount} raw tokens"
    if info.get("state") != "initialized":
        return f"state {info.get('state')}"
    if info.get("closeAuthority") not in (None, wallet):
        return "another close authority"
    for ext in info.get("extensions") or []:
        if ext.get("extension") == "transferFeeAmount" and int((ext.get("state") or {}).get("withheldAmount") or 0) > 0:
            return "withheld transfer fees"
    if not ta.account:
        return "no address"
    return None


def build(wallet: str, accounts: list[TokenAccount], blockhash: str,
          priority_fee_lamports: int = PRIORITY_FEE_LAMPORTS) -> MessageV0:
    units = CU_BASE + CU_PER_CLOSE * len(accounts)
    micro = priority_fee_lamports * 1_000_000 // units if priority_fee_lamports else 0
    ixs = [set_compute_unit_limit(units), set_compute_unit_price(micro)]
    ixs += [p.close_account(a.account, wallet, wallet, a.program) for a in accounts]
    return MessageV0.try_compile(Pubkey.from_string(wallet), ixs, [], Hash.from_string(blockhash))
