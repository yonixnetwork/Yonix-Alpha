"""Launch-window coordination (master §11): the detections on constructed
launches, Pons V2 exemption lists decoded from real ABI-encoded launch
calldata, the on-chain snipe-tax confirmation, the entry effect and the
operator approval, and first-funder lookups against mocked explorers.
Proves the logic only; real-chain behaviour is NOT VERIFIED here."""
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://yonixalpha:yonixalpha_test_pw@localhost:5432/yonixalpha_test")

import httpx  # noqa: E402
import pytest_asyncio  # noqa: E402
from eth_abi import encode  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from yonixalpha_core import launch_coordination as lc  # noqa: E402
from yonixalpha_core.chains.evm.abi import ZERO_ADDRESS, selector  # noqa: E402
from yonixalpha_core.chains.evm.pons import PonsV2  # noqa: E402
from yonixalpha_core.db import models  # noqa: F401,E402
from yonixalpha_core.db.base import Base, make_session_factory  # noqa: E402
from yonixalpha_core.db.models import EvmToken, EvmTrade, EvmWalletFunder  # noqa: E402
from yonixalpha_core.testing.evm_node import Node, enc, rpc_for  # noqa: E402

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
CREATOR = "0x" + "c" * 40
TOKEN = "0x1111111111111111111111111111111111111111"
CURVE = "0x3333333333333333333333333333333333333333"
SUPPLY = 10 ** 27


def w(i: int) -> str:
    return f"0x{i:040x}"


def launch(**kw):
    return {"launchpad": "fourmeme", "launch_seen": True, "created_at": T0, "created_block": 100, "creator": CREATOR, **kw}


def buy(holder, s, tokens=10 ** 24, block=None):
    return lc.Trade(holder, True, tokens, 10 ** 16, block if block is not None else 100 + s, T0 + timedelta(seconds=s))


def sell(holder, s, tokens=10 ** 24):
    return lc.Trade(holder, False, tokens, 10 ** 16, 100 + s, T0 + timedelta(seconds=s))


def codes(res):
    return {f["code"] for f in res["findings"]}


def test_an_organic_launch_detects_nothing_and_takes_no_action():
    trades = [buy(w(i), 10 + i * 9) for i in range(1, 6)]  # five buyers, 9 s apart, 0.1 % each
    res = lc.analyse(launch(), trades, {"supply": SUPPLY}, lc.CoordinationConfig())
    assert res["status"] == lc.NOT_DETECTED and res["action"] == lc.NONE and not res["findings"]
    st = {c["check"]: c["status"] for c in res["checks"]}
    assert st["DECLARED_EXEMPTIONS"] == lc.NOT_APPLICABLE and st["COMMON_FUNDER"] == lc.NOT_CONFIGURED
    assert res["window"]["buyers"] == 5 and "not a safety verdict" in res["note"]


def test_a_bundled_launch_is_flagged_on_every_signal():
    bundle = [w(i) for i in range(1, 6)]
    trades = [buy(CREATOR, 0, 5 * 10 ** 25)]  # the creator's own opening buy (5 %)
    trades += [buy(b, 0, 6 * 10 ** 25) for b in bundle]  # five wallets in the launch block, 6 % each
    trades += [sell(b, 300 + i * 5, 6 * 10 ** 25) for i, b in enumerate(bundle[:3])]  # three dump within 15 s
    facts = {"supply": SUPPLY, "funding_status": "CHECKED",
             "funders": {**{b: {"status": "FOUND", "funder": "0x" + "f" * 40} for b in bundle[:3]},
                         bundle[3]: {"status": "FOUND", "funder": CREATOR}},
             "nonces": {b: {"nonce": 1} for b in bundle}}
    res = lc.analyse(launch(), trades, facts, lc.CoordinationConfig())
    assert codes(res) >= {"LAUNCH_BLOCK_BUNDLE", "NEAR_SIMULTANEOUS_BUYERS", "CREATOR_BOUGHT", "CREATOR_FUNDED_BUYERS",
                          "COMMON_FUNDER", "FRESH_WALLET_CLUSTER", "COORDINATED_EXIT"}
    assert res["action"] == lc.NO_TRADE and res["status"] == lc.DETECTED
    by = {c["check"]: c for c in res["checks"]}
    assert by["LAUNCH_BLOCK_BUNDLE"]["value"] == 5 and CREATOR not in by["LAUNCH_BLOCK_BUNDLE"]["wallets"]
    assert by["COMMON_FUNDER"]["value"] == 3 and by["CREATOR_FUNDED_BUYERS"]["wallets"] == [bundle[3]]
    assert by["WINDOW_SUPPLY_CONCENTRATION"]["value"] == 0.17  # 5 % + 2 x 6 % still held
    roles = {r["wallet"]: r["roles"] for r in res["wallets"]}
    assert roles[CREATOR] == ["CREATOR_LINKED"] and "CREATOR_FUNDED" in roles[bundle[3]]


