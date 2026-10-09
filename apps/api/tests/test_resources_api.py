"""Low-resource operation (2026-10-08): System Health resources, the
resource mode and the copy-trading status with its resume check."""

from yonixalpha_core import resources
from yonixalpha_core.db.models import AuditLog


def host(avail=2000, load=0.4, swap=0):
    return lambda: {"cpus": 2, "memory": {"total_mb": 1967, "available_mb": avail, "swap_used_mb": swap},
                    "load": {"1m": load, "5m": load, "15m": load}, "pressure": {}, "disk": None, "cpu_busy_pct": None}


def copy_on(app):
    app.state.settings = app.state.settings.model_copy(update={"COPY_TRADING_ENABLED": True})


async def test_resources_show_mode_level_and_what_is_paused(client, auth_headers, monkeypatch, app):
    copy_on(app)
    monkeypatch.setattr(resources, "sample", host(avail=345, load=9.82, swap=1298))
    await app.state.redis.set("yx:hb:paper-trading", '{"at": "2026-10-08T10:00:00+00:00", "rss_mb": 210.5, '
                                                           '"cpu_s": 100.0, "detail": {}}')
    r = (await client.get("/api/system/resources", headers=auth_headers)).json()
    assert r["mode"]["resource_mode"] == "LOW_RESOURCE" and r["mode"]["label"] == "LOW RESOURCE MODE"
    assert r["host"]["memory"]["available_mb"] == 345
    # 2026-10-09: the profile loop overwrote these with a string and System Health crashed on .join
    assert r["level"] == "WARNING" and isinstance(r["level_reasons"], list) and r["level_reasons"]
    assert "copy trading" in r["paused_now"] and r["copy_trading"]["status"] == "SUSPENDED"
    assert r["copy_trading"]["label"] == "SUSPENDED — LOW SERVER RESOURCES"
    assert r["postgres"]["connections"] and "shared_buffers" in r["postgres"]["settings"]
    assert r["redis"]["used_mb"] > 0
    pt = next(w for w in r["workers"] if w["service"] == "paper-trading")
    assert pt["rss_mb"] == 210.5 and pt["cpu_pct"] is None  # one heartbeat: no share yet
    await app.state.redis.set("yx:hb:paper-trading", '{"at": "2026-10-08T10:00:30+00:00", "rss_mb": 211, '
                                                           '"cpu_s": 106.0, "detail": {}}')
    r = (await client.get("/api/system/resources", headers=auth_headers)).json()
    assert next(w for w in r["workers"] if w["service"] == "paper-trading")["cpu_pct"] == 20.0  # 6 s of 30 s


async def test_copy_resume_is_refused_while_resources_are_unsafe(client, auth_headers, monkeypatch, app):
    copy_on(app)
    monkeypatch.setattr(resources, "sample", host(avail=345, load=9.82, swap=1298))
    s = (await client.get("/api/copy/trading-status", headers=auth_headers)).json()
    assert s["status"] == "SUSPENDED" and s["resume"]["recommendation"] == "WAIT" and s["resume"]["auto_resume"] is False
    assert s["current"]["ram_available_mb"] == 345 and s["current"]["swap_used_mb"] == 1298
    assert "Copy trading has been paused" in s["reason"]
    r = await client.post("/api/copy/trading-status", json={"status": "ACTIVE"}, headers=auth_headers)
    assert r.status_code == 409 and r.json()["detail"]["recommendation"] == "WAIT"
    assert len(r.json()["detail"]["blocked_by"]) >= 3
    assert (await client.get("/api/copy/trading-status", headers=auth_headers)).json()["status"] == "SUSPENDED"

    monkeypatch.setattr(resources, "sample", host())
    r = await client.post("/api/copy/trading-status", json={"status": "THROTTLED", "note": "after resize"},
                          headers=auth_headers)
    assert r.status_code == 200 and r.json()["status"] == "THROTTLED"
    r = await client.post("/api/copy/trading-status", json={"status": "SUSPENDED"}, headers=auth_headers)
    assert r.json()["status"] == "SUSPENDED"
    assert (await client.post("/api/copy/trading-status", json={"status": "ON"}, headers=auth_headers)).status_code == 422

    await client.put("/api/system/resource-mode", json={"mode": "EMERGENCY"}, headers=auth_headers)
    r = await client.post("/api/copy/trading-status", json={"status": "ACTIVE"}, headers=auth_headers)
    assert r.status_code == 409 and "EMERGENCY resource mode" in r.json()["detail"]["blocked_by"]
    async with app.state.db_session_factory() as s:
        from sqlalchemy import select
        kinds = (await s.execute(select(AuditLog.event_type))).scalars().all()
    assert "copy.trading_status" in kinds and "system.resource_mode" in kinds


