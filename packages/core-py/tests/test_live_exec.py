"""Live execution boundary tests: real transactions built and signed with
solders, a fake PumpPortal that returns them, and a fake RPC that answers
like a Solana node. Nothing leaves the process."""

import struct
from decimal import Decimal
from types import SimpleNamespace

import pytest
from pydantic import SecretStr
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer
from solders.transaction import VersionedTransaction

from yonixalpha_core.solana import txguard
from yonixalpha_core.solana.codec import b58encode
from yonixalpha_core.solana.live_exec import SolanaLiveExecutor, parse_fill
from yonixalpha_core.solana.pumpportal import PumpPortalError, TradeRequest
from yonixalpha_core.solana.txguard import GuardExpectation, inspect
from yonixalpha_core.solana.wallet import WalletError, load_wallet, wallet_status

KP = Keypair()
WALLET = str(KP.pubkey())
MINT = str(Pubkey.new_unique())
PUMP = Pubkey.from_string(txguard.PUMP)
FEE_ACCOUNT = Pubkey.new_unique()


def settings(**kw):
    base = {"WALLET_PRIVATE_KEY": SecretStr(b58encode(bytes(KP))), "WALLET_PUBLIC_KEY": WALLET}
    base.update(kw)
    return SimpleNamespace(**base)


def cb_price(micro: int) -> Instruction:
    return Instruction(Pubkey.from_string(txguard.COMPUTE_BUDGET), bytes([3]) + struct.pack("<Q", micro), [])


def cb_limit(units: int) -> Instruction:
    return Instruction(Pubkey.from_string(txguard.COMPUTE_BUDGET), bytes([2]) + struct.pack("<I", units), [])


def pump_ix(disc: bytes, a1: int, a2: int, mint=MINT) -> Instruction:
    return Instruction(PUMP, disc + struct.pack("<QQ", a1, a2), [
        AccountMeta(KP.pubkey(), True, True), AccountMeta(Pubkey.from_string(mint), False, False)])


BUY = bytes([102, 6, 61, 18, 1, 218, 235, 234])
SELL = bytes([51, 230, 133, 164, 1, 127, 131, 173])


def build(ixs, payer=KP.pubkey()) -> VersionedTransaction:
    msg = MessageV0.try_compile(payer, ixs, [], Hash.new_unique())
    return VersionedTransaction.populate(msg, [])


def buy_tx(max_sol=110_000_000, fee=500_000, micro=100_000, extra=()):
    return build([cb_limit(200_000), cb_price(micro), pump_ix(BUY, 1_000_000, max_sol),
                  transfer(TransferParams(from_pubkey=KP.pubkey(), to_pubkey=FEE_ACCOUNT, lamports=fee)), *extra])


def buy_exp(**kw):
    e = dict(wallet=WALLET, mint=MINT, side="buy", max_sol_in_lamports=115_000_000,
             max_fee_transfer_lamports=1_000_000, max_priority_fee_lamports=50_000)
    e.update(kw)
    return GuardExpectation(**e)


# --- wallet ---------------------------------------------------------------

def test_wallet_loads_base58_and_json_and_checks_public_key():
    w = load_wallet(settings())
    assert w.pubkey == WALLET and "keypair" not in repr(w) and b58encode(bytes(KP)) not in repr(w)
    w2 = load_wallet(settings(WALLET_PRIVATE_KEY=SecretStr(str(list(bytes(KP)))), WALLET_PUBLIC_KEY=None))
    assert w2.pubkey == WALLET
    with pytest.raises(WalletError) as e:
        load_wallet(settings(WALLET_PUBLIC_KEY=str(Pubkey.new_unique())))
    assert b58encode(bytes(KP)) not in str(e.value)
    assert load_wallet(settings(WALLET_PRIVATE_KEY=None)) is None
    with pytest.raises(WalletError):
        load_wallet(settings(WALLET_PRIVATE_KEY=SecretStr("not-base58-0OIl")))
    st = wallet_status(settings(WALLET_PRIVATE_KEY=SecretStr("abc")))
    assert st["valid"] is False and "abc" not in str(st)


# --- guard ----------------------------------------------------------------

