"""Research pipeline (master §67): RESEARCH -> REVIEW -> PAPER -> VALIDATION ->
CONTROLLED_RELEASE, operator moves only, audited, never a rule change."""

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from yonixalpha_core.db.models import AuditLog, UpdateEvent

pytestmark = pytest.mark.asyncio
EVIDENCE = "read the factory contract and the official docs; events decoded"


async def test_research_items_move_one_stage_at_a_time_and_are_audited(app, client, auth_headers):
    r = (await client.get("/api/research", headers=auth_headers)).json()
    assert r["items"] == [] and r["stages"][0] == "RESEARCH" and r["stages"][-1] == "CONTROLLED_RELEASE"
    sugg = {(x["source"], x["ref"]) for x in r["suggestions"]}
    from yonixalpha_core.chains.registry import LAUNCHPADS
    observed = {k for k, v in LAUNCHPADS.items() if not v.supports_trading and v.active}
    assert observed and {("launchpad", k) for k in observed} <= sugg  # observed, never-traded venues
    assert "never changes a trading rule" in r["note"]

    lp = next(x for x in r["suggestions"] if x["source"] == "launchpad")
    made = await client.post("/api/research", headers=auth_headers, json={**lp, "summary": "observed for 7 days"})
    assert made.status_code == 200 and made.json()["stage"] == "RESEARCH" and made.json()["next"] == "REVIEW"
    iid = made.json()["id"]
    assert (await client.post("/api/research", headers=auth_headers, json=lp)).status_code == 409  # once per source
    again = (await client.get("/api/research", headers=auth_headers)).json()
    assert (lp["source"], lp["ref"]) not in {(x["source"], x["ref"]) for x in again["suggestions"]}

    skip = await client.post(f"/api/research/{iid}/move", headers=auth_headers, json={"stage": "PAPER", "note": EVIDENCE})
    assert skip.status_code == 409 and "one stage at a time" in skip.json()["detail"]["errors"][0]
    thin = await client.post(f"/api/research/{iid}/move", headers=auth_headers, json={"stage": "REVIEW", "note": "ok"})
    assert thin.status_code == 409
    ok = await client.post(f"/api/research/{iid}/move", headers=auth_headers, json={"stage": "REVIEW", "note": EVIDENCE})
    assert ok.status_code == 200 and ok.json()["stage"] == "REVIEW" and len(ok.json()["history"]) == 2
    rej = await client.post(f"/api/research/{iid}/move", headers=auth_headers,
                            json={"stage": "REJECTED", "note": "no sell path can be quoted on chain"})
    assert rej.status_code == 200 and rej.json()["next"] is None
    async with app.state.db_session_factory() as s:
        kinds = [a.event_type for a in (await s.execute(select(AuditLog))).scalars()]
    assert kinds.count("research.moved") == 2 and "research.created" in kinds
    assert (await client.post("/api/research/999999/move", headers=auth_headers,
                              json={"stage": "REVIEW", "note": EVIDENCE})).status_code == 404


async def test_update_events_are_suggested_and_bad_input_refused(app, client, auth_headers):
    async with app.state.db_session_factory() as s:
        ev = UpdateEvent(id=uuid.uuid4(), key="github:pons/pons-labs", detected_at=datetime.now(timezone.utc),
                         classification="BREAKING_CHANGE", from_ref="v1", to_ref="v2", summary={})
        s.add(ev)
        await s.commit()
        eid = str(ev.id)
    r = (await client.get("/api/research", headers=auth_headers)).json()
    assert any(x["source"] == "update_event" and x["ref"] == eid for x in r["suggestions"])
    bad_kind = await client.post("/api/research", headers=auth_headers, json={"kind": "rumour", "title": "x y z"})
    assert bad_kind.status_code == 422
    bad_ref = await client.post("/api/research", headers=auth_headers,
                                json={"kind": "launchpad", "title": "abc", "source": "update_event", "ref": "nope"})
    assert bad_ref.status_code == 422
    manual = await client.post("/api/research", headers=auth_headers,
                               json={"kind": "execution_method", "title": "Jito bundles for exits"})
    assert manual.status_code == 200 and manual.json()["source"] == "manual" and manual.json()["ref"] is None
    assert (await client.get("/api/research")).status_code == 401