async def test_copy_trading_disabled_by_the_profile_is_never_resumed(client, auth_headers, monkeypatch, app):
    """COPY_TRADING_ENABLED=false (production default): SUSPENDED whatever
    the dashboard stored, resume refused even with plenty of resources, and
    the SOLANA_ONLY profile is listed with what it switches off."""
    monkeypatch.setattr(resources, "sample", host())
    s = (await client.get("/api/copy/trading-status", headers=auth_headers)).json()
    assert s["status"] == "SUSPENDED" and s["enabled"] is False and s["label"] == "SUSPENDED — DISABLED BY SYSTEM PROFILE"
    assert s["resume"]["safe"] is False and s["resume"]["recommendation"] == "DISABLED"
    r = await client.post("/api/copy/trading-status", json={"status": "ACTIVE"}, headers=auth_headers)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "COPY_DISABLED"
    assert (await client.post("/api/copy/trading-status", json={"status": "SUSPENDED"}, headers=auth_headers)).status_code == 200
    r = (await client.get("/api/system/resources", headers=auth_headers)).json()
    assert r["system_profile"]["profile"] == "SOLANA_ONLY" and r["system_profile"]["enabled_chains"] == ["solana"]
    assert r["system_profile"]["compose_profiles"] == []
    prof = (await client.get("/api/system/profile", headers=auth_headers)).json()
    assert prof["chains"]["bsc"] == {"enabled": False, "reason": "DISABLED — SOLANA_ONLY MODE"}
    assert (await client.get("/api/system/profile")).status_code == 401
    assert any(p.startswith("data-evm: DISABLED — SOLANA_ONLY MODE") for p in r["paused_now"])
    evm = next(w for w in r["workers"] if w["service"] == "data-evm")
    assert evm["disabled"].startswith("DISABLED — SOLANA_ONLY MODE")
    assert next(w for w in r["workers"] if w["service"] == "paper-trading")["disabled"] is None


async def test_review_page_is_202_while_its_first_result_waits_for_resources(client, auth_headers, monkeypatch):
    monkeypatch.setattr(resources, "level", lambda s, st: (resources.CRITICAL, ["load 9.82 = 4.91 per CPU >= 3.0"]))
    r = await client.get("/api/ml/ledger-review", params={"days": 7}, headers=auth_headers)
    assert r.status_code == 202 and r.json()["review_status"] == "PENDING" and "CRITICAL" in r.json()["deferred"]
    r = await client.get("/api/ml/ledger-review", params={"days": 7, "refresh": True}, headers=auth_headers)
    assert r.status_code == 200 and r.json()["review_status"] == "CURRENT"  # the operator asked: computed anyway


