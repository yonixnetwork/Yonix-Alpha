"""Scanner pipeline health (memecoin refocus Phase 3): every stage's state
comes from its last recorded activity; nothing is CONNECTED because a
process runs, and missing evidence is a DATA GAP."""

import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from yonixalpha_core import gate_events
from yonixalpha_core.db.models import PaperPosition, RiskAssessment
from yonixalpha_core.db.redis import make_redis
from yonixalpha_core.config import get_settings
from yonixalpha_core.scanner_health import CONNECTED, DATA_GAP, DISCONNECTED, INFO, STALE, stage_state
from yonixalpha_core.solana import pump_stream, pumpportal_ws


def test_stage_state_thresholds():
    assert stage_state(None, 60, 300) == DATA_GAP
    assert stage_state(10, 60, 300) == CONNECTED
    assert stage_state(61, 60, 300) == STALE
    assert stage_state(301, 60, 300) == DISCONNECTED
    assert stage_state(10 ** 6, None, None) == INFO  # paper trades: information only


async def test_scanner_health_reports_each_stage_from_evidence(app, client, auth_headers):
    r = await client.get("/api/system/scanner-health", headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["overall"] == DATA_GAP and all(s["state"] == DATA_GAP for s in body["stages"])

    now = datetime.now(timezone.utc)
    redis = make_redis(get_settings())
    await redis.set(pump_stream.HEARTBEAT, now.isoformat())
    await redis.set(pumpportal_ws.HEARTBEAT, str(time.time() - 400))  # 400 s: STALE (> 120 s)
    await redis.zadd(pump_stream.RECENT, {"MINT_NEW": now.timestamp() - 30})
    await redis.zadd(pump_stream.PROMOTED, {"MINT_OLD": now.timestamp() - 10 * 3600})  # DISCONNECTED (> 2 h)
    await redis.rpush(gate_events.GATE_WAKE, "c1", "c2")
    await redis.hset(pumpportal_ws.STATS, mapping={"create": 5, "disconnects": 2})
    await redis.aclose()
    async with app.state.db_session_factory() as s:
        s.add(RiskAssessment(idempotency_key="k1", engine="solana_fresh", strategy="fresh_launch_flow", asset_id="M",
                             decision="NO_TRADE", status_label="NO_TRADE", executable=False, execution_target="NONE",
                             overall_risk="LOW", risk_engine_version="1", evaluated_at=now - timedelta(seconds=100),
                             assessment={}))
        s.add(PaperPosition(id=uuid.uuid4(), symbol="T", provider="paper", side="LONG", engine="solana_fresh",
                            entry_price=Decimal(1), quantity=Decimal(1), take_profit=[], status="closed",
                            execution_mode="PAPER", entry_at=now - timedelta(hours=3)))
        await s.commit()

    body = (await client.get("/api/system/scanner-health", headers=auth_headers)).json()
    st = {s["stage"]: s["state"] for s in body["stages"]}
    assert st == {"stream_event": CONNECTED, "pumpportal_event": STALE, "token_discovered": CONNECTED,
                  "candidate_promoted": DISCONNECTED, "candidate_evaluated": CONNECTED, "paper_trade": INFO}, st
    assert body["overall"] == DISCONNECTED  # the worst counted stage; the paper trade never counts
    assert body["queues"]["gate_wake_depth"] == 2
    assert body["pumpportal_counters"] == {"create": 5, "disconnects": 2}
    assert set(body["rpc_last_hour"]) >= {"ok", "http_429", "failed", "failover_retry", "rate_limited_share"}
    assert (await client.get("/api/system/scanner-health")).status_code == 401