def test_guard_accepts_a_bounded_pump_buy():
    r = inspect(buy_tx(), buy_exp())
    assert r.ok, r.violations
    assert r.trade["max_sol_in"] == 110_000_000 and r.fee_transfers_lamports == 500_000 and r.priority_fee_lamports == 20_000


@pytest.mark.parametrize("tx,exp,needle", [
    (buy_tx(max_sol=200_000_000), buy_exp(), "may spend"),
    (buy_tx(fee=5_000_000), buy_exp(), "transferred out as fees"),
    (buy_tx(micro=10_000_000), buy_exp(), "priority fee"),
    (buy_tx(), buy_exp(side="sell", max_tokens_in=1), "is a buy"),
    (buy_tx(), buy_exp(mint=str(Pubkey.new_unique())), "mint"),
    (build([pump_ix(BUY, 1, 1)], payer=Pubkey.new_unique()), buy_exp(), "fee payer"),
    (buy_tx(extra=[Instruction(Pubkey.new_unique(), b"\x00", [])]), buy_exp(), "not allowed"),
    (buy_tx(extra=[Instruction(Pubkey.from_string(txguard.TOKEN), bytes([4]) + struct.pack("<Q", 1), [
        AccountMeta(KP.pubkey(), False, True)])]), buy_exp(), "move or re-authorize"),
    (buy_tx(extra=[pump_ix(BUY, 1, 1)]), buy_exp(), "exactly one trade"),
    (buy_tx(extra=[Instruction(PUMP, bytes(8) + bytes(16), [])]), buy_exp(), "unknown Pump instruction"),
])
def test_guard_refuses_anything_outside_the_bounds(tx, exp, needle):
    r = inspect(tx, exp)
    assert not r.ok and any(needle in v for v in r.violations), r.violations


def test_guard_sell_bounds():
    tx = build([pump_ix(SELL, 5_000, 90_000_000)])
    ok = inspect(tx, GuardExpectation(wallet=WALLET, mint=MINT, side="sell", max_tokens_in=5_000, min_sol_out_lamports=80_000_000))
    assert ok.ok, ok.violations
    low = inspect(tx, GuardExpectation(wallet=WALLET, mint=MINT, side="sell", max_tokens_in=5_000, min_sol_out_lamports=95_000_000))
    assert not low.ok and "floor" in low.violations[0]
    more = inspect(tx, GuardExpectation(wallet=WALLET, mint=MINT, side="sell", max_tokens_in=4_000, min_sol_out_lamports=1))
    assert not more.ok


# --- executor -------------------------------------------------------------

class FakePP:
    def __init__(self, tx=None, error=None):
        self.tx, self.error, self.requests = tx, error, []

    async def build_transaction(self, req):
        self.requests.append(req)
        if self.error:
            raise PumpPortalError(self.error)
        return bytes(self.tx)


class FakeRpc:
    """Answers like a node: the first status polls are empty, then the
    signature is confirmed and getTransaction shows the balance changes."""

    def __init__(self, sim_err=None, never_lands=False, onchain_err=None, pending_polls=2):
        self.sim_err, self.never_lands, self.onchain_err = sim_err, never_lands, onchain_err
        self.pending_polls, self.calls = pending_polls, []
        self.sent = 0

    async def call(self, method, params=None):
        self.calls.append(method)
        if method == "simulateTransaction":
            return {"value": {"err": self.sim_err, "logs": ["sim log"]}}
        if method == "sendTransaction":
            self.sent += 1
            return "sig"
        if method == "getSignatureStatuses":
            if self.never_lands or self.pending_polls > 0:
                self.pending_polls -= 1
                return {"value": [None]}
            return {"value": [{"confirmationStatus": "confirmed", "err": self.onchain_err}]}
        if method == "getTransaction":
            return {
                "slot": 123, "blockTime": 1700000000,
                "transaction": {"message": {"accountKeys": [{"pubkey": WALLET}, {"pubkey": MINT}]}},
                "meta": {"err": None, "fee": 25_000, "preBalances": [1_000_000_000, 0], "postBalances": [889_475_000, 0],
                         "preTokenBalances": [],
                         "postTokenBalances": [{"mint": MINT, "owner": WALLET, "uiTokenAmount": {"amount": "3500000000", "decimals": 6}}],
                         "logMessages": ["Program log: Instruction: Buy"]},
            }
        raise AssertionError(method)