async def test_losing_and_rejected_up_lists_read_their_partial_indexes(client, auth_headers, app):
    """Server 2026-10-08: these two ML Review lists answered 503 after 25 s
    scanning a week of opportunity_outcomes. Their filters must match the
    partial indexes of migration 0044 (created here the same way)."""
    from sqlalchemy import select, text
    from sqlalchemy.dialects import postgresql

    from app.api.routes.ml import opportunity_filters
    from yonixalpha_core.db.models import OpportunityOutcome

    async with app.state.db_session_factory() as s:
        await s.execute(text("CREATE INDEX IF NOT EXISTS ix_opportunity_outcomes_losses ON opportunity_outcomes "
                             "(decided_at) WHERE loss_analysis IS NOT NULL"))
        await s.execute(text("CREATE INDEX IF NOT EXISTS ix_opportunity_outcomes_rejected_up ON opportunity_outcomes "
                             "(decided_at) WHERE traded IS false AND peak_pct >= 30"))
        await s.commit()
        for kw, index in (({"losses_only": True}, "ix_opportunity_outcomes_losses"),
                          ({"rejected_up": True}, "ix_opportunity_outcomes_rejected_up")):
            q = select(OpportunityOutcome.id).where(*opportunity_filters(days=7, **kw)) \
                .order_by(OpportunityOutcome.decided_at.desc()).limit(20)
            sql = str(q.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
            await s.execute(text("SET LOCAL enable_seqscan = off"))
            plan = "\n".join(r[0] for r in (await s.execute(text("EXPLAIN " + sql))).all())
            assert index in plan, plan
            await s.rollback()
    for kw in ({"losses_only": True}, {"rejected_up": True}):
        r = await client.get("/api/ml/opportunities", params={**kw, "days": 7, "limit": 20}, headers=auth_headers)
        assert r.status_code == 200 and r.json()["total"] == 0


async def test_category_lists_read_their_expression_indexes_and_count_is_capped(client, auth_headers, app, monkeypatch):
    """Server 2026-10-09: "Rejection justified" / "Correct rejections" lists
    answered 503 after 25 s (the category is inside the analysis JSON). Each
    category filter must use the expression index of migration 0045, and the
    list's total is counted only up to OPPORTUNITY_COUNT_CAP."""
    from datetime import datetime, timezone

    from sqlalchemy import select, text
    from sqlalchemy.dialects import postgresql

    from app.api.routes import ml as ml_routes
    from app.api.routes.ml import opportunity_filters
    from yonixalpha_core.db.models import OpportunityOutcome

    async with app.state.db_session_factory() as s:
        for category, index in (("missed_win", "ix_opportunity_outcomes_cf_class"),
                                ("correct_rejection", "ix_opportunity_outcomes_cf_class"),
                                ("rejection_justified_drawdown", "ix_opportunity_outcomes_cf_class"),
                                ("counterfactual_unknown", "ix_opportunity_outcomes_cf_class"),
                                ("premature_exit", "ix_opportunity_outcomes_exit_class"),
                                ("recovery", "ix_opportunity_outcomes_recovery")):
            q = select(OpportunityOutcome.id).where(*opportunity_filters(days=7, category=category)) \
                .order_by(OpportunityOutcome.decided_at.desc()).limit(25)
            sql = str(q.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
            await s.execute(text("SET LOCAL enable_seqscan = off"))
            plan = "\n".join(r[0] for r in (await s.execute(text("EXPLAIN " + sql))).all())
            assert index in plan, (category, plan)
            await s.rollback()
        now = datetime.now(timezone.utc)
        s.add_all([OpportunityOutcome(key=f"k{i}", mint=f"M{i}", engine="solana_fresh", stage="GATE", decision="REJECT",
                                      traded=False, decided_at=now, snapshot={},
                                      analysis={"counterfactual": {"classification": "CORRECT_REJECTION"}})
                   for i in range(5)])
        await s.commit()
    monkeypatch.setattr(ml_routes, "OPPORTUNITY_COUNT_CAP", 3)
    r = (await client.get("/api/ml/opportunities", params={"category": "correct_rejection", "days": 7, "limit": 2},
                          headers=auth_headers)).json()
    assert (r["total"], r["total_capped"], len(r["items"])) == (3, True, 2)
    monkeypatch.setattr(ml_routes, "OPPORTUNITY_COUNT_CAP", 1000)
    r = (await client.get("/api/ml/opportunities", params={"category": "correct_rejection", "days": 7},
                          headers=auth_headers)).json()
    assert (r["total"], r["total_capped"]) == (5, False)
