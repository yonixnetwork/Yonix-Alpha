from datetime import datetime, timedelta, timezone

from yonixalpha_core.db.models import Token, TokenEvent

from app.momentum import WINDOW_SECONDS, compute_transfer_acceleration

MINT = "TestMint111111111111111111111111111111111"


async def _make_token(db_session) -> Token:
    token = Token(mint_address=MINT, first_seen_source="test")
    db_session.add(token)
    await db_session.flush()
    return token


async def _add_transfer_event(db_session, token_id, occurred_at, signature):
    db_session.add(
        TokenEvent(
            token_id=token_id,
            event_type="transfer",
            source="test",
            occurred_at=occurred_at,
            signature=signature,
            payload={},
        )
    )


async def test_acceleration_counts_events_in_correct_windows(db_session):
    token = await _make_token(db_session)
    now = datetime.now(timezone.utc)
    window = timedelta(seconds=WINDOW_SECONDS)

    # 2 events in the prior window, 6 in the current window
    for i in range(2):
        await _add_transfer_event(db_session, token.id, now - window - timedelta(seconds=i + 1), f"prior-{i}")
    for i in range(6):
        await _add_transfer_event(db_session, token.id, now - timedelta(seconds=i + 1), f"current-{i}")
    await db_session.commit()

    result = await compute_transfer_acceleration(db_session, token.id, now)
    assert result.current_count == 6
    assert result.prior_count == 2
    assert result.ratio == 3.0
    assert result.is_accelerating is True


async def test_acceleration_ignores_events_outside_both_windows(db_session):
    token = await _make_token(db_session)
    now = datetime.now(timezone.utc)
    window = timedelta(seconds=WINDOW_SECONDS)

    await _add_transfer_event(db_session, token.id, now - 5 * window, "ancient")  # way outside
    for i in range(6):
        await _add_transfer_event(db_session, token.id, now - timedelta(seconds=i + 1), f"current-{i}")
    await db_session.commit()

    result = await compute_transfer_acceleration(db_session, token.id, now)
    assert result.current_count == 6
    assert result.prior_count == 0


async def test_acceleration_zero_prior_gives_undefined_ratio_not_infinite():
    from app.momentum import AccelerationResult

    result = AccelerationResult(current_count=10, prior_count=0)
    assert result.ratio is None
    assert result.is_accelerating is False  # undefined ratio is never treated as "accelerating"


async def test_acceleration_below_min_events_not_accelerating_even_with_high_ratio():
    from app.momentum import AccelerationResult

    result = AccelerationResult(current_count=3, prior_count=1)  # ratio 3.0 but below MIN_CURRENT_WINDOW_EVENTS
    assert result.ratio == 3.0
    assert result.is_accelerating is False


async def test_acceleration_below_ratio_threshold_not_accelerating():
    from app.momentum import AccelerationResult

    result = AccelerationResult(current_count=10, prior_count=8)  # ratio 1.25, well below threshold
    assert result.is_accelerating is False


async def test_acceleration_no_events_at_all(db_session):
    token = await _make_token(db_session)
    await db_session.commit()
    now = datetime.now(timezone.utc)

    result = await compute_transfer_acceleration(db_session, token.id, now)
    assert result.current_count == 0
    assert result.prior_count == 0
    assert result.ratio is None
