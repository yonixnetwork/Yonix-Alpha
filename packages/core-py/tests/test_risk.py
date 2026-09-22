from datetime import datetime, timedelta, timezone
from decimal import Decimal

from yonixalpha_core.risk import DataQuality, RiskConfig, RiskContext, evaluate

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _config(**overrides) -> RiskConfig:
    defaults = dict(trading_enabled=True, live_trading_enabled=True)
    defaults.update(overrides)
    return RiskConfig(**defaults)


def _context(**overrides) -> RiskContext:
    defaults = dict(now=NOW, data_quality=DataQuality.HEALTHY)
    defaults.update(overrides)
    return RiskContext(**defaults)


def test_fully_permissive_config_approves():
    verdict = evaluate(_config(), _context())
    assert verdict.approved is True
    assert verdict.reasons == []


def test_kill_switch_rejects_regardless_of_everything_else():
    verdict = evaluate(_config(), _context(kill_switch_engaged=True))
    assert verdict.approved is False
    assert "kill switch engaged" in verdict.reasons[0]


def test_trading_disabled_rejects():
    verdict = evaluate(_config(trading_enabled=False), _context())
    assert verdict.approved is False
    assert any("TRADING_ENABLED" in r for r in verdict.reasons)


def test_live_trading_disabled_rejects():
    verdict = evaluate(_config(live_trading_enabled=False), _context())
    assert verdict.approved is False
    assert any("LIVE_TRADING_ENABLED" in r for r in verdict.reasons)


def test_stale_data_rejects():
    verdict = evaluate(_config(), _context(data_quality=DataQuality.STALE))
    assert verdict.approved is False


def test_unavailable_data_rejects():
    verdict = evaluate(_config(), _context(data_quality=DataQuality.UNAVAILABLE))
    assert verdict.approved is False


def test_degraded_data_does_not_reject_by_itself():
    """DEGRADED is a caller/signal-confidence concern, not an automatic
    risk rejection — only STALE/UNAVAILABLE are.
    """
    verdict = evaluate(_config(), _context(data_quality=DataQuality.DEGRADED))
    assert verdict.approved is True


def test_max_position_size_enforced():
    verdict = evaluate(
        _config(max_position_size=Decimal("100")),
        _context(proposed_position_size=Decimal("150")),
    )
    assert verdict.approved is False
    assert any("max_position_size" in r for r in verdict.reasons)


def test_max_position_size_at_limit_is_allowed():
    verdict = evaluate(
        _config(max_position_size=Decimal("100")),
        _context(proposed_position_size=Decimal("100")),
    )
    assert verdict.approved is True


def test_max_position_size_none_means_unenforced():
    verdict = evaluate(_config(max_position_size=None), _context(proposed_position_size=Decimal("999999")))
    assert verdict.approved is True


def test_max_portfolio_exposure_includes_proposed_size():
    verdict = evaluate(
        _config(max_portfolio_exposure=Decimal("1000")),
        _context(current_portfolio_exposure=Decimal("900"), proposed_position_size=Decimal("200")),
    )
    assert verdict.approved is False
    assert any("portfolio exposure" in r for r in verdict.reasons)


def test_max_daily_loss_enforced():
    verdict = evaluate(
        _config(max_daily_loss=Decimal("500")),
        _context(daily_realized_pnl=Decimal("-500")),
    )
    assert verdict.approved is False
    assert any("max_daily_loss" in r for r in verdict.reasons)


def test_max_daily_loss_not_hit_when_still_positive():
    verdict = evaluate(
        _config(max_daily_loss=Decimal("500")),
        _context(daily_realized_pnl=Decimal("-100")),
    )
    assert verdict.approved is True


def test_max_open_positions_enforced_at_limit():
    verdict = evaluate(_config(max_open_positions=3), _context(open_position_count=3))
    assert verdict.approved is False


def test_max_open_positions_below_limit_allowed():
    verdict = evaluate(_config(max_open_positions=3), _context(open_position_count=2))
    assert verdict.approved is True


def test_max_slippage_enforced():
    verdict = evaluate(
        _config(max_slippage_bps=Decimal("50")),
        _context(proposed_slippage_bps=Decimal("75")),
    )
    assert verdict.approved is False


