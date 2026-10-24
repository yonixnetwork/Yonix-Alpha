"""ML engine monitoring over the API: computed in the background cache,
login required, history bounded and filterable."""

import asyncio

from yonixalpha_core.db.models import MlExperiment


async def test_monitor_and_history(app, client, auth_headers):
    async with app.state.db_session_factory() as s:
        s.add(MlExperiment(idea="fresh tokens with liquidity above 30 SOL", status="DRAFT", proposal={}, testable=True))
        await s.commit()
    r = await client.get("/api/ml/monitor", headers=auth_headers)
    for _ in range(40):  # 202 until the background computation has a result
        if r.status_code == 200 and r.json().get("review_status") == "CURRENT":
            break
        await asyncio.sleep(0.25)
        r = await client.get("/api/ml/monitor", headers=auth_headers)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["experiment_counts"]["DRAFT"] == 1 and d["experiments"][0]["status"] == "AWAITING MY APPROVAL"
    assert "paper_trades" in d and "shadow_predictions" in d and d["signals"]["total"] == 0
    h = await client.get("/api/ml/history", params={"traded": "true", "hours": 24}, headers=auth_headers)
    assert h.status_code == 200 and h.json()["rows"] == []
    assert (await client.get("/api/ml/history", params={"engine": "evm_bsc"}, headers=auth_headers)).status_code == 422


async def test_monitor_needs_login(client):
    assert (await client.get("/api/ml/monitor")).status_code == 401
    assert (await client.get("/api/ml/history")).status_code == 401
