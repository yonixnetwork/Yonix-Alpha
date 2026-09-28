"""Venue-aware execution, end to end against a fake node (tests/chain_fake):
the venue comes from chain state, the transaction from the builder for that
venue, and the guard verifies the trade semantics before anything is
signed. Test numbers follow the audit's test matrix."""

import base64
import struct
from decimal import Decimal
from types import SimpleNamespace

import pytest
from pydantic import SecretStr
from solders.address_lookup_table_account import AddressLookupTableAccount
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from yonixalpha_core.solana import pump_tx as p
from yonixalpha_core.solana import txguard
from yonixalpha_core.solana.codec import b58encode
from yonixalpha_core.solana.live_exec import SolanaLiveExecutor
from yonixalpha_core.solana.market_data import QuoteResult
from yonixalpha_core.solana.pumpportal import TradeRequest
from yonixalpha_core.solana.txguard import GuardExpectation, inspect
from yonixalpha_core.solana.wallet import load_wallet

from tests.chain_fake import FakeChain

KP = Keypair()
WALLET = str(KP.pubkey())
MINT = str(Pubkey.new_unique())
CREATOR = str(Pubkey.new_unique())
SIZE = 100_000_000  # 0.1 SOL


def wallet():
    return load_wallet(SimpleNamespace(WALLET_PRIVATE_KEY=SecretStr(b58encode(bytes(KP))), WALLET_PUBLIC_KEY=WALLET))


def buy_req(mint=MINT):
    return TradeRequest(WALLET, "buy", mint, "0.1", True, Decimal(10), Decimal("0.0001"), "pump")


def buy_exp(mint=MINT, **kw):
    e = dict(wallet=WALLET, mint=mint, side="buy", max_sol_in_lamports=110_000_000, max_fee_transfer_lamports=1_000_000,
             max_priority_fee_lamports=200_000)
    e.update(kw)
    return GuardExpectation(**e)


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    async def sleep(self, s):
        self.t += s


def executor(chain, jupiter=None, builder="native", timeout=20):
    c = Clock()
    return SolanaLiveExecutor(chain, None, wallet(), confirm_timeout=timeout, sleep=c.sleep, clock=c, jupiter=jupiter,
                              builder=builder)


async def run(ex, req, exp):
    persisted = []

    async def on_signed(sig):
        persisted.append(sig)
    return await ex.execute(req, exp, on_signed), persisted


def sent_tx(chain) -> VersionedTransaction:
    return VersionedTransaction.from_bytes(base64.b64decode(chain.sent[-1]))


def programs(tx) -> list[str]:
    keys = [str(k) for k in tx.message.account_keys]
    return [keys[ix.program_id_index] for ix in tx.message.instructions]


def stage_names(out) -> list[str]:
    return [s["stage"] for s in out.stages]


# --- Test 1: fresh Pump.fun token, no DEX pool -----------------------------
@pytest.mark.parametrize("token_program", [p.TOKEN, p.TOKEN_2022])
async def test_1_fresh_token_without_a_dex_pool_trades_on_the_bonding_curve(token_program):
    chain = FakeChain()
    chain.add_mint(MINT, token_program)
    chain.add_curve(MINT, CREATOR)  # no pool anywhere
    chain.fill = (WALLET, MINT, -SIZE, 3_000_000_000)
    out, persisted = await run(executor(chain), buy_req(), buy_exp())
    assert out.status == "CONFIRMED", (out.error, out.stages)
    assert out.venue["venue"] == "PUMP_BONDING_CURVE" and out.provider == "native_pump"
    assert stage_names(out) == ["VENUE_RESOLVED", "TRANSACTION_BUILT", "TRANSACTION_GUARD_PASSED", "TRANSACTION_SIGNED",
                                "SIMULATED", "TRANSACTION_SUBMITTED", "TRANSACTION_SEEN", "TRANSACTION_CONFIRMED",
                                "FILL_VERIFIED"]
    tx = sent_tx(chain)
    assert p.PUMP in programs(tx) and p.PUMP_AMM not in programs(tx)
    buy = [ix for ix in tx.message.instructions if str(tx.message.account_keys[ix.program_id_index]) == p.PUMP][0]
    assert bytes(buy.data)[:8] == p.BUY and len(buy.accounts) == 18
    amount, max_cost = struct.unpack_from("<QQ", bytes(buy.data), 8)
    assert max_cost <= 110_000_000 and amount > 0
    assert persisted == [out.signature] and out.guard["fee_transfers_lamports"] == 0
    # Latency trace: every RPC call the executor made is timed, in order.
    methods = [c["method"] for c in out.rpc_calls]
    assert methods[:2] == ["getMultipleAccounts", "getAccountInfo"] and "sendTransaction" in methods
    before_send = methods[:methods.index("sendTransaction")]
    assert before_send.count("getLatestBlockhash") == 1 and "simulateTransaction" in before_send


