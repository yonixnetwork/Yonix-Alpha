"""The native Pump.fun / PumpSwap instructions are byte-identical to what the
OFFICIAL SDKs build (@pump-fun/pump-sdk 2.0.0, @pump-fun/pump-swap-sdk
1.20.0) for the same inputs: program id, every account in order with its
signer/writable flags, and the instruction data. Fixtures:
tests/fixtures/pump_sdk/generate.cjs (offline; see its header)."""

import json
from pathlib import Path

import pytest

from yonixalpha_core.solana import pump_tx as p

FIX = json.loads((Path(__file__).parent / "fixtures" / "pump_sdk" / "fixtures.json").read_text())
IN = FIX["inputs"]


def _ix(i) -> dict:
    return {"program": str(i.program_id), "keys": [[str(a.pubkey), a.is_signer, a.is_writable] for a in i.accounts],
            "data": bytes(i.data).hex()}


def _same(built, expected):
    assert built["program"] == expected["program"]
    for n, (b, e) in enumerate(zip(built["keys"], expected["keys"])):
        assert b == e, f"account {n}: built {b}, SDK {e}"
    assert len(built["keys"]) == len(expected["keys"])
    assert built["data"] == expected["data"]


@pytest.mark.parametrize("name,tp", [("token", p.TOKEN), ("token2022", p.TOKEN_2022)])
def test_bonding_curve_buy_matches_the_official_sdk(name, tp):
    built = p.curve_buy_ix(user=IN["user"], mint=IN["mint"], creator=IN["creator"], token_program=tp, amount=123456789000,
                           max_sol_cost=250000000, fee_recipient=IN["fee_recipient"],
                           buyback_fee_recipient=IN["buyback_fee_recipient"])
    _same(_ix(built), FIX[f"curve_buy_{name}"])
    assert len(built.accounts) == 18  # docs/BREAKING_FEE_RECIPIENT.md


@pytest.mark.parametrize("name,tp", [("token", p.TOKEN), ("token2022", p.TOKEN_2022)])
@pytest.mark.parametrize("cashback", [False, True])
def test_bonding_curve_sell_matches_the_official_sdk(name, tp, cashback):
    built = p.curve_sell_ix(user=IN["user"], mint=IN["mint"], creator=IN["creator"], token_program=tp, amount=123456789000,
                            min_sol_output=200000000, fee_recipient=IN["fee_recipient"],
                            buyback_fee_recipient=IN["buyback_fee_recipient"], cashback=cashback)
    _same(_ix(built), FIX[f"curve_sell_{name}{'_cashback' if cashback else ''}"])
    assert len(built.accounts) == (17 if cashback else 16)


CASES = [("token", p.TOKEN, False, 300), ("token2022", p.TOKEN_2022, False, 300), ("token_cashback", p.TOKEN, True, 300),
         ("token_short_pool", p.TOKEN, False, 243)]


def _pool(cashback, size):
    return p.AmmPoolInfo(pool=p.pda(p.PUMP_AMM, b"pool", (0).to_bytes(2, "little"),
                                    bytes(p._pk(p.pda(p.PUMP, b"pool-authority", bytes(p._pk(IN["mint"]))))),
                                    bytes(p._pk(IN["mint"])), bytes(p._pk(p.WSOL))),
                         base_mint=IN["mint"], quote_mint=p.WSOL, pool_base_token_account=IN["pool_base"],
                         pool_quote_token_account=IN["pool_quote"], coin_creator=IN["amm_coin_creator"],
                         is_mayhem_mode=False, is_cashback_coin=cashback, account_size=size)


@pytest.mark.parametrize("name,tp,cashback,size", CASES)
def test_pumpswap_buy_matches_the_official_sdk(name, tp, cashback, size):
    built = p.amm_buy_ixs(user=IN["user"], pool=_pool(cashback, size), base_token_program=tp, base_out=5000000000,
                          max_quote_in=260000000, protocol_fee_recipient=IN["amm_protocol_fee_recipient"],
                          buyback_fee_recipient=IN["amm_buyback_fee_recipient"])
    expected = FIX[f"amm_buy_{name}"]
    assert len(built) == len(expected)
    for b, e in zip(built, expected):
        _same(_ix(b), e)


@pytest.mark.parametrize("name,tp,cashback,size", CASES)
def test_pumpswap_sell_matches_the_official_sdk(name, tp, cashback, size):
    built = p.amm_sell_ixs(user=IN["user"], pool=_pool(cashback, size), base_token_program=tp, base_in=5000000000,
                           min_quote_out=190000000, protocol_fee_recipient=IN["amm_protocol_fee_recipient"],
                           buyback_fee_recipient=IN["amm_buyback_fee_recipient"])
    expected = FIX[f"amm_sell_{name}"]
    assert len(built) == len(expected)
    for b, e in zip(built, expected):
        _same(_ix(b), e)


def test_quote_math_matches_the_sdk_formulas():
    # getBuyTokenAmountFromSolAmount: ((sol - 1) * 1e4 / (fee + 1e4)) * vT / (vS + that), capped at real tokens.
    assert p.curve_buy_tokens(1_000_000_001, 1_073_000_000_000_000, 30_000_000_000, 793_100_000_000_000, 100) == \
        (1_000_000_000 * 10_000 // 10_100) * 1_073_000_000_000_000 // (30_000_000_000 + 1_000_000_000 * 10_000 // 10_100)
    assert p.curve_buy_tokens(10**18, 10**15, 10**9, 5, 0) == 5  # never more than the real reserves
    gross = 10**9 * 30_000_000_000 // (1_073_000_000_000_000 + 10**9)
    assert p.curve_sell_sol(10**9, 1_073_000_000_000_000, 30_000_000_000, 125) == gross - (gross * 125 + 9_999) // 10_000


def test_global_and_global_config_decode_what_anchor_encodes_from_the_official_idl():
    g = FIX["global_account"]
    dec = p.decode_pump_global(bytes.fromhex(g["hex"]))
    assert dec.fee_recipient == g["fee_recipient"] and list(dec.fee_recipients) == g["fee_recipients"]
    assert dec.reserved_fee_recipient == g["reserved_fee_recipient"]
    assert list(dec.reserved_fee_recipients) == g["reserved_fee_recipients"]
    assert list(dec.buyback_fee_recipients) == g["buyback_fee_recipients"]
    assert (dec.fee_basis_points, dec.creator_fee_basis_points) == (95, 30)
    assert dec.fee_recipient_for(False) in [g["fee_recipient"], *g["fee_recipients"]]
    assert dec.fee_recipient_for(True) in [g["reserved_fee_recipient"], *g["reserved_fee_recipients"]]

    c = FIX["global_config_account"]
    cfg = p.decode_amm_global_config(bytes.fromhex(c["hex"]))
    assert list(cfg.protocol_fee_recipients) == c["protocol_fee_recipients"]
    assert list(cfg.buyback_fee_recipients) == c["buyback_fee_recipients"]
    assert cfg.reserved_fee_recipient == c["reserved_fee_recipient"]
    assert (cfg.lp_fee_basis_points, cfg.protocol_fee_basis_points, cfg.coin_creator_fee_basis_points) == (20, 5, 5)
    assert cfg.protocol_fee_recipient_for(False) in c["protocol_fee_recipients"]
    assert cfg.buyback_fee_recipient() in c["buyback_fee_recipients"]
    with pytest.raises(Exception):
        p.decode_pump_global(bytes.fromhex(c["hex"]))  # wrong discriminator
