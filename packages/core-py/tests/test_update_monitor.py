"""Update monitor (master §64-66): rule-based classification, a baseline
first check that never notifies, one event per change, empty commits ignored,
a rate limit stops the pass without losing state, vulnerable installed
dependencies reported once, and notifications that say nothing is applied
automatically. GitHub / PyPI are mocked: real API behaviour is NOT VERIFIED here."""

import asyncio
import os
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from yonixalpha_core import update_monitor as um

NOW = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
PONS = um.Watch("github:ponsdotdev/ponsfamily", "robinhood", "Pons contracts", True, ("contractsV2", "abi.json"))
AGAVE = um.Watch("github:anza-xyz/agave", "solana", "validator releases")
HELIUS = um.Watch("github:helius-labs/helius-sdk", "provider", "Helius SDK")
DEP = um.Watch("pypi:httpx", "dependency", "every HTTP call", True)


def test_empty_commits_raise_nothing():
    cls, _, summary = um.classify(AGAVE, um.Change(commits=3))
    assert cls is None and summary["note"].startswith("no file changes")
    assert um.classify(AGAVE, um.Change()) == (None, {"security": False, "breaking": False, "api": False,
                                                      "performance": False}, {})


def test_classification_precedence_and_action_required():
    files = [{"filename": "src/lib.rs", "status": "modified"}]
    assert um.classify(AGAVE, um.Change(commits=1, messages=["tidy logs"], files=files))[0] == um.INFO
    assert um.classify(AGAVE, um.Change(old_release="v3.0.1", new_release="v3.0.2"))[0] == um.UPGRADE
    cls, flags, summary = um.classify(AGAVE, um.Change(old_release="v2.3.0", new_release="v3.0.0"))
    assert cls == um.BREAKING and flags["breaking"] and "major version change" in summary["reasons"]
    cls, flags, _ = um.classify(AGAVE, um.Change(commits=1, messages=["Fix CVE-2026-1234 in gossip"], files=files))
    assert cls == um.SECURITY and flags["security"]
    # provider: an API wording change is a PROVIDER_CHANGE, not an INFO
    assert um.classify(HELIUS, um.Change(commits=1, messages=["new rpc endpoint for priority fees"], files=files))[0] == um.PROVIDER
    # used directly: a change on a path we read (ABI) needs action, other paths do not
    abi = [{"filename": "contractsV2/abi.json", "status": "modified"}]
    cls, flags, summary = um.classify(PONS, um.Change(commits=1, messages=["update curve"], files=abi))
    assert cls == um.ACTION and flags["api"] and summary["files_on_paths_we_use"] == ["contractsV2/abi.json"]
    assert um.classify(PONS, um.Change(commits=1, messages=["readme"], files=[{"filename": "README.md"}]))[0] == um.INFO
    removed = [{"filename": "contractsV2/Curve.sol", "status": "removed"}]
    cls, flags, _ = um.classify(PONS, um.Change(commits=1, files=removed))
    assert cls == um.ACTION and flags["breaking"]
    # dependency: a new version is an upgrade; a vulnerable installed version is a security action
    assert um.classify(DEP, um.Change(old_version="0.27.0", new_version="0.28.1"))[0] == um.UPGRADE
    cls, _, summary = um.classify(DEP, um.Change(old_version="0.27.0", new_version="0.27.0",
                                                 vulnerabilities=[{"id": "GHSA-x", "fixed_in": ["0.27.1"]}]))
    assert cls == um.ACTION and summary["vulnerabilities"] == [{"id": "GHSA-x", "fixed_in": ["0.27.1"]}]


def test_message_says_nothing_is_applied():
    title, body = um.message({"classification": um.ACTION, "key": PONS.key, "why": PONS.why,
                              "summary": {"reasons": ["1 changed file(s) on paths we use"], "messages": ["update curve"]}})
    assert title == "ACTION REQUIRED: ponsdotdev/ponsfamily"
    assert "What to do (INTEGRATION CHECK)" in body and body.endswith("Never applied automatically.")