# --- Test 2: migrates between build and signing -----------------------------
async def test_2_token_that_migrates_before_signing_is_rebuilt_for_pumpswap():
    chain = FakeChain()
    chain.add_mint(MINT)
    chain.add_curve(MINT, CREATOR)
    chain.fill = (WALLET, MINT, -SIZE, 3_000_000_000)
    state = {"built": 0}

    def migrate(method, params):
        if method == "getLatestBlockhash":
            state["built"] += 1
            if state["built"] == 1:  # right after the first (curve) build: the token graduates
                chain.add_curve(MINT, CREATOR, vtok=0, vsol=0, rtok=0, rsol=0, complete=True)
                chain.add_pool(MINT, CREATOR)
    chain.on_call = migrate
    out, _ = await run(executor(chain), buy_req(), buy_exp())
    assert out.status == "CONFIRMED", (out.error, out.stages)
    names = stage_names(out)
    assert names.count("TRANSACTION_BUILT") == 2 and "VENUE_CHANGED" in names
    changed = next(s for s in out.stages if s["stage"] == "VENUE_CHANGED")
    assert (changed["before"], changed["after"]) == ("PUMP_BONDING_CURVE", "PUMP_AMM")
    assert out.venue["venue"] == "PUMP_AMM" and p.PUMP_AMM in programs(sent_tx(chain))
    assert p.PUMP not in programs(sent_tx(chain))  # no stale bonding-curve transaction was sent
    assert len(chain.sent) >= 1 and len({*chain.sent}) == 1


async def test_2b_migration_in_progress_is_not_traded():
    chain = FakeChain()
    chain.add_mint(MINT)
    chain.add_curve(MINT, CREATOR, vtok=0, vsol=0, rtok=0, rsol=0, complete=True)  # no pool yet
    out, persisted = await run(executor(chain), buy_req(), buy_exp())
    assert out.status == "FAILED" and out.stage == "NO_EXECUTABLE_ROUTE" and "MIGRATION_IN_PROGRESS" in out.error
    assert persisted == [] and chain.sent == []


# --- Test 3: migrated token ------------------------------------------------
@pytest.mark.parametrize("pool_size", [300, 243])
async def test_3_migrated_token_trades_on_the_canonical_pumpswap_pool(pool_size):
    chain = FakeChain()
    chain.add_mint(MINT)
    chain.add_curve(MINT, CREATOR, vtok=0, vsol=0, rtok=0, rsol=0, complete=True)
    pool = chain.add_pool(MINT, CREATOR, size=pool_size)
    chain.fill = (WALLET, MINT, -SIZE, 2_000_000_000)
    out, _ = await run(executor(chain), buy_req(), buy_exp())
    assert out.status == "CONFIRMED", (out.error, out.stages)
    assert out.venue["venue"] == "PUMP_AMM" and out.venue["pool"]["address"] == pool
    tx = sent_tx(chain)
    progs = programs(tx)
    # setup + compute + swap + cleanup, all accepted: CU limit/price, [extend], WSOL ATA, wrap, sync, base ATA, buy, close
    assert progs.count(p.PUMP_AMM) == (2 if pool_size < 300 else 1)
    assert txguard.COMPUTE_BUDGET in progs and txguard.ATA in progs and p.SYSTEM in progs and p.TOKEN in progs
    assert out.guard["wrap_lamports"] <= 110_000_000


# --- Test 4 / 5: non-Pump token ---------------------------------------------
JUP = Pubkey.from_string(txguard.JUPITER)
SHARED_ROUTE = bytes([193, 32, 155, 51, 65, 214, 156, 129])