def test_supply_concentration_and_a_launch_this_system_did_not_see():
    res = lc.analyse(launch(), [buy(w(1), 20, 3 * 10 ** 26)], {"supply": SUPPLY}, lc.CoordinationConfig())
    assert codes(res) == {"WINDOW_SUPPLY_CONCENTRATION", "SINGLE_WALLET_CONCENTRATION"}  # 30 % in one wallet
    unseen = lc.analyse(launch(launch_seen=False), [], {}, lc.CoordinationConfig())
    assert codes(unseen) == {"WINDOW_NOT_OBSERVED"} and unseen["action"] == lc.NO_TRADE
    no_supply = lc.analyse(launch(), [buy(w(1), 20)], {"supply": None, "unavailable": ["totalSupply(): reverted"]},
                           lc.CoordinationConfig())
    assert "COORDINATION_DATA_UNAVAILABLE" in codes(no_supply) and no_supply["action"] == lc.NO_TRADE
    assert {c["status"] for c in no_supply["checks"] if c["check"].endswith("CONCENTRATION")} == {lc.UNKNOWN}


def _launch_calldata(sig: str, exemptions: list[str], recipient: str | None = None) -> str:
    params = ("Moon", "MOON", "ipfs://x", "d", ("", "", "", "", ""), CREATOR, 100, True, b"\0" * 32, b"\1" * 32)
    if sig.startswith("launchAndBuy"):
        args = [params, 1, ZERO_ADDRESS, 10 ** 16, 1, recipient, exemptions]
    elif sig.startswith("launchTokenFor"):
        args = [params, 1, ZERO_ADDRESS, CREATOR, exemptions]
    elif exemptions is None:
        args = [params, 1, ZERO_ADDRESS]
    else:
        args = [params, 1, ZERO_ADDRESS, exemptions]
    full = sig.replace("P", lc.PONS_V2_TOKEN_PARAMS)
    types = lc.PONS_V2_LAUNCH_CALLS["0x" + selector(full).hex()][1]
    return "0x" + (selector(full) + encode(types, args)).hex()


def test_pons_v2_exemption_lists_are_read_from_every_launch_entrypoint():
    ex = [w(7), w(8)]
    r = lc.decode_pons_v2_launch(_launch_calldata("launchToken(P,uint256,address,address[])", ex))
    assert r["status"] == "READ" and r["declared"] == ex and r["via"].endswith("(exemption list)")
    assert r["creator_fee_recipient_at_launch"] == CREATOR and r["creator_tax_bps"] == 100
    r = lc.decode_pons_v2_launch(_launch_calldata("launchTokenFor(P,uint256,address,address,address[])", ex))
    assert r["declared"] == ex and "forwarder" in r["via"]
    r = lc.decode_pons_v2_launch(_launch_calldata("launchAndBuy(P,uint256,address,uint256,uint256,address,address[])",
                                                  ex, recipient=w(9)))
    assert r["declared"] == ex and r["opening_recipient"] == w(9) and r["via"] == "launchAndBuy router"
    r = lc.decode_pons_v2_launch(_launch_calldata("launchToken(P,uint256,address)", None))
    assert r["status"] == "READ" and r["declared"] == []
    params = ("Moon", "MOON", "ipfs://x", "d", ("", "", "", "", ""), CREATOR, 100, True, b"\0" * 32, b"\1" * 32)
    wsig = f"launch({lc.PONS_V2_TOKEN_PARAMS},address)"  # 0xa3a3ee69, seen on real launches
    wrapped = lc.decode_pons_v2_launch("0x" + (selector(wsig) + encode([lc.PONS_V2_TOKEN_PARAMS, "address"],
                                                                        [params, w(5)])).hex())
    assert wrapped["selector"] == "0xa3a3ee69" and wrapped["status"] == lc.UNKNOWN and "wrapper" in wrapped["via"]
    unknown = lc.decode_pons_v2_launch("0xdeadbeef" + "00" * 64)
    assert unknown["status"] == lc.UNKNOWN and unknown["selector"] == "0xdeadbeef"


