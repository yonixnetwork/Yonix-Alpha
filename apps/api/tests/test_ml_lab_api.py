"""ML Training Lab API: labels, ideas -> DRAFT experiments the operator
must accept, accept refused when untestable, edits only on drafts, every
write audited, CSV export, and nothing here changes a trading mode."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from yonixalpha_core.db.models import AuditLog, MlSignal, OpportunityOutcome, PlatformSetting
from yonixalpha_core import signal_center

NOW = datetime.now(timezone.utc).replace(microsecond=0)
URL = "/api/ml-lab"


async def test_label_an_observation_and_export_it(app, client, auth_headers):
    async with app.state.db_session_factory() as s:
        row = OpportunityOutcome(key="lab:1", mint="MINT1", engine="solana_migration", stage="GATE", decision="REJECT",
                                 traded=False, decided_at=NOW - timedelta(minutes=10), snapshot={"liquidity_sol": 40.0},
                                 status="COMPLETE", executable_return_pct=7, peak_pct=35, drawdown_pct=-5)
        s.add(row)
        await s.commit()
        opp_id = str(row.id)

    q = (await client.get(f"{URL}/queue", params={"hours": 1}, headers=auth_headers)).json()
    assert [r["id"] for r in q["rows"]] == [opp_id] and q["rows"][0]["category"] == "MIGRATED"
    assert (await client.get(f"{URL}/queue", params={"category": "NOPE"}, headers=auth_headers)).status_code == 422

    r = await client.post(f"{URL}/observations/{opp_id}/labels", json={"label": "NO_TRADE_MISSED", "notes": "clean run"},
                          headers=auth_headers)
    assert r.status_code == 200, r.text
    assert r.json()["verification"]["status"] == "AGREES" and r.json()["source"] == "MANUAL"
    dup = await client.post(f"{URL}/observations/{opp_id}/labels", json={"label": "NO_TRADE_MISSED"}, headers=auth_headers)
    assert dup.status_code == 422
    assert (await client.post(f"{URL}/observations/{opp_id}/labels", json={"label": "BOGUS"},
                              headers=auth_headers)).status_code == 422
    tl = (await client.get(f"{URL}/observations/{opp_id}", headers=auth_headers)).json()
    assert tl["labels"][0]["label"] == "NO_TRADE_MISSED" and tl["features"]["liquidity_sol"] == 40.0
    labels = (await client.get(f"{URL}/labels", headers=auth_headers)).json()
    assert len(labels["catalog"]) == 15 and labels["labels"]["NO_TRADE_MISSED"]["AGREES"] == 1

    csv = await client.get(f"{URL}/dataset", params={"days": 1, "format": "csv"}, headers=auth_headers)
    assert csv.status_code == 200 and "attachment" in csv.headers["content-disposition"]
    lines = csv.text.strip().splitlines()
    assert len(lines) == 2 and "NO_TRADE_MISSED" in lines[1]
    async with app.state.db_session_factory() as s:
        ev = (await s.execute(select(AuditLog.event_type).where(AuditLog.event_type.like("ml_lab.%")))).scalars().all()
    assert ev == ["ml_lab.label_added"]


async def test_idea_is_a_draft_until_accepted_and_untestable_ideas_cannot_be_accepted(app, client, auth_headers):
    async with app.state.db_session_factory() as s:
        settings_before = (await s.execute(select(func.count()).select_from(PlatformSetting))).scalar_one()

    bad = (await client.post(f"{URL}/ideas", json={"idea": "fresh tokens trending on twitter with liquidity"},
                             headers=auth_headers)).json()
    assert bad["status"] == "DRAFT" and bad["testable"] is False
    refused = await client.post(f"{URL}/experiments/{bad['id']}/accept", headers=auth_headers)
    assert refused.status_code == 409 and "not testable" in refused.text

    r = await client.post(f"{URL}/ideas", json={"idea": "Migrated tokens with liquidity above 30 SOL and 2 qualified wallets"},
                          headers=auth_headers)
    exp = r.json()
    assert exp["status"] == "DRAFT" and exp["testable"] and exp["proposal"]["categories"] == ["MIGRATED"]
    # a draft records nothing
    async with app.state.db_session_factory() as s:
        assert (await signal_center.generate(s))["experiments"] == 0

    bad_edit = await client.put(f"{URL}/experiments/{exp['id']}", json={"entry_conditions": [
        {"feature": "executable_return_pct", "op": ">", "value": 0}]}, headers=auth_headers)
    assert bad_edit.status_code == 422  # an outcome can never be a condition
    edit = await client.put(f"{URL}/experiments/{exp['id']}", json={"entry_conditions": [
        {"feature": "liquidity_sol", "op": ">=", "value": 50}], "exit": {"horizon": "T+15m"}}, headers=auth_headers)
    assert edit.status_code == 200 and "50 SOL" in edit.json()["proposal"]["hypothesis"]

    acc = await client.post(f"{URL}/experiments/{exp['id']}/accept", headers=auth_headers)
    assert acc.status_code == 200 and acc.json()["status"] == "ACCEPTED" and acc.json()["decided_by"]
    assert (await client.put(f"{URL}/experiments/{exp['id']}", json={"categories": ["FRESH"]},
                             headers=auth_headers)).status_code == 409  # accepted: frozen
    assert (await client.post(f"{URL}/experiments/{exp['id']}/reject", headers=auth_headers)).status_code == 409

    # a matching decision after the acceptance becomes one signal
    async with app.state.db_session_factory() as s:
        s.add(OpportunityOutcome(key="lab:2", mint="MINT2", engine="solana_migration", stage="GATE", decision="REJECT",
                                 traded=False, decided_at=datetime.now(timezone.utc), snapshot={"liquidity_sol": 60.0},
                                 status="TRACKING"))
        await s.commit()
        assert (await signal_center.generate(s))["signals"] == 1
        await s.commit()
        assert (await s.execute(select(func.count()).select_from(MlSignal))).scalar_one() == 1
    sigs = (await client.get(f"{URL}/signals", params={"experiment_id": exp["id"]}, headers=auth_headers)).json()
    assert sigs["signals"][0]["mint"] == "MINT2" and sigs["signals"][0]["outcome"] is None
    rep = (await client.get(f"{URL}/experiments/{exp['id']}/report", headers=auth_headers)).json()
    assert rep["signals"] == 1 and rep["verdict"]["state"] == "INSUFFICIENT_DATA"
    listing = (await client.get(f"{URL}/experiments", headers=auth_headers)).json()["experiments"]
    assert next(e for e in listing if e["id"] == exp["id"])["signals"] == 1

    ret = await client.post(f"{URL}/experiments/{exp['id']}/retire", headers=auth_headers)
    assert ret.json()["status"] == "RETIRED"
    async with app.state.db_session_factory() as s:
        assert (await s.execute(select(func.count()).select_from(PlatformSetting))).scalar_one() == settings_before
        ev = set((await s.execute(select(AuditLog.event_type).where(AuditLog.event_type.like("ml_lab.%")))).scalars().all())
    assert {"ml_lab.experiment_proposed", "ml_lab.experiment_edited", "ml_lab.experiment_accepted",
            "ml_lab.experiment_retired"} <= ev


async def test_lab_needs_login(client):
    assert (await client.get(f"{URL}/queue")).status_code == 401
    assert (await client.post(f"{URL}/ideas", json={"idea": "anything at all here"})).status_code == 401
