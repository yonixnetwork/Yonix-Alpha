"""EVM ML knowledge (master §41, §75) and wallet behaviour labels (§36-37)."""

from datetime import datetime, timedelta, timezone

import pytest

from yonixalpha_core.db.models import EvmMlSample, WalletProfile, WalletTradeLabel
from yonixalpha_core.ml import evm_samples, wallet_labels

pytestmark = pytest.mark.asyncio
NOW = datetime.now(timezone.utc).replace(microsecond=0)
W = "0x" + "a" * 40


def _sample(token: str, final: str, ml: str, labels: dict, *, state: str = "EXPIRED", traded: bool = False,
            executable: float | None = None, scored: dict | None = None) -> EvmMlSample:
    return EvmMlSample(chain="bsc", token=token, category="FRESH", launchpad="fourmeme", decided_at=NOW - timedelta(hours=2),
                       features={"buyers": 3.0}, labels=labels, feature_version=evm_samples.FEATURE_VERSION,
                       label_version=evm_samples.LABEL_VERSION,
                       verdicts={"deterministic": final, "risk": final, "final": final, "ml": ml},
                       observation_state=state, traded=traded, executable_return_pct=executable, ml_shadow=scored,
                       created_at=NOW)


WIN = {"upside_50": True, "upside_100": True, "fast_dump": False, "migrate_60m": False, "return_60m_pct": 120.0,
       "max_drawdown_pct": -10.0}
DUMP = {"upside_50": False, "upside_100": False, "fast_dump": True, "migrate_60m": False, "return_60m_pct": -80.0,
        "max_drawdown_pct": -85.0}


async def test_evm_knowledge_counts_compares_and_keeps_ml_at_zero(app, client, auth_headers):
    empty = (await client.get("/api/ml/evm", headers=auth_headers)).json()
    assert empty["samples"]["evm_total"] == 0 and empty["models"] == []
    assert empty["contribution"] == {"percent": 0, "status": "SHADOW", "why": empty["contribution"]["why"]}
    async with app.state.db_session_factory() as s:
        s.add(_sample("0x" + "1" * 40, "BUY", "NOT_AVAILABLE", DUMP, state="ENTERED", traded=True, executable=-60.0))
        s.add(_sample("0x" + "2" * 40, "REJECT", "BUY", WIN, state="REJECTED",
                      scored={"out_of_sample": True, "scores": {}}))
        s.add(_sample("0x" + "3" * 40, "WAIT", "IN_SAMPLE", WIN, scored={"out_of_sample": False, "scores": {}}))
        s.add(_sample("0x" + "4" * 40, "WAIT", "NOT_AVAILABLE", {"unknown": "no snapshot at T+5"}))
        await s.commit()
    # served from the 60 s review cache (audit 2026-10-07): still the earlier answer
    assert (await client.get("/api/ml/evm", headers=auth_headers)).json()["samples"]["evm_total"] == 0
    from app.api import review_cache
    for key in await app.state.redis.keys(review_cache.PREFIX + "*"):
        await app.state.redis.delete(key)  # the cache expired
    r = (await client.get("/api/ml/evm", headers=auth_headers)).json()
    assert r["cached_at"]
    n = r["samples"]
    assert (n["evm_total"], n["evm_labelled"], n["traded"], n["rejected"], n["expired_no_entry"]) == (4, 3, 1, 1, 2)
    assert (n["missed_winners"], n["wins"], n["losses"], n["scored"], n["scored_out_of_sample"]) == (2, 0, 1, 2, 1)
    c = r["comparison"]
    assert c["final"]["BUY"]["fast_dump_rate"] == 1.0 and c["final"]["BUY"]["executable"] == {"n": 1, "mean_pct": -60.0}
    assert c["final"]["WAIT"] == {**c["final"]["WAIT"], "n": 2, "labelled": 1}
    assert c["final_missed_winners"] == 2 and c["final_bad_entries"] == 1
    # an in-sample ML verdict is shown as such, never counted as an ML BUY
    assert c["final_vs_ml"] == {"final REJECT / ml BUY": 1, "final WAIT / ml IN_SAMPLE": 1}
    assert r["contribution"]["percent"] == 0 and r["definitions"]["decision_point"] == "T+5 min of each observation"
    assert r["exits"]["checkpoints"] == 0 and "never exits a position" in r["exits"]["note"]
    import uuid

    from yonixalpha_core.db.models import EvmExitSample
    async with app.state.db_session_factory() as s:
        for i, (final, fell) in enumerate((("SELL", True), ("HOLD", False), ("HOLD", None))):
            s.add(EvmExitSample(position_id=uuid.uuid4(), chain="bsc", token="0x" + "1" * 40, engine="evm_bsc",
                                at=NOW - timedelta(minutes=30 + i), price=1.0, features={},
                                verdicts={"deterministic": "HOLD", "risk": final, "final": final, "ml": "NOT_AVAILABLE"},
                                exit_reasons=["stop_loss"] if final == "SELL" else [],
                                labels=None if fell is None else {"forward_return_pct": -20.0 if fell else 4.0,
                                                                  "fell_10": fell, "rose_10": False, "trades_after": 2},
                                feature_version="x", label_version="x", created_at=NOW))
        await s.commit()
    from app.api import review_cache
    for key in await app.state.redis.keys(review_cache.PREFIX + "*"):
        await app.state.redis.delete(key)  # the 60 s review cache expired
    x = (await client.get("/api/ml/evm", headers=auth_headers)).json()["exits"]
    assert x["checkpoints"] == 3 and x["labelled"] == 2
    assert x["final"]["SELL"]["fell_10_rate"] == 1.0 and x["final"]["HOLD"] == {**x["final"]["HOLD"], "n": 2, "labelled": 1}
    old = (await client.get("/api/ml/evm?days=1", headers=auth_headers)).json()
    assert old["samples"]["evm_total"] == 4
    assert (await client.get("/api/ml/evm")).status_code == 401