def test_privileged_buyers_from_the_declared_list_and_from_the_curve():
    ex = {"status": "READ", "via": "factory.launchToken (exemption list)", "declared": [w(1), w(9)]}
    snipe = {w(2): {"exempt": True}, w(3): {"exempt": False}}
    trades = [buy(w(1), 5), buy(w(2), 6), buy(w(3), 7)]
    res = lc.analyse(launch(launchpad="pons_v2"), trades, {"supply": SUPPLY, "exemptions": ex, "snipe": snipe},
                     lc.CoordinationConfig())
    by = {c["check"]: c for c in res["checks"]}
    assert by["PRIVILEGED_BUYERS"]["wallets"] == [w(1), w(2)] and by["DECLARED_EXEMPTIONS"]["value"] == 2
    assert res["action"] == lc.NO_TRADE and {"PRIVILEGED_BUYERS", "DECLARED_EXEMPTIONS"} <= codes(res)
    unread = lc.analyse(launch(launchpad="pons_v2"), trades, {"supply": SUPPLY, "snipe": {w(1): {"exempt": None}},
                                                              "exemptions": {"status": lc.UNKNOWN, "detail": "x"}},
                        lc.CoordinationConfig())
    st = {c["check"]: c["status"] for c in unread["checks"]}
    assert st["DECLARED_EXEMPTIONS"] == lc.UNKNOWN and st["PRIVILEGED_BUYERS"] == lc.UNKNOWN


def test_tokens_outside_the_curve_without_a_recorded_buy_are_abnormal():
    own = {"status": "READ", "supply": SUPPLY, "curve_balance": SUPPLY - 10 ** 25 - 5 * 10 ** 25,
           "net_curve_buys": 10 ** 25, "block": 120}
    res = lc.analyse(launch(launchpad="pons_v2"), [buy(w(1), 5, 10 ** 25)],
                     {"supply": SUPPLY, "ownership": own, "exemptions": {"status": "READ", "declared": []}},
                     lc.CoordinationConfig())
    by = {c["check"]: c for c in res["checks"]}
    assert by["ABNORMAL_INITIAL_OWNERSHIP"]["status"] == lc.FLAG and by["ABNORMAL_INITIAL_OWNERSHIP"]["value"] == 0.05
    own["curve_balance"] = SUPPLY - 10 ** 25
    ok = lc.analyse(launch(launchpad="pons_v2"), [buy(w(1), 5, 10 ** 25)],
                    {"supply": SUPPLY, "ownership": own, "exemptions": {"status": "READ", "declared": []}},
                    lc.CoordinationConfig())
    assert "ABNORMAL_INITIAL_OWNERSHIP" not in codes(ok)


