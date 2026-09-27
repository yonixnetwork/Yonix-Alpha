import base64
import hashlib
import struct
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest

from yonixalpha_core.safety.models import Venue
from yonixalpha_core.solana import pumpfun
from yonixalpha_core.solana.codec import DEFAULT_PUBKEY, b58decode, b58encode
from yonixalpha_core.solana.flow import Trade, acceleration, realized_volatility, trade_flow
from yonixalpha_core.solana.market_data import (
    DexScreenerClient,
    JupiterClient,
    RateBudget,
    parse_dexscreener_pairs,
)
from yonixalpha_core.solana.token_safety import UnexpectedShape, parse_holders, parse_mint_account

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
MINT = "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"
USER = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"


# --- tiny Borsh encoder for building events exactly as the IDL lays them out ---

def pk(address: str) -> bytes:
    raw = b58decode(address)
    return raw.rjust(32, b"\x00")


def u64(v: int) -> bytes:
    return struct.pack("<Q", v)


def i64(v: int) -> bytes:
    return struct.pack("<q", v)


def s(text: str) -> bytes:
    raw = text.encode()
    return struct.pack("<I", len(raw)) + raw


def b(v: bool) -> bytes:
    return b"\x01" if v else b"\x00"


def trade_event(is_buy=True, sol=1_000_000_000, tokens=35_000_000_000_000, user=USER, full=True, quote_mint=None):
    body = pk(MINT) + u64(sol) + u64(tokens) + b(is_buy) + pk(user) + i64(1_790_000_000)
    body += u64(40_000_000_000) + u64(800_000_000_000_000) + u64(10_000_000_000) + u64(700_000_000_000_000)
    if full:
        body += pk(USER) + u64(95) + u64(9_500_000) + pk(USER) + u64(30) + u64(3_000_000)
        body += b(False) + u64(0) + u64(0) + u64(0) + i64(0) + s("buy") + b(False)
        # cashback, then buyback: 5000 bps is the production value (half of
        # the protocol fee goes to buyback), not a 50% charge on the trade.
        body += u64(0) + u64(0) + u64(5000) + u64(4_750_000)
        body += struct.pack("<I", 1) + pk(USER) + struct.pack("<H", 10000)  # shareholders
        body += pk(quote_mint or DEFAULT_PUBKEY)
    return pumpfun.TRADE_EVENT + body


def create_event(with_creator=True):
    body = s("Test Coin") + s("TEST") + s("https://x/meta.json") + pk(MINT) + pk(USER) + pk(USER)
    if with_creator:
        body += pk(USER) + i64(1_790_000_000)
    return pumpfun.CREATE_EVENT + body


# --- codec ---------------------------------------------------------------------

def test_base58_round_trips_known_program_ids():
    for address in (pumpfun.PUMP_PROGRAM_ID, pumpfun.PUMPSWAP_PROGRAM_ID, pumpfun.WSOL_MINT):
        assert b58encode(b58decode(address)) == address
        assert len(b58decode(address)) == 32


def test_default_pubkey_is_all_ones():
    assert DEFAULT_PUBKEY == "1" * 32


@pytest.mark.parametrize("name,const", [("event:CreateEvent", pumpfun.CREATE_EVENT), ("event:TradeEvent", pumpfun.TRADE_EVENT),
                                        ("event:CompleteEvent", pumpfun.COMPLETE_EVENT),
                                        ("event:CompletePumpAmmMigrationEvent", pumpfun.MIGRATION_EVENT),
                                        ("account:BondingCurve", pumpfun.BONDING_CURVE_ACCOUNT)])
def test_discriminators_follow_anchor_convention(name, const):
    assert hashlib.sha256(name.encode()).digest()[:8] == const


def test_event_ix_tag_is_anchor_event_tag_little_endian():
    assert pumpfun.EVENT_IX_TAG == hashlib.sha256(b"anchor:event").digest()[:8][::-1]


# --- events ----------------------------------------------------------------------

def test_full_trade_event_decodes_every_field_through_quote_mint():
    kind, f = pumpfun.decode_event(trade_event())
    assert kind == "trade"
    assert f["mint"] == MINT and f["user"] == USER and f["is_buy"] is True
    assert f["sol_amount"] == 1_000_000_000
    assert f["buyback_fee_basis_points"] == 5000
    # Protocol + creator, as the official SDK's getFee; buyback is a split of
    # the protocol fee (regression: 5125 bps made every round trip ~105%).
    assert pumpfun.total_fee_bps(f) == 95 + 30
    assert pumpfun.is_sol_quoted(f)
    assert f["shareholders"][0]["share_bps"] == 10000


