"""The local backtest runner: candles on disk with provenance, a batch run
whose bundle and report carry every assumption, exact reproducibility,
verification, and an import that refuses anything it cannot reproduce."""

import json
import os
import uuid
import zipfile
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from yonixalpha_crypto.backtest import jobs, local, policy
from yonixalpha_crypto.db import BacktestBatch, BacktestRun

from tests.test_backtest import FakeMD
from tests.test_library import walk


def _write(series, path, symbol, interval="1h", exchange="binance_us"):
    rows = [(t, repr(o), repr(h), repr(lo), repr(c), repr(v))
            for t, o, h, lo, c, v in zip(series.times, series.o, series.h, series.l, series.c, series.v)]
    return local.write_candles(path / f"{local.file_stem(exchange, symbol, interval)}.csv", rows,
                               {"exchange": exchange, "symbol": symbol, "interval": interval,
                                "source": "test candles", "host": "test"})


@pytest.fixture
def batch(tmp_path):
    btc = walk([(700, 0.001), (700, -0.001)], seed=8)
    eth = walk([(700, -0.001), (700, 0.0012)], seed=9)
    eth.times = btc.times
    _write(btc, tmp_path, "BTC/USDT")
    _write(eth, tmp_path, "ETH/USDT")
    cfg = local.resolve_config({
        "name": "test batch", "exchange": "binance_us", "symbols": ["BTC/USDT", "ETH/USDT"], "interval": "1h",
        "start": btc.times[0].isoformat(), "end": (btc.times[-1] + timedelta(hours=1)).isoformat(),
        "strategies": ["donchian_breakout", "rsi_reversion", "meta_muse"],
        "costs": {"taker_fee_bps": 2, "maker_fee_bps": 0, "spread_bps": 2, "slippage_bps": 5},
        "risk": {"mode": "PERCENT", "value": 0.5, "starting_equity": 10000}, "optimize": False,
        "params": {"donchian_breakout": {"entry_len": 30}}})
    local._read_file.cache_clear()
    return cfg, tmp_path


def test_every_run_and_the_report_carry_provenance_costs_config_code_and_periods(batch):
    cfg, data_dir = batch
    b = local.run_batch(cfg, data_dir, progress=lambda _m: None)
    assert b["code_version"] == jobs.code_version() and b["runner"]["place"].startswith("local")
    assert len(b["runs"]) == 5 and {r["status"] for r in b["runs"]} == {"DONE"}
    for r in b["runs"]:
        assert r["config"]["costs_effective"]["taker_fee_bps"] == 2
        assert r["config"]["costs_effective"]["slippage_bps"] == 5 and r["config"]["risk_effective"]["value"] == 0.5
        assert r["data"]["file_crc32"] and r["data"]["source"] == "test candles" and r["data"]["bars"] == 1400
        assert set(r["test_periods"]) == {"train", "validation", "out_of_sample"}
        assert r["result"]["data_quality"]["coverage_pct"] == 100.0 and r["summary"]["tier"]
    mm = next(r for r in b["runs"] if r["strategy_id"] == "meta_muse")
    assert mm["symbol"] == "ETH/USDT" and mm["data"]["lead"]["symbol"] == "BTC/USDT"
    dc = next(r for r in b["runs"] if r["strategy_id"] == "donchian_breakout")
    assert dc["config"]["param_source"] == "config" and any("non-default" in x for x in dc["result"]["verdict"]["reasons"])
    md = local.report_md(b)
    for needle in (b["content_crc"], b["code_version"], "taker_fee_bps | 2", "slippage_bps | 5", "test candles",
                   "Out-of-sample", "Win rate is never used to rank"):
        assert needle in md, needle


def test_the_same_code_and_data_reproduce_the_same_bundle_and_verify_says_so(batch, tmp_path):
    cfg, data_dir = batch
    b1 = local.run_batch(cfg, data_dir, progress=lambda _m: None)
    b2 = local.run_batch(cfg, data_dir, jobs_n=2, progress=lambda _m: None)  # worker processes give the same result
    assert b1["content_crc"] == b2["content_crc"]
    bp, rp = local.write_bundle(b1, tmp_path / "out")
    stored = json.loads(bp.read_text())
    assert local.content_crc(stored) == b1["content_crc"] and rp.exists()
    assert local.verify(stored, data_dir) == []
    # a changed candle file is detected before anything is re-run
    f = data_dir / stored["data"]["BTC/USDT"]["file"]
    f.write_text(f.read_text() + "\n")
    local._read_file.cache_clear()
    assert any("changed since the run" in p for p in local.verify(stored, data_dir))
    # an edited result breaks the checksum
    stored["runs"][0]["summary"]["oos_expectancy_r"] = 9.9
    assert any("checksum" in p for p in local.verify(stored, data_dir))


def test_config_is_validated(batch):
    cfg, _ = batch
    with pytest.raises(local.LocalError, match="unknown parameter"):
        local.resolve_config({**cfg, "params": {"sma_cross": {"nope": 1}}})
    with pytest.raises(local.LocalError, match="outside"):
        local.resolve_config({**cfg, "params": {"sma_cross": {"fast": 100000}}})
    with pytest.raises(local.LocalError, match="costs"):
        local.resolve_config({**cfg, "costs": {"taker_fee_bps": 500}})
    with pytest.raises(local.LocalError, match="unknown strategies"):
        local.resolve_config({**cfg, "strategies": ["nope"]})


