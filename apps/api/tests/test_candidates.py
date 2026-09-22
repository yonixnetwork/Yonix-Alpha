from datetime import datetime, timezone
from decimal import Decimal

import pytest

from yonixalpha_core.db.models import PaperPosition, RiskEvent, StrategySignal, Token, TradingCandidate
from yonixalpha_core.state_machine import CandidateState

pytestmark = pytest.mark.asyncio

MINT = "TestMint111111111111111111111111111111111"


async def _seed_candidate(app, state=CandidateState.DISCOVERED, mint=MINT) -> TradingCandidate:
    async with app.state.db_session_factory() as session:
        token = Token(mint_address=mint, first_seen_source="test")
        session.add(token)
        await session.flush()
        candidate = TradingCandidate(
            token_id=token.id,
            engine="momentum",
            state=state.value,
            state_history=[{"state": state.value, "at": datetime.now(timezone.utc).isoformat(), "reason": "test"}],
            detail={"ratio": 3.5},
        )
        session.add(candidate)
        await session.commit()
        await session.refresh(candidate)
        return candidate


async def test_list_candidates_requires_auth(client):
    resp = await client.get("/api/candidates")
    assert resp.status_code == 401


async def test_list_candidates_empty(client, auth_headers):
    resp = await client.get("/api/candidates", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"items": [], "total": 0, "limit": 50, "offset": 0}


async def test_list_candidates_returns_seeded_row(app, client, auth_headers):
    candidate = await _seed_candidate(app)

    resp = await client.get("/api/candidates", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["id"] == str(candidate.id)
    assert body["items"][0]["mint_address"] == MINT
    assert body["items"][0]["state"] == "discovered"


async def test_list_candidates_filters_by_state(app, client, auth_headers):
    await _seed_candidate(app, state=CandidateState.DISCOVERED, mint=MINT + "A")
    await _seed_candidate(app, state=CandidateState.REJECTED, mint=MINT + "B")

    resp = await client.get("/api/candidates", params={"state": "rejected"}, headers=auth_headers)
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["state"] == "rejected"


async def test_get_candidate_not_found(client, auth_headers):
    resp = await client.get("/api/candidates/00000000-0000-0000-0000-000000000000", headers=auth_headers)
    assert resp.status_code == 404


async def test_get_candidate_detail_includes_related_rows(app, client, auth_headers):
    candidate = await _seed_candidate(app)

    async with app.state.db_session_factory() as session:
        session.add(
            StrategySignal(
                candidate_id=candidate.id,
                symbol=MINT,
                decision="WAIT",
                confidence=Decimal("0.2"),
                risk_score=Decimal("0.5"),
                data_quality="degraded",
            )
        )
        session.add(RiskEvent(candidate_id=candidate.id, symbol=MINT, approved=True, reasons=[]))
        session.add(
            PaperPosition(
                candidate_id=candidate.id,
                symbol=MINT,
                provider="jupiter",
                side="LONG",
                entry_price=Decimal("0.001"),
                quantity=Decimal("100000"),
                entry_at=datetime.now(timezone.utc),
            )
        )
        await session.commit()

    resp = await client.get(f"/api/candidates/{candidate.id}", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["detail"] == {"ratio": 3.5}
    assert body["latest_signal"]["decision"] == "WAIT"
    assert body["latest_risk_event"]["approved"] is True
    assert body["paper_position"]["side"] == "LONG"


async def test_get_candidate_detail_with_no_related_rows(app, client, auth_headers):
    candidate = await _seed_candidate(app)

    resp = await client.get(f"/api/candidates/{candidate.id}", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["latest_signal"] is None
    assert body["latest_risk_event"] is None
    assert body["paper_position"] is None


async def test_malformed_state_history_is_normalised_not_passed_through(app, client, auth_headers):
    """state_history is a JSONB column, so nothing at the database level
    guarantees its shape — an entry written by an older build, a migration
    or a manual fix can differ. It used to be typed `list[Any]` and
    forwarded verbatim, and the dashboard called `.replace()` on
    `entry.state`; one such row raised an uncaught TypeError and
    white-screened the whole candidate detail page (reproduced in a real
    browser during the audit). The API must normalise instead.
    """
    candidate = await _seed_candidate(app)
    async with app.state.db_session_factory() as session:
        row = await session.get(TradingCandidate, candidate.id)
        row.state_history = [
            {"to": "discovered", "at": "2026-01-01"},  # legacy/unknown shape: no "state" key
            {"state": "observing", "at": "2026-01-02", "reason": "ok"},
        ]
        await session.commit()

    resp = await client.get(f"/api/candidates/{candidate.id}", headers=auth_headers)

    assert resp.status_code == 200
    history = resp.json()["state_history"]
    assert len(history) == 2
    # Every entry is guaranteed to carry a string `state`, so no consumer
    # can trip over a missing key.
    assert all(isinstance(entry["state"], str) for entry in history)
    assert history[0]["state"] == "unknown"
    assert history[1]["state"] == "observing"
    assert history[1]["reason"] == "ok"