async def test_wallet_behaviour_summary_on_profiles_and_detail(app, client, auth_headers):
    async with app.state.db_session_factory() as s:
        s.add(WalletProfile(chain="bsc", wallet=W, metrics={}, labels=[], source="evm_trades", trades=4, tokens=3, last_seen=NOW))
        for i, labels in enumerate(([wallet_labels.SUCCESSFUL], [wallet_labels.FAILED, wallet_labels.LATE_ENTRY])):
            s.add(WalletTradeLabel(chain="bsc", wallet=W, token="0x" + str(i) * 40, kind="EPISODE", launchpad="fourmeme",
                                   entry_at=NOW - timedelta(days=1, hours=i), labels=labels,
                                   outcome={"entry_multiple": 1.2 + i * 3, "max_return_60m_pct": 80.0 - i * 90,
                                            "min_return_60m_pct": -5.0 - i * 50},
                                   features={}, feature_version=wallet_labels.FEATURE_VERSION,
                                   label_version=wallet_labels.LABEL_VERSION, created_at=NOW))
        s.add(WalletTradeLabel(chain="bsc", wallet=W, token="0x" + "9" * 40, kind="MISSED", launchpad="fourmeme",
                               entry_at=NOW - timedelta(days=2), labels=[wallet_labels.MISSED], outcome={}, features={},
                               feature_version=wallet_labels.FEATURE_VERSION, label_version=wallet_labels.LABEL_VERSION,
                               created_at=NOW))
        # a processed-token marker is not an episode of any wallet
        s.add(WalletTradeLabel(chain="bsc", wallet="", token="0x" + "8" * 40, kind="NONE", launchpad="fourmeme",
                               entry_at=NOW, labels=[], outcome={}, features={}, feature_version=wallet_labels.FEATURE_VERSION,
                               label_version=wallet_labels.LABEL_VERSION, created_at=NOW))
        await s.commit()
    p = (await client.get("/api/wallets/profiles?chain=bsc", headers=auth_headers)).json()["profiles"]
    assert p[0]["behaviour"] == {"episodes": 2, "labels": {wallet_labels.SUCCESSFUL: 1, wallet_labels.FAILED: 1,
                                                           wallet_labels.LATE_ENTRY: 1, wallet_labels.MISSED: 1}}
    d = (await client.get(f"/api/wallets/behaviour/bsc/{W.upper().replace('0X', '0x')}", headers=auth_headers)).json()
    assert [e["kind"] for e in d["episodes"]] == ["EPISODE", "EPISODE", "MISSED"]
    assert d["episodes"][1]["labels"] == [wallet_labels.FAILED, wallet_labels.LATE_ENTRY]
    assert d["summary"]["episodes"] == 2 and set(d["definitions"]) == set(wallet_labels.LABELS)
    assert "never a reason to copy or buy" in d["note"]
    none = (await client.get("/api/wallets/behaviour/bsc/0x" + "c" * 40, headers=auth_headers)).json()
    assert none["episodes"] == [] and none["summary"] is None
    assert (await client.get(f"/api/wallets/behaviour/bsc/{W}")).status_code == 401


async def test_ml_steps_show_each_step_and_need_auth(app, client, auth_headers):
    from yonixalpha_core.ml import steps

    redis = app.state.redis

    async def ok():
        return None
    await steps.timed(redis, "evm_wallet_ml", ok)
    r = (await client.get("/api/ml/steps", headers=auth_headers)).json()
    assert r["steps"]["evm_wallet_ml"]["state"] == "OK" and r["intervals_s"]["evm_wallet_ml"] == 1800
    assert "its own loop" in r["note"]
    assert (await client.get("/api/ml/steps")).status_code == 401
