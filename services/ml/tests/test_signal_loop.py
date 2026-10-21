"""Signal Center loop in the ml service: it records and evaluates signals,
and a failure there is logged without stopping the service."""

import asyncio
import os

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from app import main
from yonixalpha_core.db.base import make_session_factory


async def _run(monkeypatch, gen, ev):
    monkeypatch.setattr(main.signal_center, "generate", gen)
    monkeypatch.setattr(main.signal_center, "evaluate", ev)
    sf = make_session_factory(create_async_engine(os.environ["DATABASE_URL"]))
    stop = asyncio.Event()
    task = asyncio.create_task(main._signal_loop(sf, stop))
    await asyncio.sleep(0.3)
    stop.set()
    await asyncio.wait_for(task, 5)


@pytest.mark.asyncio
async def test_the_loop_generates_and_evaluates(db_session, monkeypatch):
    calls = []

    async def gen(session):
        calls.append("generate")
        return {"signals": 0}

    async def ev(session):
        calls.append("evaluate")
        return {"evaluated": 0}

    await _run(monkeypatch, gen, ev)
    assert calls == ["generate", "evaluate"]


@pytest.mark.asyncio
async def test_a_failure_does_not_stop_the_loop(db_session, monkeypatch):
    async def gen(session):
        raise RuntimeError("boom")

    async def ev(session):
        return {"evaluated": 0}

    await _run(monkeypatch, gen, ev)  # returns normally when stopped