class FakeJupiter:
    def __init__(self, out_amount=5_000_000, route=True, tx_factory=None):
        self.out, self.route, self.tx_factory = out_amount, route, tx_factory
        self.swaps = 0

    async def quote(self, input_mint, output_mint, amount_raw, slippage_bps):
        if not self.route:
            return QuoteResult("no_route", error="COULD_NOT_FIND_ANY_ROUTE")
        return QuoteResult("ok", data={"inAmount": str(amount_raw), "outAmount": str(self.out), "priceImpactPct": "0.01",
                                       "routePlan": [{"swapInfo": {"label": "Raydium"}}]})

    async def swap_transaction(self, quote, user, priority_fee_lamports):
        self.swaps += 1
        return base64.b64encode(bytes(self.tx_factory(int(quote["inAmount"]), int(quote["outAmount"])))).decode()


def jupiter_like_tx(mint: str, in_amount: int, quoted_out: int, *, slippage_bps=1000, fee_bps=0, table=None,
                    authority=None, destination=None, extra=()):
    """Layout of a Jupiter /swap transaction for SOL -> token: compute budget,
    WSOL ATA + wrap + sync, destination ATA, shared_accounts_route, close WSOL.
    Accounts after the route's fixed ones go through a lookup table, as
    Jupiter does."""
    user = KP.pubkey()
    wsol = txguard.ata(WALLET, txguard.WSOL)
    dest = destination or txguard.ata(WALLET, mint)
    authority = authority or WALLET
    route_accounts = [txguard.TOKEN, str(Pubkey.new_unique()), authority, wsol, str(Pubkey.new_unique()),
                      str(Pubkey.new_unique()), dest, txguard.WSOL, mint, txguard.JUPITER, txguard.TOKEN_2022,
                      str(Pubkey.new_unique()), txguard.JUPITER]
    metas = [AccountMeta(Pubkey.from_string(a), a == authority and a == WALLET, i not in (0, 7, 8, 9, 10, 12))
             for i, a in enumerate(route_accounts)]
    data = SHARED_ROUTE + bytes([3]) + struct.pack("<I", 1) + bytes(10) + struct.pack("<QQHB", in_amount, quoted_out, slippage_bps, fee_bps)
    ixs = [p.Instruction(Pubkey.from_string(txguard.COMPUTE_BUDGET), bytes([2]) + struct.pack("<I", 300_000), []),
           p.Instruction(Pubkey.from_string(txguard.COMPUTE_BUDGET), bytes([3]) + struct.pack("<Q", 300), []),
           p.ata_create_idempotent(WALLET, WALLET, txguard.WSOL, txguard.TOKEN), p.system_transfer(WALLET, wsol, in_amount),
           p.sync_native(wsol), p.ata_create_idempotent(WALLET, WALLET, mint, txguard.TOKEN),
           Instruction(JUP, data, metas), p.close_account(wsol, WALLET, WALLET), *extra]
    tables = [AddressLookupTableAccount(Pubkey.from_string(table[0]), [Pubkey.from_string(a) for a in table[1]])] if table else []
    msg = MessageV0.try_compile(user, ixs, tables, Hash.new_unique())
    return VersionedTransaction.populate(msg, [])


async def test_4_non_pump_token_goes_through_a_validated_jupiter_route():
    other = str(Pubkey.new_unique())
    chain = FakeChain()
    chain.add_mint(other)  # a plain SPL token: no Pump curve, no Pump pool
    table_addr = str(Pubkey.new_unique())
    pooled = [str(Pubkey.new_unique()) for _ in range(3)]
    chain.add_table(table_addr, pooled + [txguard.ata(WALLET, other)])
    jup = FakeJupiter(tx_factory=lambda i, o: jupiter_like_tx(other, i, o, table=(table_addr, pooled + [txguard.ata(WALLET, other)])))
    chain.fill = (WALLET, other, -SIZE, 5_000_000)
    out, _ = await run(executor(chain, jupiter=jup), buy_req(other), buy_exp(other))
    assert out.status == "CONFIRMED", (out.error, out.stages)
    assert out.venue["venue"] == "JUPITER_ROUTE" and out.provider == "jupiter" and jup.swaps == 1
    assert out.guard["trade"]["instruction"] == "shared_accounts_route" and out.guard["trade"]["in_amount"] == SIZE
    assert "+1 lookup table(s)" in out.guard["programs"]  # destination came from a table and was resolved
    assert stage_names(out)[:3] == ["VENUE_RESOLVED", "TRANSACTION_BUILT", "TRANSACTION_GUARD_PASSED"]