def test_entry_effect_and_operator_approval():
    cfg = lc.CoordinationConfig(actions={**lc.DEFAULT_ACTIONS, "LAUNCH_BLOCK_BUNDLE": lc.MANUAL})
    trades = [buy(w(i), 0) for i in range(1, 4)]
    res = lc.analyse(launch(), trades, {"supply": SUPPLY}, cfg)
    assert res["action"] == lc.MANUAL
    now, age = T0 + timedelta(minutes=1), timedelta(minutes=5)
    fx = lc.entry_effect(res, T0, None, cfg, now, age)
    assert fx.blocker == "COORDINATION_MANUAL_APPROVAL"
    ok = {"by": "op", "fingerprint": res["fingerprint"], "expires_at": (now + timedelta(minutes=30)).isoformat()}
    fx = lc.entry_effect(res, T0, ok, cfg, now, age)
    assert fx.blocker is None and fx.size_factor == Decimal(1)  # approved; no REDUCE_SIZE finding
    assert lc.entry_effect(res, T0, {**ok, "fingerprint": "other"}, cfg, now, age).blocker == "COORDINATION_MANUAL_APPROVAL"
    expired = {**ok, "expires_at": (now - timedelta(seconds=1)).isoformat()}
    assert lc.entry_effect(res, T0, expired, cfg, now, age).blocker == "COORDINATION_MANUAL_APPROVAL"
    assert lc.entry_effect(res, T0 - timedelta(minutes=10), ok, cfg, now, age).blocker == "COORDINATION_NOT_CHECKED"
    assert lc.entry_effect(None, None, None, lc.CoordinationConfig(enabled=False), now, age).blocker is None
    hard = lc.analyse(launch(), trades, {"supply": SUPPLY}, lc.CoordinationConfig())
    assert lc.entry_effect(hard, T0, {**ok, "fingerprint": hard["fingerprint"]}, lc.CoordinationConfig(), now,
                           age).blocker == "LAUNCH_COORDINATION"  # NO_TRADE is never approvable
    soft = lc.analyse(launch(), [buy(CREATOR, 3), buy(w(1), 30)], {"supply": SUPPLY}, lc.CoordinationConfig())
    fx = lc.entry_effect(soft, T0, None, lc.CoordinationConfig(), now, age)
    assert soft["action"] == lc.REDUCE and fx.blocker is None and fx.size_factor == Decimal("0.5")


def test_settings_are_validated_and_actions_merge():
    cfg, errors = lc.parse_config({"window_seconds": 30, "actions": {"CREATOR_BOUGHT": "NO_TRADE"},
                                   "ignore_funders": ["0x" + "A" * 40]})
    assert not errors and cfg.window_seconds == 30 and cfg.action("CREATOR_BOUGHT") == lc.NO_TRADE
    assert cfg.action("COMMON_FUNDER") == lc.NO_TRADE and cfg.ignore_funders == ("0x" + "a" * 40,)
    _, errors = lc.parse_config({"actions": {"CREATOR_BOUGHT": "IGNORE", "NOPE": "NONE"}, "reduce_size_factor": 2,
                                 "enabled": "yes", "bogus": 1})
    assert len(errors) == 5