class FakeGitHub:
    """Serves the three GitHub endpoints and PyPI JSON from mutable state."""

    def __init__(self):
        self.head = "a" * 40
        self.release = {"tag_name": "v1.0.0", "name": "v1.0.0", "body": ""}
        self.compare = {}
        self.pypi_latest = "0.28.1"
        self.vulns: list = []
        self.rate_limited = False
        self.calls: list[str] = []
        self.auth: list[str | None] = []

    def handler(self, req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        self.calls.append(url)
        if "api.github.com" in url:
            self.auth.append(req.headers.get("authorization"))
        if self.rate_limited and "api.github.com" in url:
            return httpx.Response(403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1"})
        if url.endswith("/commits?per_page=1"):
            return httpx.Response(200, json=[{"sha": self.head, "commit": {"committer": {"date": "2026-10-02T10:00:00Z"}}}])
        if url.endswith("/releases/latest"):
            return httpx.Response(200, json=self.release) if self.release else httpx.Response(404)
        if "/compare/" in url:
            return httpx.Response(200, json=self.compare)
        if url == f"{um.PYPI}/httpx/json":
            return httpx.Response(200, json={"info": {"version": self.pypi_latest}})
        if url.startswith(f"{um.PYPI}/httpx/"):
            return httpx.Response(200, json={"vulnerabilities": self.vulns})
        return httpx.Response(404)


async def test_check_all_baseline_then_events_rate_limit_and_vulnerabilities(monkeypatch):
    engine = create_async_engine(os.environ.get(
        "DATABASE_URL", "postgresql+asyncpg://yonixalpha:yonixalpha_test_pw@localhost:5432/yonixalpha_test"))
    from yonixalpha_core.db import models
    from yonixalpha_core.db.base import Base, make_session_factory

    monkeypatch.setattr(um, "installed_version", lambda pkg: "0.27.0")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    gh = FakeGitHub()
    sf = make_session_factory(engine)
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(gh.handler)) as client:
            async def check(at):
                async with sf() as s:
                    out = await um.check_all(s, client, at, token="t0k", watches=(PONS, DEP))
                    await s.commit()
                return out

            # 1. baseline: stored, nothing reported (dependency has no known vulnerability)
            assert await check(NOW) == []
            assert gh.auth and set(gh.auth) == {"Bearer t0k"}
            async with sf() as s:
                row = await s.get(models.UpdateWatch, PONS.key)
                assert row.latest_commit == "a" * 40 and row.latest_release == "v1.0.0" and row.classification is None
                assert (await s.get(models.UpdateWatch, DEP.key)).installed_version == "0.27.0"
            # 2. nothing changed: no events, no compare call
            n = len(gh.calls)
            assert await check(NOW + timedelta(hours=6)) == []
            assert not any("/compare/" in c for c in gh.calls[n:])
            # 3. empty commit: recorded, no event
            gh.head, gh.compare = "b" * 40, {"total_commits": 1, "commits": [{"commit": {"message": "chore: bump"}}], "files": []}
            assert await check(NOW + timedelta(hours=12)) == []
            async with sf() as s:
                row = await s.get(models.UpdateWatch, PONS.key)
                assert row.latest_commit == "b" * 40 and row.previous_commit == "a" * 40
                assert row.change_summary["last_check"]["note"].startswith("no file changes")
            # 4. ABI change + a new PyPI version: one event each
            gh.head = "c" * 40
            gh.compare = {"total_commits": 2, "commits": [{"commit": {"message": "update V2 curve abi"}}],
                          "files": [{"filename": "contractsV2/abi.json", "status": "modified"}]}
            gh.pypi_latest = "0.29.0"
            evs = await check(NOW + timedelta(hours=18))
            assert [(e["key"], e["classification"]) for e in evs] == [(PONS.key, um.ACTION), (DEP.key, um.UPGRADE)]
            # the same state again is not reported twice
            assert await check(NOW + timedelta(hours=24)) == []
            # 5. a newly published vulnerability of the installed version: reported once
            gh.vulns = [{"id": "GHSA-abcd", "fixed_in": ["0.27.2"]}]
            evs = await check(NOW + timedelta(hours=30))
            assert [(e["key"], e["classification"]) for e in evs] == [(DEP.key, um.ACTION)]
            assert await check(NOW + timedelta(hours=36)) == []
            # 6. rate limited: the pass stops, the error is recorded, earlier state is kept
            gh.rate_limited = True
            assert await check(NOW + timedelta(hours=42)) == []
            async with sf() as s:
                row = await s.get(models.UpdateWatch, PONS.key)
                assert "rate limited" in row.error and row.latest_commit == "c" * 40
                assert (await s.get(models.UpdateWatch, DEP.key)).last_checked == NOW + timedelta(hours=36)  # not reached
                stored = (await s.execute(select(models.UpdateEvent).order_by(models.UpdateEvent.detected_at))).scalars().all()
                assert [e.classification for e in stored] == [um.ACTION, um.UPGRADE, um.ACTION]
                assert stored[0].from_ref == "b" * 40 and stored[0].to_ref == "c" * 40
                assert stored[1].from_ref == "0.27.0" and stored[1].to_ref == "0.29.0"
        # official APIs only, never HTML pages
        assert all(c.startswith(("https://api.github.com/repos/", "https://pypi.org/pypi/")) for c in gh.calls)
    finally:
        await engine.dispose()


async def test_vulnerable_dependency_is_reported_at_baseline_and_run_notifies(monkeypatch):
    engine = create_async_engine(os.environ.get(
        "DATABASE_URL", "postgresql+asyncpg://yonixalpha:yonixalpha_test_pw@localhost:5432/yonixalpha_test"))
    from yonixalpha_core import events
    from yonixalpha_core.db import models
    from yonixalpha_core.db.base import Base, make_session_factory

    monkeypatch.setattr(um, "installed_version", lambda pkg: "0.27.0")
    monkeypatch.setattr(um, "WATCHES", (DEP,))
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    gh = FakeGitHub()
    gh.vulns = [{"id": "PYSEC-1", "fixed_in": ["0.27.1"]}]
    sent = []

    async def fake_notify(session, redis, settings, kind, title, body=None, severity="info", data=None):
        sent.append((kind, title, body, severity, data, settings is not None))

    monkeypatch.setattr(events, "notify", fake_notify)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(um.httpx, "AsyncClient", lambda: real_client(transport=httpx.MockTransport(gh.handler)))

    class S:
        GITHUB_TOKEN = None

    stop = asyncio.Event()
    sf = make_session_factory(engine)
    try:
        task = asyncio.create_task(um.run(sf, None, S(), stop, interval=3600))
        for _ in range(100):
            if sent:
                break
            await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, 5)
        assert len(sent) == 1
        kind, title, body, severity, data, telegram = sent[0]
        assert kind == "infrastructure_update" and severity == "critical" and title == "ACTION REQUIRED: httpx"
        assert telegram  # ACTION REQUIRED goes to Telegram; UPGRADE AVAILABLE only in-app (below)
        assert "1 known vulnerabilities" in body and "PIN BUMP" in body and body.endswith("Never applied automatically.")
        async with sf() as s:
            ev = (await s.execute(select(models.UpdateEvent))).scalar_one()
            assert ev.notified and str(ev.id) == data["event_id"]
        # a newer version (no vulnerability left): UPGRADE AVAILABLE, in-app only
        gh.vulns, gh.pypi_latest, sent[:] = [], "0.29.0", []
        stop.clear()
        task = asyncio.create_task(um.run(sf, None, S(), stop, interval=3600))
        for _ in range(100):
            if sent:
                break
            await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, 5)
        assert [(t[1], t[3], t[5]) for t in sent] == [("UPGRADE AVAILABLE: httpx", "info", False)]
        from yonixalpha_core.events import DEFAULT_TELEGRAM_KINDS, NOTIFICATION_KINDS
        assert "infrastructure_update" in DEFAULT_TELEGRAM_KINDS and "infrastructure_update" in NOTIFICATION_KINDS
    finally:
        await engine.dispose()


