"""ML governance (master §38-42): stages, operator-set contribution with its
gates, frozen validation sets and reports."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from yonixalpha_core.db.models import AuditLog, MlValidationReport, MlValidationSet, ModelVersion, PlatformSetting
from yonixalpha_core.ml import governance

pytestmark = pytest.mark.asyncio
NOW = datetime.now(timezone.utc).replace(microsecond=0)
NAME = "solana_candidate_momentum"


async def _put(client, headers, stage, percent, name=NAME):
    return await client.put(f"/api/ml/governance/{name}", headers=headers, json={"stage": stage, "percent": percent})


async def test_governance_starts_at_zero_and_needs_a_validated_champion(app, client, auth_headers):
    g = (await client.get("/api/ml/governance", headers=auth_headers)).json()
    m = {x["name"]: x for x in g["models"]}[NAME]
    assert (m["stage"], m["percent"], m["weight"], m["kind"]) == ("SHADOW", 0, 0.0, "contributor")
    assert m["health"] == "NOT_VALIDATED" and m["validation"] is None and m["version"] is None
    assert g["frozen_sets"] == [] and set(g["not_validated"]) == {"copy_trades", "wallet_exits"}
    assert g["rules"]["max_pct"] == governance.MAX_PCT and "LIVE_CONTRIBUTOR is locked" in g["rules"]["live"]

    # no champion, no frozen-set PASS: refused, with every reason
    r = await _put(client, auth_headers, "PAPER_CONTRIBUTOR", 5)
    assert r.status_code == 409
    errors = r.json()["detail"]["errors"]
    assert any("champion" in e for e in errors) and any("not PASS" in e for e in errors)

    async with app.state.db_session_factory() as s:
        champ = ModelVersion(name=NAME, version=1, status="active", feature_names=["x"], training_sample_count=500,
                             metrics={"holdout_auc": 0.7}, artifact=b"not loaded by the API", activated_at=NOW)
        fs = MlValidationSet(family="solana_candidate", window_start=NOW - timedelta(days=3),
                             window_end=NOW - timedelta(days=2), frozen_at=NOW, samples=300)
        s.add_all([champ, fs])
        await s.flush()
        s.add(MlValidationReport(set_id=fs.id, model_name=NAME, model_version=1, evaluated_at=NOW, status="FAIL",
                                 reason="calibration error 0.2 > 0.1", metrics={"n": 300, "auc": 0.6, "ece": 0.2}))
        await s.commit()
    r = await _put(client, auth_headers, "PAPER_CONTRIBUTOR", 5)
    assert r.status_code == 409 and any("(FAIL)" in e for e in r.json()["detail"]["errors"])
    g = (await client.get("/api/ml/governance", headers=auth_headers)).json()
    m = {x["name"]: x for x in g["models"]}[NAME]
    assert m["health"] == "DEGRADED" and m["champion_version"] == 1 and m["validation_samples"] == 300

    async with app.state.db_session_factory() as s:
        rep = (await s.execute(select(MlValidationReport))).scalar_one()
        rep.status, rep.reason = "PASS", "AUC 0.7"
        rep.metrics = {"n": 300, "positives": 120, "auc": 0.7, "auc_lower_bound": 0.64, "ece": 0.05, "brier": 0.2}
        await s.commit()
    # more than one step at once, a stage without a percentage, live: refused
    assert (await _put(client, auth_headers, "PAPER_CONTRIBUTOR", 10)).status_code == 409
    assert (await _put(client, auth_headers, "PAPER_CONTRIBUTOR", 7)).status_code == 409
    assert (await _put(client, auth_headers, "SHADOW", 5)).status_code == 409
    live = await _put(client, auth_headers, "LIVE_CONTRIBUTOR", 0)
    assert live.status_code == 409 and "locked" in live.json()["detail"]["errors"][0]

    r = await _put(client, auth_headers, "PAPER_CONTRIBUTOR", 5)
    assert r.status_code == 200 and r.json()["weight"] == 0.05
    async with app.state.db_session_factory() as s:
        assert await governance.weight_for(s, NAME) == 0.05
        audit = (await s.execute(select(AuditLog).where(AuditLog.event_type == "ml.contribution_changed"))).scalar_one()
        assert audit.detail["after"]["percent"] == 5 and audit.detail["validation"] == "PASS"
    # a second raise inside 7 days is refused, however well things went
    r = await _put(client, auth_headers, "PAPER_CONTRIBUTOR", 10)
    assert r.status_code == 409 and any("wait 7 days" in e for e in r.json()["detail"]["errors"])
    # lowering is always allowed at once
    r = await _put(client, auth_headers, "SHADOW", 0)
    assert r.status_code == 200 and r.json()["weight"] == 0.0
    async with app.state.db_session_factory() as s:
        row = await s.get(PlatformSetting, governance.KEY)
        assert row.value["models"][NAME]["stage"] == "SHADOW" and row.value["models"][NAME]["raised_at"]
    g = (await client.get("/api/ml/governance", headers=auth_headers)).json()
    m = {x["name"]: x for x in g["models"]}[NAME]
    assert m["health"] == "OK" and m["out_of_sample_auc"] == 0.7 and m["confidence_auc_lower_bound"] == 0.64
    assert g["frozen_sets"][0]["reports"] == 1 and g["frozen_sets"][0]["passed"] == 1


async def test_models_without_a_consumer_stay_shadow(app, client, auth_headers):
    async with app.state.db_session_factory() as s:
        s.add(ModelVersion(name="shadow_evm_p_upside_50", version=3, status="shadow", feature_names=["x"],
                           training_sample_count=900, metrics={"target": "P_UPSIDE_50", "kind": "binary",
                                                               "frozen_excluded": [1, 2]},
                           artifact=b"not loaded by the API"))
        await s.commit()
    g = (await client.get("/api/ml/governance", headers=auth_headers)).json()
    m = {x["name"]: x for x in g["models"]}["shadow_evm_p_upside_50"]
    assert (m["kind"], m["stage"], m["family"], m["frozen_excluded"]) == ("shadow", "SHADOW", "evm_entry", 2)
    r = await _put(client, auth_headers, "PAPER_CONTRIBUTOR", 5, "shadow_evm_p_upside_50")
    assert r.status_code == 409 and "no decision consumer" in r.json()["detail"]["errors"][0]
    assert (await _put(client, auth_headers, "OBSERVATION_ONLY", 0, "shadow_evm_p_upside_50")).status_code == 200
    assert (await client.get("/api/ml/governance")).status_code == 401
    assert (await client.put(f"/api/ml/governance/{NAME}", json={"stage": "SHADOW", "percent": 0})).status_code == 401