def test_legacy_short_trade_event_decodes_core_fields():
    kind, f = pumpfun.decode_event(trade_event(full=False))
    assert kind == "trade" and f["virtual_token_reserves"] == 800_000_000_000_000
    assert pumpfun.total_fee_bps(f) is None
    assert pumpfun.is_sol_quoted(f)


def test_non_sol_quoted_trade_is_detected():
    _, f = pumpfun.decode_event(trade_event(quote_mint=MINT))
    assert not pumpfun.is_sol_quoted(f)


def test_truncated_required_fields_are_not_an_event():
    assert pumpfun.decode_event(trade_event()[:40]) is None


def test_self_cpi_form_decodes_the_same():
    assert pumpfun.decode_event(pumpfun.EVENT_IX_TAG + trade_event()) == pumpfun.decode_event(trade_event())


def test_log_lines_decode_only_program_data_events():
    logs = [
        "Program 6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P invoke [1]",
        "Program log: Instruction: Create",
        "Program data: " + base64.b64encode(create_event()).decode(),
        "Program data: " + base64.b64encode(b"\x00" * 20).decode(),
        "Program data: not-base64!!",
    ]
    events = pumpfun.decode_log_events(logs)
    assert [k for k, _ in events] == ["create"]
    assert events[0][1]["symbol"] == "TEST" and events[0][1]["creator"] == USER


def test_older_create_event_without_creator_still_decodes():
    kind, f = pumpfun.decode_event(create_event(with_creator=False))
    assert kind == "create" and "creator" not in f and f["bonding_curve"] == USER


def test_bonding_curve_account_decodes_and_models_price():
    data = pumpfun.BONDING_CURVE_ACCOUNT + u64(800_000_000_000_000) + u64(40_000_000_000) + u64(700_000_000_000_000)
    data += u64(10_000_000_000) + u64(1_000_000_000_000_000) + b(False) + pk(USER)
    curve = pumpfun.decode_bonding_curve(data)
    assert curve and not curve.complete and curve.creator == USER and curve.sol_quoted
    assert curve.real_liquidity_sol() == Decimal(10)
    assert curve.price_sol(6) == Decimal(40) / Decimal(800_000_000)
    model = curve.model(6, 125)
    assert model.marginal_price == curve.price_sol(6)
    assert pumpfun.decode_bonding_curve(b"\x00" * 60) is None


# --- mint / holders -----------------------------------------------------------------

def _mint(owner="TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb", **info):
    base = {"mintAuthority": None, "freezeAuthority": None, "decimals": 6, "supply": "1000000000000000", "isInitialized": True}
    base.update(info)
    return {"owner": owner, "data": {"program": "spl-token-2022", "parsed": {"type": "mint", "info": base}}}


def test_mint_with_revoked_authorities_parses_clean():
    t = parse_mint_account(_mint(), NOW, "rpc")
    assert t.mint_authority is None and t.freeze_authority is None and t.extensions == []


def test_token2022_extensions_are_extracted():
    exts = [
        {"extension": "transferFeeConfig", "state": {"transferFeeConfigAuthority": "Auth", "withdrawWithheldAuthority": None,
                                                      "withheldAmount": 0,
                                                      "olderTransferFee": {"epoch": 1, "maximumFee": 1, "transferFeeBasisPoints": 50},
                                                      "newerTransferFee": {"epoch": 2, "maximumFee": 1, "transferFeeBasisPoints": 200}}},
        {"extension": "transferHook", "state": {"authority": None, "programId": "HookProg"}},
        {"extension": "permanentDelegate", "state": {"delegate": None}},
        {"extension": "pausableConfig", "state": {"authority": "A", "paused": True}},
    ]
    t = parse_mint_account(_mint(extensions=exts), NOW, "rpc")
    assert t.transfer_fee_bps == 200 and t.transfer_fee_authority == "Auth"
    assert "transferHook" in t.extensions and t.transfer_hook_program == "HookProg"
    assert "permanentDelegate" not in t.extensions and "permanentDelegateUnset" in t.extensions
    assert t.paused is True


