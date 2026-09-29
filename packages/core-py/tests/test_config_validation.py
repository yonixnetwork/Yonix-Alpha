"""Per-module configuration validation: missing config blocks only the
module that needs it, and only for what it is asked to do. (The futures /
FX / grid modules were removed with those features.)"""

from types import SimpleNamespace

from pydantic import SecretStr

from yonixalpha_core.config_validation import CONFIG_ERROR, DISABLED, READY, blocking_errors, validate
from yonixalpha_core.safety.models import GlobalMode, StrategyMode as M

LOCKS = dict(TRADING_ENABLED=True, LIVE_TRADING_ENABLED=True, PAPER_TRADING=False)
RPC = dict(SOLANA_RPC_URL="https://rpc.example", SOLANA_WS_URL="wss://rpc.example")


def env(**kw):
    return SimpleNamespace(**{**LOCKS, **kw})


def test_only_the_solana_modules_and_alerts_are_validated():
    assert set(validate(env(), {}, GlobalMode.PAPER)) == {"solana_fresh", "solana_migration", "solana_momentum", "alerts"}


def test_paper_needs_only_data_config_and_disabled_modules_never_block():
    r = validate(env(), {"solana_fresh": M.PAPER, "solana_migration": M.OFF}, GlobalMode.PAPER)
    assert r["solana_fresh"]["status"] == CONFIG_ERROR and "SOLANA_RPC_URL is not set" in r["solana_fresh"]["errors"]
    assert r["solana_migration"]["status"] == DISABLED and r["solana_migration"]["errors"] == []
    ok = validate(env(**RPC), {"solana_fresh": M.PAPER}, GlobalMode.PAPER)
    assert ok["solana_fresh"]["status"] == READY  # the wallet is needed only to trade live
    assert "WALLET_PRIVATE_KEY is not set" in ok["solana_fresh"]["live_missing"]
    assert blocking_errors(ok, "solana_fresh") == []


def test_live_requirements_apply_only_to_modules_asked_to_trade_live():
    modes = {"solana_fresh": M.AUTO, "solana_migration": M.PAPER, "solana_momentum": M.OFF}
    r = validate(env(**RPC), modes, GlobalMode.LIVE)
    assert r["solana_fresh"]["status"] == CONFIG_ERROR and "WALLET_PRIVATE_KEY is not set" in r["solana_fresh"]["errors"]
    assert r["solana_migration"]["status"] == READY
    assert r["solana_momentum"]["status"] == DISABLED
    assert validate(env(**RPC), modes, GlobalMode.PAPER)["solana_fresh"]["status"] == READY


def test_messages_name_variables_never_values():
    secret = "not-a-valid-key-" + "ab" * 24
    r = validate(env(WALLET_PRIVATE_KEY=SecretStr(secret), TELEGRAM_BOT_TOKEN="tok123"),
                 {"solana_fresh": M.AUTO}, GlobalMode.LIVE)
    text = str(r)
    assert secret not in text and "tok123" not in text
    assert r["solana_fresh"]["status"] == CONFIG_ERROR
    assert r["alerts"]["status"] == CONFIG_ERROR