def test_apply_guide_says_how_each_update_reaches_the_server():
    dep = next(w for w in um.WATCHES if w.key == "pypi:websockets")
    g = um.apply_guide(dep, um.UPGRADE, "14.1", "17.1")
    assert g["action"] == um.PIN_BUMP and um.PIN_FILE in g["steps"] and "deploying again keeps" in g["steps"]
    assert um.apply_guide(dep, um.UPGRADE, "17.1", "17.1")["action"] == um.APPLIED
    idl = next(w for w in um.WATCHES if w.key == "github:pump-fun/pump-public-docs")
    assert um.apply_guide(idl, um.ACTION, None, "abc")["action"] == um.INTEGRATION_CHECK
    assert um.apply_guide(idl, um.INFO, None, "abc")["action"] == um.REVIEW_ONLY
    ref = next(w for w in um.WATCHES if w.key == "github:anza-xyz/agave")
    assert um.apply_guide(ref, um.BREAKING, None, "v3")["action"] == um.REVIEW_ONLY  # not used directly
    title, body = um.message({"key": "pypi:websockets", "classification": um.BREAKING, "why": dep.why,
                              "summary": {"version": {"installed": "14.1", "latest": "17.1"}}})
    assert "PIN BUMP" in body and "Never applied automatically" in body
