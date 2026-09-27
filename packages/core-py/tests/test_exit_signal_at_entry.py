"""EXIT_SIGNAL_AT_ENTRY: the gate never opens a position that the existing
exit intelligence would start selling on its first tick (production case
3eSai…pump: bought, then REDUCE 15 s later on "volume collapsed to 8% of the
previous 120s window; sellers outnumber buyers 2:1")."""

import os
from dataclasses import replace

import pytest_asyncio
from redis.asyncio import from_url

from yonixalpha_core.safety.gate import assess
from yonixalpha_core.safety.models import FinalDecision
from yonixalpha_core.safety.settings import default_settings_for
from yonixalpha_core.solana.assembler import Controls, Sources, assemble_fresh
from yonixalpha_core.testing.pump import MINT, FakeRpc, empty_account, seed_fading_flow, seed_healthy_launch

from tests.test_pump_pipeline import NOW
from tests.test_safety_gate import healthy

FRESH = default_settings_for("solana_fresh")
PRODUCTION_REASONS = ["volume collapsed to 8% of the previous 120s window", "sellers outnumber buyers 2:1"]


@pytest_asyncio.fixture
async def redis():
    r = from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/9"), decode_responses=True)
    await r.flushdb()
    yield r
    await r.flushdb()
    await r.aclose()


def test_reduce_at_entry_turns_an_executable_buy_into_wait():
    base = healthy(engine="solana_fresh")
    assert assess(base, FRESH).executable  # the same token, without the check, would be bought
    a = assess(replace(base, entry_exit_check={"action": "REDUCE", "reasons": PRODUCTION_REASONS, "metrics": {}}), FRESH)
    assert a.decision == FinalDecision.WAIT and not a.executable and not a.qualified
    f = next(f for f in a.findings if f.code == "EXIT_SIGNAL_AT_ENTRY")
    assert f.action == FinalDecision.WAIT and "volume collapsed" in f.message and "REDUCE" in f.message


def test_exit_and_emergency_verdicts_also_block_and_hold_does_not():
    base = healthy(engine="solana_fresh")
    for action in ("EXIT", "EXIT_NOW"):
        a = assess(replace(base, entry_exit_check={"action": action, "reasons": ["x"], "metrics": {}}), FRESH)
        assert "EXIT_SIGNAL_AT_ENTRY" in {f.code for f in a.findings} and not a.executable
    a = assess(replace(base, entry_exit_check={"action": "HOLD", "reasons": [], "metrics": {}}), FRESH)
    assert a.executable and "EXIT_SIGNAL_AT_ENTRY" not in {f.code for f in a.findings}


async def test_assembler_runs_the_existing_exit_rules_on_pre_entry_flow(redis):
    curve = await seed_healthy_launch(redis, NOW)
    c = Controls(settings=FRESH, account=empty_account())
    inp, ev = await assemble_fresh(Sources(redis, FakeRpc(curve)), MINT, NOW, c)
    assert inp.entry_exit_check["action"] == "HOLD" and assess(inp, FRESH).executable

    at = await seed_fading_flow(redis, curve, NOW)
    inp, ev = await assemble_fresh(Sources(redis, FakeRpc(curve)), MINT, at, c)
    chk = inp.entry_exit_check
    assert chk["action"] == "REDUCE" and ev["entry_exit_check"] == chk
    assert chk["reasons"][0].startswith("volume collapsed to") and chk["reasons"][1] == "sellers outnumber buyers 2:1"
    assert chk["metrics"]["volume_collapse"] is True and chk["metrics"]["sellers"] == 2 and chk["metrics"]["buyers"] == 1
    a = assess(inp, FRESH)
    assert "EXIT_SIGNAL_AT_ENTRY" in {f.code for f in a.findings} and not a.executable
