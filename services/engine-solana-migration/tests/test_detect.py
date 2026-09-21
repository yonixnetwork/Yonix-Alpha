import pytest

from app import detect


@pytest.fixture(autouse=True)
def _clean_registry():
    """detect._PARSERS is module-level state; isolate each test."""
    saved = dict(detect._PARSERS)
    detect._PARSERS.clear()
    yield
    detect._PARSERS.clear()
    detect._PARSERS.update(saved)


def test_configured_program_ids_empty_by_default(monkeypatch):
    monkeypatch.delenv("MIGRATION_AMM_PROGRAM_IDS", raising=False)
    assert detect.configured_program_ids() == {}


def test_configured_program_ids_parses_name_address_pairs(monkeypatch):
    monkeypatch.setenv("MIGRATION_AMM_PROGRAM_IDS", "raydium_v4:Addr1111111111111111111111111111111111111,orca:Addr2222222222222222222222222222222222222")
    result = detect.configured_program_ids()
    assert result == {
        "Addr1111111111111111111111111111111111111": "raydium_v4",
        "Addr2222222222222222222222222222222222222": "orca",
    }


def test_configured_program_ids_skips_malformed_entries(monkeypatch, caplog):
    monkeypatch.setenv("MIGRATION_AMM_PROGRAM_IDS", "not-a-valid-entry,raydium_v4:Addr1111111111111111111111111111111111111")
    result = detect.configured_program_ids()
    assert result == {"Addr1111111111111111111111111111111111111": "raydium_v4"}


def test_has_parser_false_when_unregistered():
    assert detect.has_parser("SomeProgram111111111111111111111111111111") is False


def test_has_parser_true_after_registration():
    detect.register_parser("SomeProgram111111111111111111111111111111", lambda tx: None)
    assert detect.has_parser("SomeProgram111111111111111111111111111111") is True


def test_extract_pool_initialization_returns_none_for_unregistered_program():
    assert detect.extract_pool_initialization("Unregistered1111111111111111111111111111", {}) is None


def test_extract_pool_initialization_dispatches_to_registered_parser():
    calls = []

    def fake_parser(tx_result):
        calls.append(tx_result)
        return {"pool_address": "Pool111111111111111111111111111111111111", "token_mint": "Mint11111111111111111111111111111111111"}

    detect.register_parser("FakeAmm1111111111111111111111111111111111", fake_parser)
    result = detect.extract_pool_initialization("FakeAmm1111111111111111111111111111111111", {"some": "tx"})

    assert calls == [{"some": "tx"}]
    assert result["pool_address"] == "Pool111111111111111111111111111111111111"


def test_extract_pool_initialization_parser_can_return_none():
    """A registered parser inspecting a transaction that doesn't actually
    contain a pool init (e.g. some other instruction on the same program)
    returns None — must propagate cleanly, not be mistaken for "no parser".
    """
    detect.register_parser("FakeAmm1111111111111111111111111111111111", lambda tx: None)
    assert detect.extract_pool_initialization("FakeAmm1111111111111111111111111111111111", {}) is None
