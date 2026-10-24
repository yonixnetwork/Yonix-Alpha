"""The large-buy observation loop runs whatever the trading mode, and a
failure in it is logged without stopping the scanner."""

import asyncio

import pytest

from app import main


async def _run(monkeypatch, session_factory, redis, process):
    from yonixalpha_core import large_trades

    async def track(session, r, now=None):
        return {"completed": 0, "incomplete": 0}

    monkeypatch.setattr(large_trades, "process", process)
    monkeypatch.setattr(large_trades, "track", track)
    stop = asyncio.Event()
    task = asyncio.create_task(main._large_trade_loop(redis, session_factory, stop))
    await asyncio.sleep(0.3)
    stop.set()
    await asyncio.wait_for(task, 5)


@pytest.mark.asyncio
async def test_the_loop_processes_candidates(session_factory, redis, monkeypatch):
    calls = []

    async def process(session, r, now=None):
        calls.append(1)
        return {"candidates": 0, "events": 0, "controls": 0, "rejected": 0}

    await _run(monkeypatch, session_factory, redis, process)
    assert calls == [1]


@pytest.mark.asyncio
async def test_a_failure_never_stops_the_loop(session_factory, redis, monkeypatch):
    async def process(session, r, now=None):
        raise RuntimeError("boom")

    await _run(monkeypatch, session_factory, redis, process)  # returns normally when stopped