async def test_5_non_pump_token_without_a_route_is_no_executable_route():
    other = str(Pubkey.new_unique())
    chain = FakeChain()
    chain.add_mint(other)
    out, persisted = await run(executor(chain, jupiter=FakeJupiter(route=False)), buy_req(other), buy_exp(other))
    assert out.status == "FAILED" and out.stage == "NO_EXECUTABLE_ROUTE" and "no executable route" in out.error
    assert persisted == [] and chain.sent == []


# --- Test 8: RPC down ---------------------------------------------------------
async def test_8_rpc_failure_is_a_controlled_rpc_unavailable():
    chain = FakeChain()
    chain.fail = {"getMultipleAccounts"}
    out, persisted = await run(executor(chain), buy_req(), buy_exp())
    assert out.status == "FAILED" and out.stage == "RPC_UNAVAILABLE" and persisted == [] and chain.sent == []


# --- Test 9 / 10: guard semantics -------------------------------------------
def test_9_jupiter_transaction_with_setup_compute_swap_cleanup_is_accepted():
    other = str(Pubkey.new_unique())
    tx = jupiter_like_tx(other, SIZE, 5_000_000)
    r = inspect(tx, buy_exp(other, venue="JUPITER_ROUTE", max_slippage_bps=1000, min_tokens_out=4_500_000,
                            max_fee_transfer_lamports=0))
    assert r.ok, r.violations
    assert r.wrap_lamports == SIZE and r.trade["min_out"] == 4_500_000


@pytest.mark.parametrize("kw,needle", [
    ({"fee_bps": 50}, "platform fee"),
    ({"slippage_bps": 5000}, "slippage"),
    ({"authority": str(Pubkey.new_unique())}, "transfer authority"),
    ({"destination": str(Pubkey.new_unique())}, "destination is not our wallet"),
    ({"extra": [p.system_transfer(WALLET, str(Pubkey.new_unique()), 1_000)]}, "transferred out as fees"),
    ({"extra": [Instruction(Pubkey.from_string("FAdo9NCw1ssek6Z6yeWzWjhLVsr8uiCwcWNUnKgzTnHe"), b"\x00", [])]}, "not allowed"),
])
def test_10_jupiter_transaction_that_does_something_else_is_rejected(kw, needle):
    other = str(Pubkey.new_unique())
    tx = jupiter_like_tx(other, SIZE, 5_000_000, **kw)
    r = inspect(tx, buy_exp(other, venue="JUPITER_ROUTE", max_slippage_bps=1000, min_tokens_out=4_500_000,
                            max_fee_transfer_lamports=0))
    assert not r.ok and any(needle in v for v in r.violations), r.violations


def _native_buy_ixs(user=WALLET, mint=MINT):
    return [p.ata_create_idempotent(WALLET, WALLET, mint, p.TOKEN),
            p.curve_buy_ix(user=user, mint=mint, creator=CREATOR, token_program=p.TOKEN, amount=1_000, max_sol_cost=105_000_000,
                           fee_recipient=FIX_FEE, buyback_fee_recipient=p.CURVE_BUYBACK_FEE_RECIPIENTS[0])]


FIX_FEE = str(Pubkey.new_unique())


def _compile(ixs, payer=KP.pubkey()):
    return VersionedTransaction.populate(MessageV0.try_compile(payer, ixs, [], Hash.new_unique()), [])


