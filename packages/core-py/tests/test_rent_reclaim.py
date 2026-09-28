"""Returning token-account rent: only empty accounts of our wallet are
closed, only back to our wallet, and the guard refuses any transaction that
does anything else (tests/chain_fake stands in for the node)."""

import base64

from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer
from solders.transaction import VersionedTransaction

from yonixalpha_core.solana import pump_tx as p
from yonixalpha_core.solana import rent_reclaim as rr
from yonixalpha_core.solana.tx_builders import unsigned
from yonixalpha_core.solana.txguard import TOKEN, TOKEN_2022, inspect_close_accounts

from tests.chain_fake import FakeChain
from tests.test_execution_venues import WALLET, executor

RENT = 1_513_840
BLOCKHASH = "EETubP5AKHgjPAhzPAFcb8BAY1hMH639CWCFTqi3hq1k"


def new() -> str:
    return str(Pubkey.new_unique())


def token_acc(account: str, mint: str, amount: int = 0, *, owner: str = WALLET, program: str = TOKEN_2022, **info) -> dict:
    return {"pubkey": account, "account": {"lamports": RENT, "owner": program, "data": {"parsed": {"info": {
        "mint": mint, "owner": owner, "state": "initialized", "tokenAmount": {"amount": str(amount), "decimals": 6},
        **info}}}}}


class Chain(FakeChain):
    def __init__(self, accounts: list[dict]):
        super().__init__()
        self.token_accounts = accounts

    async def call(self, method, params=None):
        if method == "getTokenAccountsByOwner":
            self.calls.append(method)
            prog = params[1]["programId"]
            return {"value": [a for a in self.token_accounts if a["account"]["owner"] == prog]}
        return await super().call(method, params)


def test_scan_closes_only_empty_accounts_of_our_wallet_without_a_live_position():
    empty, busy_mint = new(), new()
    accounts = [
        token_acc(empty, new()),
        token_acc(new(), new(), amount=5),                                     # still holds tokens
        token_acc(new(), p.WSOL, program=TOKEN, isNative=True),                # wrapped SOL
        token_acc(new(), new(), state="frozen"),
        token_acc(new(), new(), closeAuthority=new()),
        token_acc(new(), new(), extensions=[{"extension": "transferFeeAmount", "state": {"withheldAmount": 3}}]),
        token_acc(new(), busy_mint),                                           # a live position is open for it
        token_acc(new(), new(), owner=new()),
    ]
    chain = Chain(accounts)
    import asyncio
    closable, skipped = asyncio.run(rr.scan(chain, WALLET, None, {busy_mint}))
    assert [a.account for a in closable] == [empty]
    reasons = {s["reason"] for s in skipped}
    assert {"holds 5 raw tokens", "wrapped SOL account", "state frozen", "another close authority", "withheld transfer fees",
            "a live position for this token is open or pending", "not owned by our wallet"} <= reasons


def _accounts(n=2):
    return [rr.TokenAccount(new(), new(), TOKEN_2022 if i % 2 else TOKEN, RENT, 0) for i in range(n)]


def test_guard_accepts_the_reclaim_transaction_it_was_built_for():
    accs = _accounts(3)
    tx = unsigned(rr.build(WALLET, accs, BLOCKHASH))
    r = inspect_close_accounts(tx, WALLET, {a.account: a.program for a in accs}, rr.PRIORITY_FEE_LAMPORTS)
    assert r.ok, r.violations
    assert r.priority_fee_lamports <= rr.PRIORITY_FEE_LAMPORTS


def _compile(ixs):
    return unsigned(MessageV0.try_compile(Pubkey.from_string(WALLET), ixs, [], Hash.from_string(BLOCKHASH)))


