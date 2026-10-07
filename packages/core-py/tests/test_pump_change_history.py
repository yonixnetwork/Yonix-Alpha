"""The read-only Pump change history: admin instruction names, program
upgrade slots and the failure-window mark."""
import asyncio
import base64
import struct

from solders.pubkey import Pubkey

from yonixalpha_core.solana import pump_tx as p
from yonixalpha_core.solana.codec import b58encode
from yonixalpha_core.tools import pump_change_history as h


def test_instruction_names_match_the_pump_amm_idl():
    # pump-swap-sdk 1.20.0 IDL discriminator of updateBuybackConfig
    assert h.anchor_disc("update_buyback_config") == bytes([251, 224, 171, 146, 160, 26, 113, 233])
    assert h.NAMES[bytes([251, 224, 171, 146, 160, 26, 113, 233])] == "update_buyback_config"


def test_program_data_slot_and_window_mark():
    assert h.last_deploy_slot(struct.pack("<IQ", 3, 454_000_000) + b"\x01" + bytes(32)) == 454_000_000
    assert h.last_deploy_slot(struct.pack("<IQ", 2, 1)) is None
    assert "inside the sell-failure window" in h.when(int(h.FAILURE_WINDOW[0].timestamp()) + 60)
    assert "window" not in h.when(int(h.FAILURE_WINDOW[1].timestamp()) + 3600) and h.when(None) == "time unknown"


def test_lists_upgrades_and_names_admin_transactions(monkeypatch, capsys):
    admin = Pubkey.new_unique()
    cfg = p.GLOBAL_CONFIG_DISC + bytes(admin) + bytes(600)
    upgrade_at = int(h.FAILURE_WINDOW[0].timestamp()) + 2640  # 22:44
    tx = {"version": 0, "transaction": {"message": {"accountKeys": [], "instructions": [
        {"programId": p.PUMP_AMM, "accounts": [], "data": b58encode(h.anchor_disc("update_buyback_config"))}]}},
        "meta": {"innerInstructions": []}}

    class Rpc:
        def replace_endpoints(self, specs):
            pass

        async def call(self, method, params, priority=None):
            if method == "getAccountInfo":
                data = cfg if params[0] == p.amm_global_config_pda() else struct.pack("<IQ", 3, 7) + b"\x00"
                return {"value": {"data": [base64.b64encode(data).decode(), "base64"]}}
            if method == "getBlockTime":
                return upgrade_at
            if method == "getSignaturesForAddress":
                return [{"signature": "s" * 64, "err": None, "blockTime": upgrade_at}]
            if method == "getTransaction":
                return tx
            raise AssertionError(method)

    class Engine:
        async def dispose(self):
            pass

    class Session:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *a):
            return False

    async def effective_rpc(session, settings):
        return [{"url": "http://fake"}]

    monkeypatch.setattr(h, "get_settings", lambda: None)
    monkeypatch.setattr(h, "make_engine", lambda s: Engine())
    monkeypatch.setattr(h, "make_session_factory", lambda e: Session)
    monkeypatch.setattr(h, "effective_rpc", effective_rpc)
    monkeypatch.setattr(h.RpcManager, "create", classmethod(lambda cls, **kw: Rpc()))
    assert asyncio.run(h.main([])) == 0
    out = capsys.readouterr().out
    assert "PumpSwap (pump-amm): slot 7, 2026-10-06 22:44:00 UTC  <- inside the sell-failure window" in out
    assert f"GlobalConfig admin {admin}" in out and "update_buyback_config" in out
    assert "Nothing was signed or sent." in out