def test_10b_pump_transaction_semantics_are_checked_account_by_account():
    ok = inspect(_compile(_native_buy_ixs()), buy_exp(venue="PUMP_BONDING_CURVE", max_fee_transfer_lamports=0))
    assert ok.ok, ok.violations
    stranger = Keypair()
    cases = [
        # a second signer
        (_compile(_native_buy_ixs() + [Instruction(Pubkey.from_string(p.SYSTEM), struct.pack("<IQ", 2, 1),
                                                  [AccountMeta(stranger.pubkey(), True, True), AccountMeta(KP.pubkey(), False, True)])]),
         "signers required"),
        # SOL to someone else
        (_compile(_native_buy_ixs() + [p.system_transfer(WALLET, str(Pubkey.new_unique()), 5_000)]), "transferred out as fees"),
        # the unverified program seen in production
        (_compile(_native_buy_ixs() + [Instruction(Pubkey.from_string("FAdo9NCw1ssek6Z6yeWzWjhLVsr8uiCwcWNUnKgzTnHe"), b"\x01", [])]),
         "not allowed"),
        # a token account created for another owner
        (_compile([p.ata_create_idempotent(WALLET, str(Pubkey.new_unique()), MINT, p.TOKEN)] + _native_buy_ixs()[1:]),
         "another owner"),
    ]
    for tx, needle in cases:
        r = inspect(tx, buy_exp(venue="PUMP_BONDING_CURVE", max_fee_transfer_lamports=0))
        assert not r.ok and any(needle in v for v in r.violations), (needle, r.violations)
    # A bonding-curve venue must not be satisfied by a PumpSwap trade, and vice versa.
    wrong = inspect(_compile(_native_buy_ixs()), buy_exp(venue="PUMP_AMM", max_fee_transfer_lamports=0))
    assert not wrong.ok and any("expected the PUMP_AMM program" in v for v in wrong.violations)
    # The token account the program credits must be ours.
    ix = _native_buy_ixs()[1]
    accts = list(ix.accounts)
    accts[5] = AccountMeta(Pubkey.new_unique(), False, True)
    r = inspect(_compile([Instruction(ix.program_id, bytes(ix.data), accts)]), buy_exp(venue="PUMP_BONDING_CURVE"))
    assert not r.ok and any("not our wallet's account" in v for v in r.violations)
    # The FAdo9 layout seen in production: no Pump trade at all.
    r = inspect(_compile([Instruction(Pubkey.from_string("FAdo9NCw1ssek6Z6yeWzWjhLVsr8uiCwcWNUnKgzTnHe"), b"\x01",
                                      [AccountMeta(KP.pubkey(), True, True), AccountMeta(Pubkey.from_string(MINT), False, False)])]),
                buy_exp())
    assert not r.ok and "found 0" in " ".join(r.violations)


# --- Test 11: timed out, but it had landed ------------------------------------
async def test_11_a_buy_that_looked_timed_out_but_confirmed_is_never_rebought():
    chain = FakeChain()
    chain.add_mint(MINT)
    chain.add_curve(MINT, CREATOR)
    chain.fill = (WALLET, MINT, -SIZE, 3_000_000_000)
    chain.pending_polls = 5  # unseen for the whole confirmation window
    out, persisted = await run(executor(chain, timeout=5), buy_req(), buy_exp())
    assert out.status == "CONFIRMED" and out.fill.token_change_raw == 3_000_000_000
    # Every broadcast was the same signed transaction (same signature): one buy at most.
    assert len(chain.sent) >= 2 and len(set(chain.sent)) == 1 and persisted == [out.signature]


# --- order_inspect: the stored transaction is decoded with program labels ---
async def test_order_inspect_describes_each_instruction_with_its_program():
    from yonixalpha_core.tools.order_inspect import describe

    chain = FakeChain()
    chain.add_mint(MINT, p.TOKEN)
    chain.add_curve(MINT, CREATOR)
    chain.fill = (WALLET, MINT, -SIZE, 3_000_000_000)
    await run(executor(chain), buy_req(), buy_exp())
    lines = describe(chain.sent[-1])
    assert lines[0].startswith(f"fee payer {WALLET}")
    assert any("[Pump]" in ln and p.BUY.hex() in ln and "accounts=18" in ln for ln in lines)
    assert not any("NOT ALLOWED" in ln for ln in lines)


# --- compute-unit limit setting -------------------------------------------------------
async def test_compute_unit_limit_setting_is_applied_and_bounded():
    from solders.compute_budget import set_compute_unit_limit

    from yonixalpha_core.live_trading import LiveExecutionSettings, parse_live_settings

    assert LiveExecutionSettings().compute_unit_limit_curve == 200_000  # the working default is unchanged
    chain = FakeChain()
    chain.add_mint(MINT)
    chain.add_curve(MINT, CREATOR)
    chain.fill = (WALLET, MINT, -SIZE, 3_000_000_000)
    ex = executor(chain)
    ex.native.cu_limits = {"PUMP_BONDING_CURVE": 130_000, "PUMP_AMM": 350_000}
    out, _ = await run(ex, buy_req(), buy_exp())
    assert out.status == "CONFIRMED"
    tx = sent_tx(chain)
    datas = [bytes(ix.data) for ix in tx.message.instructions]
    assert bytes(set_compute_unit_limit(130_000).data) in datas
    built = next(s for s in out.stages if s["stage"] == "TRANSACTION_BUILT")
    assert built["compute_unit_limit"] == 130_000
    _, errors = parse_live_settings({"compute_unit_limit_curve": 50_000})
    assert errors and "between 120000 and 400000" in errors[0]