def test_max_slippage_unenforced_when_not_proposed():
    """Config sets a limit but the caller has no slippage estimate to
    offer (e.g. a market order with no quote yet) — never enforced
    against a None the caller was honest about not having.
    """
    verdict = evaluate(_config(max_slippage_bps=Decimal("50")), _context(proposed_slippage_bps=None))
    assert verdict.approved is True


def test_min_liquidity_enforced():
    verdict = evaluate(
        _config(min_liquidity=Decimal("10000")),
        _context(available_liquidity=Decimal("5000")),
    )
    assert verdict.approved is False


def test_max_price_impact_enforced():
    verdict = evaluate(
        _config(max_price_impact_bps=Decimal("100")),
        _context(proposed_price_impact_bps=Decimal("150")),
    )
    assert verdict.approved is False


def test_max_leverage_enforced():
    verdict = evaluate(_config(max_leverage=10), _context(proposed_leverage=20))
    assert verdict.approved is False


def test_max_leverage_at_limit_allowed():
    verdict = evaluate(_config(max_leverage=10), _context(proposed_leverage=10))
    assert verdict.approved is True


def test_cooldown_after_loss_rejects_within_window():
    verdict = evaluate(
        _config(cooldown_after_loss_seconds=3600),
        _context(last_loss_at=NOW - timedelta(seconds=1000)),
    )
    assert verdict.approved is False
    assert any("cooldown" in r for r in verdict.reasons)


def test_cooldown_after_loss_allows_after_window_elapses():
    verdict = evaluate(
        _config(cooldown_after_loss_seconds=3600),
        _context(last_loss_at=NOW - timedelta(seconds=4000)),
    )
    assert verdict.approved is True


def test_multiple_violations_all_collected_not_just_first():
    verdict = evaluate(
        _config(max_position_size=Decimal("100"), max_open_positions=1),
        _context(proposed_position_size=Decimal("200"), open_position_count=5),
    )
    assert verdict.approved is False
    assert len(verdict.reasons) >= 2


# ---------------------------------------------------------------------------
# Audit regression: a configured limit whose input is unknown must REJECT.
#
# Previously proposed_position_size/current_portfolio_exposure/
# daily_realized_pnl defaulted to Decimal(0), so a caller that could not
# measure them (every caller in this codebase today) silently satisfied
# every corresponding limit. An operator following docs/DEPLOYMENT.md's
# "enabling live trading" checklist would set MAX_POSITION_SIZE and
# MAX_DAILY_LOSS and believe capital was capped, while neither check could
# ever fire. Unknown is now distinct from zero, and unknown loses.
# ---------------------------------------------------------------------------


def test_unknown_position_size_rejects_when_max_position_size_configured():
    verdict = evaluate(_config(max_position_size=Decimal("1000")), _context())
    assert verdict.approved is False
    assert any("cannot determine the proposed position size" in r for r in verdict.reasons)


def test_unknown_daily_pnl_rejects_when_max_daily_loss_configured():
    verdict = evaluate(_config(max_daily_loss=Decimal("500")), _context())
    assert verdict.approved is False
    assert any("cannot determine today's realized PnL" in r for r in verdict.reasons)


def test_unknown_exposure_rejects_when_max_portfolio_exposure_configured():
    verdict = evaluate(_config(max_portfolio_exposure=Decimal("5000")), _context())
    assert verdict.approved is False
    assert any("cannot determine current exposure" in r for r in verdict.reasons)


def test_unconfigured_limits_still_approve_when_inputs_unknown():
    """Not knowing a value is only fatal when a limit depends on it."""
    verdict = evaluate(_config(), _context())
    assert verdict.approved is True


def test_zero_is_still_a_real_measured_value_not_unknown():
    """An honestly-measured zero must pass a configured limit, proving the
    fix distinguishes 'measured 0' from 'unknown'.
    """
    verdict = evaluate(
        _config(max_position_size=Decimal("1000"), max_daily_loss=Decimal("500"), max_portfolio_exposure=Decimal("5000")),
        _context(
            proposed_position_size=Decimal("0"),
            daily_realized_pnl=Decimal("0"),
            current_portfolio_exposure=Decimal("0"),
        ),
    )
    assert verdict.approved is True, verdict.reasons