def test_guard_refuses_anything_but_closing_the_expected_accounts_to_our_wallet():
    accs = _accounts(2)
    expected = {a.account: a.program for a in accs}
    closes = [p.close_account(a.account, WALLET, WALLET, a.program) for a in accs]
    stranger = new()
    cases = {
        "not our wallet": [p.close_account(accs[0].account, stranger, WALLET, accs[0].program), closes[1]],
        "not one of the expected": closes + [p.close_account(new(), WALLET, WALLET, TOKEN)],
        "is not allowed in a rent-reclaim transaction": closes + [transfer(TransferParams(from_pubkey=Pubkey.from_string(WALLET),
                                                         to_pubkey=Pubkey.from_string(stranger), lamports=1))],
        "expected exactly": closes[:1],
        "not a plain CloseAccount": closes + [Instruction(Pubkey.from_string(TOKEN), bytes([3, 1, 0, 0, 0, 0, 0, 0, 0]), [
            AccountMeta(Pubkey.from_string(accs[0].account), False, True), AccountMeta(Pubkey.from_string(stranger), False, True),
            AccountMeta(Pubkey.from_string(WALLET), True, False)])],
        "which is not one of the expected empty accounts of this token program": [p.close_account(accs[0].account, WALLET, WALLET, TOKEN_2022 if accs[0].program == TOKEN else TOKEN),
                          closes[1]],
    }
    for needle, ixs in cases.items():
        r = inspect_close_accounts(_compile(ixs), WALLET, expected, rr.PRIORITY_FEE_LAMPORTS)
        assert not r.ok, needle
        assert any(needle in v for v in r.violations), (needle, r.violations)
    # A priority fee above the bound is refused too.
    greedy = rr.build(WALLET, accs, BLOCKHASH, priority_fee_lamports=10 * rr.PRIORITY_FEE_LAMPORTS)
    assert not inspect_close_accounts(unsigned(greedy), WALLET, expected, rr.PRIORITY_FEE_LAMPORTS).ok


async def test_executor_closes_empty_accounts_and_reports_the_refund():
    a1, a2 = new(), new()
    chain = Chain([token_acc(a1, new()), token_acc(a2, new(), program=TOKEN), token_acc(new(), new(), amount=9)])
    chain.fill = (WALLET, "x", 2 * RENT - 5_000 - 10_000, 0)
    ex = executor(chain)
    signed = []

    async def on_signed(sig):
        signed.append(sig)
    out = await ex.close_token_accounts(None, set(), on_signed)
    assert out.status == "CONFIRMED" and out.fill.sol_change_lamports == 2 * RENT - 15_000, out.error
    assert {c["account"] for c in out.reclaim["closing"]} == {a1, a2} and len(out.reclaim["skipped"]) == 1
    assert out.reclaim["expected_refund_lamports"] == 2 * RENT and len(signed) == 1
    stages = [s["stage"] for s in out.stages]
    assert stages[:5] == ["ACCOUNTS_SCANNED", "TRANSACTION_BUILT", "TRANSACTION_GUARD_PASSED", "TRANSACTION_SIGNED", "SIMULATED"]
    sent = VersionedTransaction.from_bytes(base64.b64decode(chain.sent[-1]))
    keys = [str(k) for k in sent.message.account_keys]
    assert {keys[ix.program_id_index] for ix in sent.message.instructions} <= {TOKEN, TOKEN_2022,
                                                                             "ComputeBudget111111111111111111111111111111"}


async def test_nothing_to_close_sends_nothing_and_a_failed_simulation_sends_nothing():
    chain = Chain([token_acc(new(), new(), amount=1)])
    out = await executor(chain).close_token_accounts(None, set(), _noop)
    assert out.status == "SKIPPED" and chain.sent == [] and "sendTransaction" not in chain.calls

    chain = Chain([token_acc(new(), new())])
    chain.sim_err = {"InstructionError": [2, {"Custom": 11}]}
    out = await executor(chain).close_token_accounts(None, set(), _noop)
    assert out.status == "FAILED" and out.stage == "SIMULATION_FAILED" and chain.sent == []


async def test_only_the_requested_token_is_closed_after_an_exit():
    target, other = new(), new()
    chain = Chain([token_acc(new(), target), token_acc(new(), other)])
    chain.fill = (WALLET, "x", RENT - 15_000, 0)
    out = await executor(chain).close_token_accounts({target}, set(), _noop)
    assert [c["mint"] for c in out.reclaim["closing"]] == [target]


async def _noop(sig):
    return None