@pytest.mark.parametrize("bad", [None, {"owner": "Other", "data": {}}, _mint(owner="SysProg"),
                                 {"owner": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "data": {"parsed": {"type": "mint", "info": {"decimals": 6}}}}])
def test_unexpected_mint_shapes_raise_instead_of_passing(bad):
    with pytest.raises(UnexpectedShape):
        parse_mint_account(bad, NOW, "rpc")


def test_holders_exclude_pool_and_aggregate_per_owner():
    largest = [{"address": "vault", "amount": "600"}, {"address": "a1", "amount": "100"}, {"address": "a2", "amount": "50"},
               {"address": "b1", "amount": "40"}]
    owners = {"vault": "CURVE", "a1": "Alice", "a2": "Alice", "b1": "Bob"}
    h = parse_holders(largest, owners, 1000, {"CURVE"}, "Bob", NOW, "rpc")
    assert h.top1_share == Decimal("0.15") and h.top10_share == Decimal("0.19")
    assert h.creator_share == Decimal("0.04") and h.excluded_pool_accounts == 1


def test_holders_with_unknown_owner_raise():
    with pytest.raises(UnexpectedShape):
        parse_holders([{"address": "x", "amount": "1"}], {}, 10, set(), None, NOW, "rpc")


class _OwnersRpc:
    def __init__(self, values):
        self.values, self.calls = values, []

    async def call(self, method, params):
        self.calls.append((method, params))
        return {"value": self.values}


@pytest.mark.asyncio
async def test_holder_owners_read_at_confirmed_and_closed_accounts_are_dropped():
    from yonixalpha_core.solana.assembler import token_account_owners

    largest = [{"address": "a1", "amount": "100"}, {"address": "gone", "amount": "90"}, {"address": "b1", "amount": "40"}]
    rpc = _OwnersRpc([{"data": {"parsed": {"info": {"owner": "Alice"}}}}, None,
                      {"data": {"parsed": {"info": {"owner": "Bob"}}}}])
    kept, owners = await token_account_owners(rpc, largest)
    # Same commitment as getTokenLargestAccounts: at "finalized" a recent
    # buyer's account does not exist yet and the whole holder check failed.
    assert rpc.calls[0][1][1]["commitment"] == "confirmed"
    assert [a["address"] for a in kept] == ["a1", "b1"] and owners == {"a1": "Alice", "b1": "Bob"}
    h = parse_holders(kept, owners, 1000, set(), None, NOW, "rpc")
    assert h.top1_share == Decimal("0.1") and h.holders_sampled == 2


@pytest.mark.asyncio
async def test_holder_owner_that_exists_but_is_unparsed_still_raises():
    from yonixalpha_core.solana.assembler import token_account_owners

    kept, owners = await token_account_owners(_OwnersRpc([{"data": ["AAAA", "base64"]}]),
                                              [{"address": "x", "amount": "1"}])
    with pytest.raises(UnexpectedShape):
        parse_holders(kept, owners, 10, set(), None, NOW, "rpc")
    with pytest.raises(UnexpectedShape):
        await token_account_owners(_OwnersRpc([]), [{"address": "x", "amount": "1"}])


# --- flow -----------------------------------------------------------------------------

def _t(sec, trader, buy, sol=1_000_000_000, vs=40_000_000_000, vt=800_000_000_000_000):
    return Trade(NOW - timedelta(seconds=sec), trader, buy, sol, 1, vs, vt)


def test_flow_counts_unique_wallets_and_concentration():
    trades = [_t(10, "a", True), _t(20, "b", True), _t(30, "a", False), _t(400, "c", True)]
    f = trade_flow(trades, NOW, 300, creator="a", source="pump", stream_seen_at=NOW)
    assert (f.trade_count, f.unique_buyers, f.unique_sellers) == (3, 2, 1)
    assert f.creator_sold is True
    assert f.top3_wallet_volume_share == Decimal(1)
    assert f.observation.observed_at == NOW


def test_flow_never_uses_future_trades():
    trades = [_t(-5, "future", True), _t(10, "a", True)]
    f = trade_flow(trades, NOW, 300, None, "pump", NOW)
    assert f.trade_count == 1


def test_volatility_needs_enough_minutes():
    assert realized_volatility([_t(10, "a", True)], NOW, 300, 6) is None
    trades = [_t(290 - 60 * i, "a", True, vs=40_000_000_000 + i * 1_000_000_000) for i in range(5)]
    vol = realized_volatility(trades, NOW, 300, 6)
    assert vol is not None and Decimal("0") <= vol < Decimal("0.05")