async def test_fetch_writes_closed_candles_with_provenance(tmp_path):
    s = walk([(50, 0.001)], seed=1)
    md = FakeMD({"BTC/USDT": s})
    now = s.times[-1] + timedelta(minutes=30)  # the last candle is still forming
    cfg = local.resolve_config({"exchange": "binance_us", "symbols": ["BTC/USDT"], "interval": "1h",
                                "start": s.times[0].isoformat(), "end": now.isoformat()})
    metas = await local.fetch(cfg, tmp_path, md_factory=lambda ex: md, now=now)
    assert metas[0]["rows"] == 49 and metas[0]["closed_candles_only"] and metas[0]["file_crc32"]
    meta = json.loads((tmp_path / "binance_us_BTCUSDT_1h.meta.json").read_text())
    assert meta["source"] == "binance_us public klines API" and meta["fetched_at"]


def test_binance_public_data_files_are_converted_with_their_names_and_checksums(tmp_path):
    t0 = 1704067200000  # 2024-01-01 00:00 UTC in milliseconds
    ms_rows = "\n".join(f"{t0 + k * 3600_000},1,2,0.5,1.5,10,0,0,0,0,0,0" for k in range(3))
    zpath = tmp_path / "BTCUSDT-1h-2024-01.zip"
    with zipfile.ZipFile(zpath, "w") as z:
        z.writestr("BTCUSDT-1h-2024-01.csv", ms_rows)
    us = (t0 + 2 * 3600_000) * 1000  # files since 2025 use microseconds; overlapping row is de-duplicated
    cpath = tmp_path / "BTCUSDT-1h-2025-01.csv"
    cpath.write_text("open_time,open,high,low,close,volume\n" + "\n".join(
        f"{us + k * 3600_000_000},1,2,0.5,1.5,10,0,0,0,0,0,0" for k in range(3)))
    m = local.convert_binance_vision([str(zpath), str(cpath)], symbol="BTC/USDT", interval="1h",
                                     data_dir=tmp_path / "data")
    assert m["rows"] == 5 and m["first"].startswith("2024-01-01T00:00") and m["last"].startswith("2024-01-01T04:00")
    assert [x["file"] for x in m["source_files"]] == [zpath.name, cpath.name] and m["host"] == "data.binance.vision"
    local._read_file.cache_clear()
    ser, prov = local.load_series(tmp_path / "data", "binance", "BTC/USDT", "1h",
                                  datetime(2024, 1, 1, tzinfo=timezone.utc), datetime(2024, 1, 1, 6, tzinfo=timezone.utc))
    assert len(ser) == 5 and prov["coverage_pct"] == pytest.approx(83.33) and prov["gaps"] == 0


async def test_import_is_local_only_until_verified_and_refuses_what_it_cannot_reproduce(batch, sf, tmp_path):
    cfg, data_dir = batch
    bundle = json.loads(local.write_bundle(local.run_batch(cfg, data_dir, progress=lambda _m: None),
                                           tmp_path / "out")[0].read_text())
    assert policy.RESULT_IMPORT_ENABLED is False
    with pytest.raises(local.LocalError, match="switched off"):  # refused before any connection is made
        await local._import_cli(str(tmp_path / "out" / "x.json"), "postgresql+asyncpg://u:p@postgres:5432/prod")
    batch_id = await local.import_bundle(sf, bundle)
    async with sf() as s:
        b = await s.get(BacktestBatch, uuid.UUID(batch_id))
        assert b.config["source"] == "local-runner" and b.config["bundle_crc"] == bundle["content_crc"]
        runs = (await s.execute(select(BacktestRun).where(BacktestRun.batch_id == b.id))).scalars().all()
        assert len(runs) == 5 and all(r.code_version == bundle["code_version"] and r.data["file_crc32"] for r in runs)
    with pytest.raises(local.LocalError, match="already imported"):
        await local.import_bundle(sf, bundle)
    other = {**bundle, "code_version": "src-00000000"}
    other["content_crc"] = local.content_crc(other)
    with pytest.raises(local.LocalError, match="made with code"):
        await local.import_bundle(sf, other)
    assert os.environ["DATABASE_URL"]  # the test database is local


def test_the_cli_plans_and_runs(batch, tmp_path, capsys):
    cfg, data_dir = batch
    cpath = tmp_path / "batch.json"
    cpath.write_text(json.dumps(cfg))
    assert local.main(["plan", "--config", str(cpath), "--data", str(data_dir)]) == 0
    out = capsys.readouterr().out
    assert "5 runs planned" in out and "BTC/USDT: present" in out
    assert local.main(["run", "--config", str(cpath), "--data", str(data_dir), "--out", str(tmp_path / "o")]) == 0
    bp = next((tmp_path / "o").glob("bundle-*.json"))
    assert local.main(["verify", "--bundle", str(bp), "--data", str(data_dir)]) == 0
    assert "REPRODUCED" in capsys.readouterr().out