def request():
    return TradeRequest(WALLET, "buy", MINT, "0.1", True, Decimal(10), Decimal("0.0001"), "pump")


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    async def sleep(self, s):
        self.t += s


async def run(pp, rpc):
    persisted = []

    async def on_signed(sig):
        persisted.append((sig, list(rpc.calls)))

    c = Clock()
    ex = SolanaLiveExecutor(rpc, pp, load_wallet(settings()), confirm_timeout=20, sleep=c.sleep, clock=c)
    return await ex.execute(request(), buy_exp(), on_signed), persisted


async def test_confirmed_buy_reports_the_actual_fill_and_persists_signature_before_sending():
    rpc = FakeRpc()
    out, persisted = await run(FakePP(buy_tx()), rpc)
    assert out.status == "CONFIRMED", out.error
    assert out.fill.token_change_raw == 3_500_000_000 and out.fill.sol_change_lamports == -110_525_000
    assert out.fill.fee_lamports == 25_000 and out.fill.token_decimals == 6
    sig, calls_before = persisted[0]
    assert sig == out.signature and "sendTransaction" not in calls_before  # stored before anything was sent
    assert calls_before == []
    assert rpc.calls.index("simulateTransaction") < rpc.calls.index("sendTransaction")


async def test_signed_transaction_carries_a_valid_signature_from_our_wallet():
    tx = buy_tx()
    signed = VersionedTransaction(tx.message, [KP])
    assert signed.verify_with_results() == [True] and str(signed.message.account_keys[0]) == WALLET


async def test_guard_violation_means_nothing_is_signed_or_sent():
    rpc = FakeRpc()
    out, persisted = await run(FakePP(buy_tx(max_sol=999_000_000)), rpc)
    assert out.status == "FAILED" and "guard" in out.error and persisted == [] and rpc.calls == []


async def test_failed_simulation_is_never_sent():
    rpc = FakeRpc(sim_err={"InstructionError": [2, {"Custom": 6002}]})
    out, _ = await run(FakePP(buy_tx()), rpc)
    assert out.status == "FAILED" and "simulation failed" in out.error and "sendTransaction" not in rpc.calls


async def test_transaction_that_never_lands_expires_after_rebroadcasting():
    rpc = FakeRpc(never_lands=True)
    out, _ = await run(FakePP(buy_tx()), rpc)
    assert out.status == "EXPIRED" and rpc.sent > 1 and out.fill is None


async def test_on_chain_error_is_a_failure_not_a_fill():
    rpc = FakeRpc(onchain_err={"InstructionError": [2, "Custom"]})
    out, _ = await run(FakePP(buy_tx()), rpc)
    assert out.status == "FAILED" and out.fill is None


async def test_provider_error_is_reported():
    out, persisted = await run(FakePP(error="trade-local HTTP 400: bad mint"), FakeRpc())
    assert out.status == "FAILED" and "400" in out.error and persisted == []


def test_parse_fill_uses_owner_and_mint_only():
    tx = {"transaction": {"message": {"accountKeys": [WALLET, "x"]}}, "meta": {
        "fee": 5000, "preBalances": [10, 0], "postBalances": [7, 0],
        "preTokenBalances": [{"mint": MINT, "owner": WALLET, "uiTokenAmount": {"amount": "100", "decimals": 6}},
                             {"mint": MINT, "owner": "other", "uiTokenAmount": {"amount": "999", "decimals": 6}}],
        "postTokenBalances": [{"mint": MINT, "owner": WALLET, "uiTokenAmount": {"amount": "40", "decimals": 6}},
                              {"mint": "othermint", "owner": WALLET, "uiTokenAmount": {"amount": "5", "decimals": 6}}]}}
    f = parse_fill(tx, WALLET, MINT)
    assert (f.sol_change_lamports, f.token_change_raw, f.fee_lamports) == (-3, -60, 5000)