def test_acceleration_ratio():
    trades = [_t(10, "a", True), _t(20, "b", True), _t(310, "c", True)]
    assert acceleration(trades, NOW, 300) == (2, 1, 2.0)


# --- Jupiter / DexScreener -------------------------------------------------------------

def _jupiter_handler(no_sell=False, error=False):
    def handler(request: httpx.Request):
        p = request.url.params
        amount = int(p["amount"])
        if error:
            return httpx.Response(500, json={"error": "boom"})
        if p["inputMint"] == pumpfun.WSOL_MINT:
            # 1 SOL buys 1,000,000 tokens minus 1% impact per SOL.
            out = int(amount * 1_000_000 / 1e9 * (1 - 0.01 * amount / 1e9) * 1e6)
        else:
            if no_sell:
                return httpx.Response(400, json={"errorCode": "COULD_NOT_FIND_ANY_ROUTE"})
            out = int(amount / 1e6 / 1_000_000 * 1e9 * 0.98)
        return httpx.Response(200, json={"outAmount": str(out), "routePlan": [{"swapInfo": {"label": "Pump.fun Amm"}}],
                                         "priceImpactPct": "0"})
    return handler


async def _jq(handler, size="1"):
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        jup = JupiterClient(client, None, RateBudget(600))
        return await jup.execution_quote(MINT, Decimal(size), Decimal("0.001"), 300, Venue.PUMPSWAP)


@pytest.mark.asyncio
async def test_execution_quote_measures_impact_against_reference():
    q, evidence = await _jq(_jupiter_handler())
    assert q.buy_route_available and q.sell_route_available
    assert Decimal("90") < q.entry_impact_bps < Decimal("110")  # ~1% modelled
    assert q.round_trip_loss_bps > q.entry_impact_bps
    assert evidence["buy"]["labels"] == ["Pump.fun Amm"]


@pytest.mark.asyncio
async def test_no_sell_route_is_reported_as_false_not_unknown():
    q, _ = await _jq(_jupiter_handler(no_sell=True))
    assert q.buy_route_available is True and q.sell_route_available is False


@pytest.mark.asyncio
async def test_transport_or_server_errors_are_unknown_not_unavailable():
    q, _ = await _jq(_jupiter_handler(error=True))
    assert q.buy_route_available is None and q.sell_route_available is None


@pytest.mark.asyncio
async def test_rate_budget_refuses_when_exhausted():
    budget = RateBudget(1)
    assert await budget.acquire(timeout=0.1)
    assert not await budget.acquire(timeout=0.1)


def test_dexscreener_picks_sol_pair_with_most_liquidity():
    body = {"pairs": [
        {"chainId": "solana", "dexId": "raydium", "pairAddress": "P1", "baseToken": {"address": MINT},
         "quoteToken": {"address": pumpfun.WSOL_MINT}, "priceNative": "0.00001", "liquidity": {"usd": 1000, "base": 5, "quote": 3}},
        {"chainId": "solana", "dexId": "pumpswap", "pairAddress": "P2", "baseToken": {"address": MINT},
         "quoteToken": {"address": pumpfun.WSOL_MINT}, "priceNative": "0.00002", "pairCreatedAt": 1_790_000_000_000,
         "liquidity": {"usd": 9000, "base": 50, "quote": 30}, "txns": {"m5": {"buys": 12, "sells": 3}}},
        {"chainId": "ethereum", "baseToken": {"address": MINT}},
    ]}
    pool = parse_dexscreener_pairs(body, MINT, NOW)
    assert pool.pair_address == "P2" and pool.dex_id == "pumpswap"
    assert pool.liquidity_sol == Decimal(30) and pool.buys_m5 == 12
    assert pool.pair_created_at.year >= 2026


def test_dexscreener_without_a_pool_returns_none():
    assert parse_dexscreener_pairs({"pairs": None}, MINT, NOW) is None
    assert parse_dexscreener_pairs({"pairs": []}, MINT, NOW) is None


@pytest.mark.asyncio
async def test_dexscreener_http_error_is_reported():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(429))) as client:
        pool, err = await DexScreenerClient(client, RateBudget(60)).pool(MINT)
    assert pool is None and err == "HTTP 429"