async def test_first_funding_from_blockscout_and_etherscan():
    funder, disperse, caller = w(0xF1), w(0xD1), w(0xCA)

    def blockscout(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path.endswith("/internal-transactions"):
            return httpx.Response(200, json={"items": [{"from": {"hash": disperse}, "value": "5",
                                                        "timestamp": "2026-09-30T10:00:00Z", "transaction_hash": "0xaa"}],
                                             "next_page_params": None})
        if path.endswith("/transactions"):
            return httpx.Response(200, json={"items": [{"from": {"hash": funder}, "value": "7", "hash": "0xbb",
                                                        "timestamp": "2026-09-30T11:00:00Z"}], "next_page_params": None})
        if path.endswith("/transactions/0xaa"):
            return httpx.Response(200, json={"from": {"hash": caller}})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(blockscout)) as c:
        r = await lc.first_funding(c, "robinhood", w(1), None)
    assert r["status"] == "FOUND" and r["funder"] == caller and r["via_contract"]  # earliest: the disperse call

    def etherscan(req: httpx.Request) -> httpx.Response:
        q = req.url.params
        assert q["apikey"] == "k" and q["chainid"] == "56"
        if q["action"] == "txlist":
            return httpx.Response(200, json={"status": "1", "result": [
                {"from": funder, "to": w(1), "value": "9", "timeStamp": "1790000000", "hash": "0xcc"}]})
        return httpx.Response(200, json={"status": "0", "message": "No transactions found", "result": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(etherscan)) as c:
        r = await lc.first_funding(c, "bsc", w(1), "k")
        assert r["status"] == "FOUND" and r["funder"] == funder
        assert (await lc.first_funding(c, "bsc", w(1), None))["status"] == lc.NOT_CONFIGURED
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(503))) as c:
        assert (await lc.first_funding(c, "robinhood", w(1), None))["status"] == "UNAVAILABLE"


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(os.environ["DATABASE_URL"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with make_session_factory(engine)() as session:
        yield session
    await engine.dispose()


async def test_assess_a_pons_v2_launch_from_stored_trades_and_the_chain(db):
    node = Node(4663)
    lp = PonsV2(rpc_for(node, "robinhood"))
    router, bundler = "0xe33e9e479df8802cb0866d5d05258bec4cf62948", w(0xB0B)
    node.txs["0x" + "ab" * 32] = {"from": CREATOR, "to": router, "input": _launch_calldata(
        "launchAndBuy(P,uint256,address,uint256,uint256,address,address[])", [w(1), w(2)], recipient=CREATOR)}
    node.on(TOKEN, "totalSupply()", enc(["uint256"], [SUPPLY]))
    node.on(TOKEN, "balanceOf(address)", enc(["uint256"], [SUPPLY - 4 * 10 ** 25]))
    node.on(lp.factory, "getLaunchedToken(address)", enc(
        ["(address,address,address,address,address,uint256,uint24,int24,uint16,bool,uint8,uint256,uint256,uint256,bool)"],
        [(TOKEN, CURVE, CREATOR, CREATOR, ZERO_ADDRESS, 4 * 10 ** 18, 10000, 200, 100, False, 0, 0, 0, 0, True)]))

    def snipe_tax(p):  # w(3) is exempt on chain although no list named it; everyone else pays inside the window
        who = "0x" + p[0]["data"][-40:]
        return enc(["uint256"], [0 if who == w(3) else 9000])

    node.on(CURVE, "currentSnipeTaxBps(address)", snipe_tax)
    for i in (1, 2, 3):
        node.nonces[w(i)] = 1
    db.add(EvmToken(chain="robinhood", token=TOKEN, launchpad="pons_v2", creator=CREATOR, created_at=T0,
                    created_block=100, created_tx="0x" + "ab" * 32, venue={"curve": CURVE}, stats={}, stage="CURVE",
                    category="FRESH", extra={"launch_seen": True}))
    for n, (holder, s, blk, tokens) in enumerate([(CREATOR, 0, 100, 10 ** 25), (w(1), 0, 100, 10 ** 25),
                                                  (w(2), 1, 101, 10 ** 25), (w(3), 2, 102, 10 ** 25)]):
        db.add(EvmTrade(event_id=f"e{n}", chain="robinhood", launchpad="pons_v2", token=TOKEN, trader=router if n == 0
                        else holder, is_buy=True, token_amount=Decimal(tokens), quote_amount=Decimal(10 ** 16),
                        block=blk, at=T0 + timedelta(seconds=s), extra={"recipient": holder}))
    db.add(EvmWalletFunder(chain="robinhood", wallet=w(1), status="FOUND", funder=bundler, checked_at=T0))
    db.add(EvmWalletFunder(chain="robinhood", wallet=w(2), status="FOUND", funder=bundler, checked_at=T0))
    db.add(EvmWalletFunder(chain="robinhood", wallet=w(3), status="NOT_FOUND", checked_at=T0))
    db.add(EvmWalletFunder(chain="robinhood", wallet=CREATOR, status="NOT_FOUND", checked_at=T0))
    await db.commit()
    row = await db.get(EvmToken, ("robinhood", TOKEN))
    res = await lc.assess(db, lp, row, T0 + timedelta(minutes=3), lc.CoordinationConfig())
    by = {c["check"]: c for c in res["checks"]}
    assert res["facts"]["exemptions"]["via"] == "launchAndBuy router"
    assert by["PRIVILEGED_BUYERS"]["wallets"] == [w(1), w(2), w(3)]  # two declared, one confirmed on chain
    assert by["CREATOR_BOUGHT"]["wallets"] == [CREATOR.lower()]  # through the router: the recipient is the holder
    assert by["COMMON_FUNDER"]["wallets"] == [w(1), w(2)] and by["ABNORMAL_INITIAL_OWNERSHIP"]["status"] == lc.OK
    assert res["facts"]["snipe"][w(3)]["exempt"] is True and res["facts"]["nonces"][w(1)]["nonce"] == 1
    assert res["action"] == lc.NO_TRADE
    roles = {r["wallet"]: r["roles"] for r in res["wallets"]}
    assert roles[w(3)] == ["CONFIRMED_EXEMPT", "FRESH_WALLET"] and "DECLARED_EXEMPT" in roles[w(1)]
