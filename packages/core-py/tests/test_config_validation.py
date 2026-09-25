"""Per-module configuration validation: missing config blocks only the
module that needs it, and only for what it is asked to do."""

from types import SimpleNamespace

from pydantic import SecretStr

from yonixalpha_core.config_validation import CONFIG_ERROR, DISABLED, READY, blocking_errors, validate
from yonixalpha_core.safety.models import GlobalMode, StrategyMode as M

LOCKS = dict(TRADING_ENABLED=True, LIVE_TRADING_ENABLED=True, PAPER_TRADING=False)


def env(**kw):
    return SimpleNamespace(**{**LOCKS, **kw})


def test_paper_needs_only_data_config_and_disabled_modules_never_block():
    r = validate(env(), {"solana_fresh": M.PAPER, "solana_migration": M.OFF}, GlobalMode.PAPER)
    assert r["solana_fresh"]["status"] == CONFIG_ERROR and "SOLANA_RPC_URL is not set" in r["solana_fresh"]["errors"]
    assert r["solana_migration"]["status"] == DISABLED and r["solana_migration"]["errors"] == []
    assert r["meta_muse"]["status"] == READY  # Binance public data needs no key in paper
    assert "BINANCE_API_KEY is not set" in r["meta_muse"]["live_missing"]
    assert blocking_errors(r, "meta_muse") == []


def test_live_requirements_apply_only_to_modules_asked_to_trade_live():
    modes = {"meta_muse": M.AUTO, "binance_futures": M.AUTO, "gold_btc_trend": M.PAPER, "confluence_matrix": M.OFF}
    r = validate(env(), modes, GlobalMode.LIVE)
    assert r["meta_muse"]["status"] == CONFIG_ERROR and "BINANCE_API_SECRET is not set" in r["meta_muse"]["errors"]
    assert r["gold_btc_trend"]["status"] == READY
    ok = validate(env(BINANCE_API_KEY="k", BINANCE_API_SECRET=SecretStr("s"), BINANCE_TESTNET=True), modes, GlobalMode.LIVE)
    assert ok["meta_muse"]["status"] == READY and "TESTNET" in ok["meta_muse"]["warnings"][0]


def test_venue_choice_changes_requirements_and_formats_are_checked():
    r = validate(env(MT5_BRIDGE_URL="bridge:9100", MT5_BRIDGE_TOKEN=SecretStr("short")), {"confluence_matrix": M.PAPER},
                 GlobalMode.PAPER, {"confluence_matrix": "mt5"})
    errs = r["confluence_matrix"]["errors"]
    assert any("http://" in e for e in errs) and any("at least 32" in e for e in errs)
    hl = validate(env(HYPERLIQUID_ACCOUNT_ADDRESS="0x123", HYPERLIQUID_API_WALLET_PRIVATE_KEY=SecretStr("zz")),
                  {"hyperliquid_grid": M.AUTO, "hyperliquid_perps": M.AUTO}, GlobalMode.LIVE)
    assert len(hl["hyperliquid_grid"]["errors"]) == 2


def test_venue_mode_off_disables_the_strategy_module():
    r = validate(env(), {"meta_muse": M.AUTO, "binance_futures": M.OFF}, GlobalMode.LIVE)
    assert r["meta_muse"]["status"] == DISABLED


def test_messages_name_variables_never_values():
    secret = "0x" + "ab" * 32
    r = validate(env(HYPERLIQUID_ACCOUNT_ADDRESS="nope", HYPERLIQUID_API_WALLET_PRIVATE_KEY=SecretStr(secret),
                     TELEGRAM_BOT_TOKEN="tok123", META_MUSE_CONTROL_URL="http://x"),
                 {"hyperliquid_grid": M.AUTO, "hyperliquid_perps": M.AUTO}, GlobalMode.LIVE)
    text = str(r)
    assert secret not in text and "tok123" not in text
    assert r["alerts"]["status"] == CONFIG_ERROR and r["control_apis"]["status"] == CONFIG_ERROR
