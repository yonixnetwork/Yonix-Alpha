"""The LOCAL backtest runner: historical backtests run on your own computer,
on downloaded public candles, never on the production server
(backtest.policy). Results are exported as a reproducible bundle that can be
re-checked (`verify`) and, once the workflow is verified, imported into
YonixAlpha (`import`).

    python -m yonixalpha_crypto.backtest.local example-config > batch.json
    python -m yonixalpha_crypto.backtest.local fetch --config batch.json --data data
    python -m yonixalpha_crypto.backtest.local convert --symbol BTC/USDT --interval 1h --data data FILES...
    python -m yonixalpha_crypto.backtest.local plan --config batch.json --data data
    python -m yonixalpha_crypto.backtest.local run --config batch.json --data data --out results [--jobs 2]
    python -m yonixalpha_crypto.backtest.local verify --bundle results/bundle-....json --data data
    python -m yonixalpha_crypto.backtest.local import --bundle results/bundle-....json [--database-url URL]

Every bundle and report records: data provenance (source, host or files,
checksum, first / last candle, gaps, coverage), fees, spread, slippage and
the rest of the cost model, the risk model, the full configuration and
parameters, the code version, and the train / validation / out-of-sample
periods of every run. The same inputs and code give the same bundle
checksum (`content_crc`).

Checksums are CRC32: they detect a changed file or result, they are not a
security signature."""

from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
import platform
import subprocess
import sys
import zipfile
import zlib
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from yonixalpha_crypto.backtest import data, jobs, policy, run
from yonixalpha_crypto.candles import INTERVAL_SECONDS
from yonixalpha_crypto.strategies.library import BY_ID, Series

FORMAT, FORMAT_VERSION = "yonixalpha-backtest-bundle", 1
CSV_HEADER = ["open_time", "open", "high", "low", "close", "volume"]

EXAMPLE_CONFIG: dict[str, Any] = {
    "name": "first batch: all strategies, 1h, two years",
    "exchange": "binance_us",
    "symbols": ["BTC/USDT", "ETH/USDT", "SOL/USDT"],
    "interval": "1h",
    "start": "2024-10-01T00:00:00Z",
    "end": "2026-10-01T00:00:00Z",
    "strategies": "all",
    "costs": {"taker_fee_bps": 2, "maker_fee_bps": 0, "spread_bps": 2, "slippage_bps": 5,
              "max_participation": 0.1, "funding_bps_per_day": 0, "min_notional": 5},
    "costs_note": "Binance.US published spot base tier (0 maker / 2 bps taker): an assumption, not account-verified",
    "risk": {"mode": "PERCENT", "value": 0.5, "starting_equity": 10000},
    "optimize": True,
    "params": {},
    "exits": {},
}


class LocalError(RuntimeError):
    pass


def _utc(s: str) -> datetime:
    d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _crc_bytes(b: bytes) -> str:
    return f"{zlib.crc32(b):08x}"


def _default(v: Any) -> Any:
    return v.isoformat() if isinstance(v, datetime) else str(v)


def plain(obj: Any) -> Any:
    """Exactly what a JSON file will hold (dates as ISO text, tuples as
    lists), so a fresh run and a re-read bundle checksum the same."""
    return json.loads(json.dumps(obj, default=_default))


def canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=_default).encode()


def file_stem(exchange: str, symbol: str, interval: str) -> str:
    return f"{exchange}_{symbol.replace('/', '')}_{interval}"


# ---- configuration -------------------------------------------------------

def load_config(path: str | Path) -> dict[str, Any]:
    cfg = json.loads(Path(path).read_text())
    return resolve_config(cfg)


def resolve_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """Validate and fill defaults, so the bundle records every assumption."""
    need = {"exchange", "symbols", "interval", "start", "end"}
    if need - set(cfg):
        raise LocalError(f"config is missing {sorted(need - set(cfg))}")
    if cfg["interval"] not in INTERVAL_SECONDS:
        raise LocalError(f"interval {cfg['interval']!r} not supported")
    start, end = _utc(cfg["start"]), _utc(cfg["end"])
    if end <= start:
        raise LocalError("end must be after start")
    expected = int((end - start).total_seconds() // INTERVAL_SECONDS[cfg["interval"]])
    if expected > data.MAX_BARS:
        raise LocalError(f"{expected} bars per symbol; the limit is {data.MAX_BARS}")
    strategies = list(BY_ID) if cfg.get("strategies", "all") == "all" else list(cfg["strategies"])
    unknown = [s for s in strategies if s not in BY_ID]
    if unknown:
        raise LocalError(f"unknown strategies: {unknown}")
    try:
        costs = run.costs_from(cfg.get("costs"))
        risk = run.risk_from(cfg.get("risk"))
        params = {sid: run.check_params(sid, p) for sid, p in (cfg.get("params") or {}).items()}
    except run.RunConfigError as e:
        raise LocalError(str(e)) from None
    if set(params) - set(BY_ID):
        raise LocalError(f"params for unknown strategies: {sorted(set(params) - set(BY_ID))}")
    return {**cfg, "start": start.isoformat(), "end": end.isoformat(), "strategies": strategies,
            "symbols": list(cfg["symbols"]), "costs": dict(costs.__dict__), "risk": dict(risk.__dict__),
            "optimize": bool(cfg.get("optimize", True)), "params": params, "exits": dict(cfg.get("exits") or {})}


def plan_runs(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """The same expansion as the database queue: one run per strategy and
    symbol; a pair strategy runs once (lead = first symbol, traded = last)."""
    out = []
    syms = cfg["symbols"]
    for sid in cfg["strategies"]:
        m = BY_ID[sid]
        for sym in ([syms[-1]] if m.needs_lead else syms):
            skip = None
            if m.needs_lead and len(syms) < 2:
                skip = "pair strategy: needs a leading and a traded symbol (e.g. BTC/USDT, ETH/USDT)"
            elif cfg["interval"] not in m.intervals:
                skip = f"{sid} supports {list(m.intervals)}, not {cfg['interval']}"
            params = cfg["params"].get(sid, {})
            out.append({"strategy_id": sid, "module_version": m.version, "symbol": sym,
                        "lead": syms[0] if m.needs_lead else None, "skip": skip,
                        "config": {"params": params, "exits": cfg["exits"].get(sid, m.default_exits),
                                   "costs": cfg["costs"], "risk": cfg["risk"], "optimize": cfg["optimize"],
                                   "lead": syms[0] if m.needs_lead else None, "module_version": m.version,
                                   "param_source": "config" if params else "defaults"}})
    return out


# ---- candles on disk -----------------------------------------------------

def write_candles(path: Path, rows: list[tuple[datetime, str, str, str, str, str]], meta: dict[str, Any]) -> dict:
    rows = sorted({r[0]: r for r in rows}.values())  # one row per open time, oldest first
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(CSV_HEADER)
    for t, o, h, lo, c, v in rows:
        w.writerow([t.astimezone(timezone.utc).isoformat(), o, h, lo, c, v])
    raw = buf.getvalue().encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    meta = {**meta, "file": path.name, "rows": len(rows), "file_crc32": _crc_bytes(raw),
            "first": rows[0][0].isoformat() if rows else None, "last": rows[-1][0].isoformat() if rows else None}
    path.with_suffix(".meta.json").write_text(json.dumps(meta, indent=2, sort_keys=True))
    return meta


async def fetch(cfg: dict[str, Any], data_dir: Path, *, md_factory=None, now: datetime | None = None) -> list[dict]:
    """Download closed candles for every symbol from the exchange's PUBLIC
    klines (no key). Run this on your own computer."""
    from yonixalpha_crypto.exchanges import market_data
    from yonixalpha_crypto.exchanges.profiles import profile

    now = now or datetime.now(timezone.utc)
    step = INTERVAL_SECONDS[cfg["interval"]]
    start, end = _utc(cfg["start"]), min(_utc(cfg["end"]), now)
    md = (md_factory or market_data)(cfg["exchange"])
    metas = []
    try:
        for sym in cfg["symbols"]:
            got = await md.history(sym, cfg["interval"], int(start.timestamp() * 1000), int(end.timestamp() * 1000))
            closed = [c for c in got if c.open_time + timedelta(seconds=step) <= now and start <= c.open_time < end]
            rows = [(c.open_time, str(c.open), str(c.high), str(c.low), str(c.close), str(c.volume)) for c in closed]
            host = profile(cfg["exchange"]).base_url if md_factory is None else getattr(md, "base_url", "test")
            metas.append(write_candles(data_dir / f"{file_stem(cfg['exchange'], sym, cfg['interval'])}.csv", rows, {
                "exchange": cfg["exchange"], "symbol": sym, "interval": cfg["interval"],
                "source": f"{cfg['exchange']} public klines API", "host": host,
                "requested": {"start": start.isoformat(), "end": end.isoformat()},
                "fetched_at": now.isoformat(), "closed_candles_only": True}))
    finally:
        await md.aclose()
    return metas


def _vision_rows(name: str, raw: bytes) -> list[tuple]:
    rows = []
    for rec in csv.reader(io.StringIO(raw.decode())):
        if not rec or not rec[0].strip().isdigit():
            continue  # header line in newer files
        ts = int(rec[0])
        ts_s = ts / 1_000_000 if ts > 10**14 else ts / 1000  # microseconds since 2025, milliseconds before
        rows.append((datetime.fromtimestamp(ts_s, tz=timezone.utc), rec[1], rec[2], rec[3], rec[4], rec[5]))
    if not rows:
        raise LocalError(f"{name}: no kline rows found")
    return rows


def convert_binance_vision(files: list[str], *, symbol: str, interval: str, data_dir: Path,
                           exchange: str = "binance") -> dict:
    """Load Binance public data files (data.binance.vision spot klines, .zip
    or .csv) into the runner's candle file, with their names and checksums
    as provenance."""
    rows, sources = [], []
    for f in files:
        p = Path(f)
        raw = p.read_bytes()
        sources.append({"file": p.name, "crc32": _crc_bytes(raw)})
        if p.suffix == ".zip":
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                for n in z.namelist():
                    if n.endswith(".csv"):
                        rows += _vision_rows(n, z.read(n))
        else:
            rows += _vision_rows(p.name, raw)
    return write_candles(data_dir / f"{file_stem(exchange, symbol, interval)}.csv", rows, {
        "exchange": exchange, "symbol": symbol, "interval": interval,
        "source": "data.binance.vision public spot klines (downloaded files)", "host": "data.binance.vision",
        "source_files": sources, "converted_at": datetime.now(timezone.utc).isoformat(),
        "closed_candles_only": True})


@lru_cache(maxsize=16)
def _read_file(path: str) -> tuple[bytes, list[tuple]]:
    raw = Path(path).read_bytes()
    rows = []
    for rec in csv.DictReader(io.StringIO(raw.decode())):
        rows.append((_utc(rec["open_time"]), float(rec["open"]), float(rec["high"]), float(rec["low"]),
                     float(rec["close"]), float(rec["volume"])))
    return raw, rows


def load_series(data_dir: Path, exchange: str, symbol: str, interval: str, start: datetime,
                end: datetime) -> tuple[Series, dict[str, Any]]:
    path = data_dir / f"{file_stem(exchange, symbol, interval)}.csv"
    if not path.exists():
        raise LocalError(f"no candle file {path} (run `fetch` or `convert` first)")
    raw, rows = _read_file(str(path))
    meta_path = path.with_suffix(".meta.json")
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    sel = [r for r in rows if start <= r[0] < end]
    step = timedelta(seconds=INTERVAL_SECONDS[interval])
    gaps = [(a[0].isoformat(), b[0].isoformat()) for a, b in zip(sel, sel[1:]) if b[0] - a[0] > step]
    series = Series([r[1] for r in sel], [r[2] for r in sel], [r[3] for r in sel], [r[4] for r in sel],
                    [r[5] for r in sel], [r[0] for r in sel])
    expected = int((end - start).total_seconds() // INTERVAL_SECONDS[interval])
    prov = {"exchange": exchange, "symbol": symbol, "interval": interval, "bars": len(sel),
            "expected_bars": expected, "coverage_pct": round(100 * len(sel) / expected, 2) if expected else None,
            "first": sel[0][0].isoformat() if sel else None, "last": sel[-1][0].isoformat() if sel else None,
            "gaps": len(gaps), "gap_examples": gaps[:5], "file": path.name, "file_crc32": _crc_bytes(raw),
            "source": meta.get("source"), "host": meta.get("host"), "source_files": meta.get("source_files"),
            "fetched_at": meta.get("fetched_at") or meta.get("converted_at"),
            "note": "public exchange klines; closed candles only"}
    return series, prov


# ---- running -------------------------------------------------------------

def _git_commit() -> str | None:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5,
                              cwd=Path(__file__).parent).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def _one(args: tuple[dict, str, dict]) -> dict[str, Any]:
    """One run (top level so it can run in a worker process)."""
    spec, data_dir, cfg = args
    base = {k: spec[k] for k in ("strategy_id", "module_version", "symbol", "lead")}
    base.update(exchange=cfg["exchange"], interval=cfg["interval"])
    if spec["skip"]:
        return {**base, "status": "SKIPPED", "error": spec["skip"], "config": spec["config"], "data": None,
                "result": None, "summary": None, "test_periods": None}
    start, end = _utc(cfg["start"]), _utc(cfg["end"])
    try:
        series, prov = load_series(Path(data_dir), cfg["exchange"], spec["symbol"], cfg["interval"], start, end)
        if spec["lead"]:
            lead, lprov = load_series(Path(data_dir), cfg["exchange"], spec["lead"], cfg["interval"], start, end)
            _, series = data.align(lead, series)
            prov = {**prov, "lead": lprov, "aligned_bars": len(series)}
        out = run.execute(series, prov, spec["strategy_id"], cfg["interval"], spec["config"], prov["expected_bars"])
    except LocalError as e:
        return {**base, "status": "FAILED", "error": str(e), "config": spec["config"], "data": None,
                "result": None, "summary": None, "test_periods": None}
    res = out["result"]
    return {**base, "status": out["status"], "error": out["error"], "data": prov, "result": res,
            "summary": out["summary"], "test_periods": res.get("splits"),
            "config": {**spec["config"], "costs_effective": out["costs_effective"],
                       "risk_effective": out["risk_effective"]}}


def content_crc(bundle: dict[str, Any]) -> str:
    """Checksum of everything that defines the result (not when or on which
    machine it was produced)."""
    core = {k: bundle[k] for k in ("format", "format_version", "code_version", "config", "data", "runs")}
    return _crc_bytes(canonical(core))


def run_batch(cfg: dict[str, Any], data_dir: Path, *, jobs_n: int = 1, progress=print) -> dict[str, Any]:
    specs = plan_runs(cfg)
    args = [(s, str(data_dir), cfg) for s in specs]
    if jobs_n > 1:
        with ProcessPoolExecutor(max_workers=jobs_n) as ex:
            runs = []
            for i, r in enumerate(ex.map(_one, args), 1):
                progress(f"[{i}/{len(args)}] {r['strategy_id']} {r['symbol']}: {r['status']}")
                runs.append(r)
    else:
        runs = []
        for i, a in enumerate(args, 1):
            r = _one(a)
            progress(f"[{i}/{len(args)}] {r['strategy_id']} {r['symbol']}: {r['status']}")
            runs.append(r)
    start, end = _utc(cfg["start"]), _utc(cfg["end"])
    data_prov = {}
    for sym in cfg["symbols"]:
        try:
            data_prov[sym] = load_series(data_dir, cfg["exchange"], sym, cfg["interval"], start, end)[1]
        except LocalError as e:
            data_prov[sym] = {"error": str(e)}
    bundle = {"format": FORMAT, "format_version": FORMAT_VERSION, "code_version": jobs.code_version(),
              "config": plain(cfg), "data": plain(data_prov), "runs": plain(runs),
              "created_at": datetime.now(timezone.utc).isoformat(),
              "runner": {"python": sys.version.split()[0], "platform": platform.platform(), "git_commit": _git_commit(),
                         "place": "local computer (never the production server)"}}
    bundle["content_crc"] = content_crc(bundle)
    return bundle


def report_md(b: dict[str, Any]) -> str:
    """A human-readable report carrying the same metadata as the bundle."""
    c = b["config"]
    lines = [f"# Backtest report: {c.get('name', 'batch')}", "",
             "Simulation on historical public candles. Not evidence of future returns; nothing here authorises "
             "live trading.", "",
             "## Reproducibility", "",
             f"- Bundle checksum (content_crc): `{b['content_crc']}`",
             f"- Code version: `{b['code_version']}`; git commit: `{b['runner'].get('git_commit')}`",
             f"- Produced: {b['created_at']} on {b['runner']['platform']} (Python {b['runner']['python']}), "
             f"{b['runner']['place']}",
             f"- Exchange data: {c['exchange']}; interval {c['interval']}; period {c['start']} to {c['end']}",
             f"- Strategies: {len(c['strategies'])}; symbols: {', '.join(c['symbols'])}; parameter grid "
             f"(train only): {'on' if c['optimize'] else 'off'}", "",
             "## Costs and risk (applied to every run)", "",
             "| Item | Value |", "|---|---|"]
    for k, v in c["costs"].items():
        lines.append(f"| {k} | {v} |")
    if c.get("costs_note"):
        lines.append(f"| note | {c['costs_note']} |")
    for k, v in c["risk"].items():
        lines.append(f"| risk.{k} | {v} |")
    lines += ["", "## Data provenance", "",
              "| Symbol | Source | Host / files | Candles | Expected | Coverage % | Gaps | First | Last | File CRC32 |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for sym, p in b["data"].items():
        if "error" in p:
            lines.append(f"| {sym} | missing: {p['error']} |||||||||")
            continue
        files = p.get("host") or ""
        if p.get("source_files"):
            files += f" ({len(p['source_files'])} files)"
        lines.append(f"| {sym} | {p.get('source')} | {files} | {p['bars']} | {p['expected_bars']} | "
                     f"{p['coverage_pct']} | {p['gaps']} | {p['first']} | {p['last']} | `{p['file_crc32']}` |")
    lines += ["", "## Results (out-of-sample period, the run's own parameters)", "",
              "Ranked by tier, then out-of-sample expectancy (R), then drawdown. Win rate is never used to rank.", "",
              "| Strategy | v | Symbol | Status | Tier | OOS trades | Expectancy R | PF | Max DD % | Return % | "
              "Buy & hold % | Train | Validation | Out-of-sample | Params |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]

    def key(r):
        k = (r.get("summary") or {}).get("rank_key") or [9, 0, 0]
        return (0 if r["status"] == "DONE" else 1, *k)

    def period(tp, k):
        return f"{tp[k]['from'][:10]}..{tp[k]['to'][:10]}" if tp and k in tp else ""

    for r in sorted(b["runs"], key=key):
        s, tp = r.get("summary") or {}, r.get("test_periods")
        lines.append(f"| {r['strategy_id']} | {r['module_version']} | {r['symbol']} | {r['status']} | "
                     f"{s.get('tier', r.get('error') or '')} | {s.get('oos_trades', '')} | "
                     f"{s.get('oos_expectancy_r', '')} | {s.get('oos_profit_factor', '')} | "
                     f"{s.get('oos_max_drawdown_pct', '')} | {s.get('oos_net_return_pct', '')} | "
                     f"{s.get('benchmark_oos_return_pct', '')} | {period(tp, 'train')} | {period(tp, 'validation')} | "
                     f"{period(tp, 'out_of_sample')} | {json.dumps(r['config'].get('params') or 'defaults')} |")
    return "\n".join(lines) + "\n"


def write_bundle(b: dict[str, Any], out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    name = f"bundle-{b['code_version']}-{b['content_crc']}"
    bp, rp = out_dir / f"{name}.json", out_dir / f"{name}.md"
    bp.write_text(json.dumps(b, indent=1, sort_keys=True, default=_default))
    rp.write_text(report_md(b))
    return bp, rp


def verify(bundle: dict[str, Any], data_dir: Path, *, jobs_n: int = 1) -> list[str]:
    """Re-run the bundle's configuration on the same files with this code.
    Returns the problems found ([] = reproduced exactly)."""
    problems = []
    if bundle.get("format") != FORMAT or bundle.get("format_version") != FORMAT_VERSION:
        return ["not a YonixAlpha backtest bundle (or an unsupported version)"]
    if content_crc(bundle) != bundle.get("content_crc"):
        problems.append("the bundle's content does not match its checksum (edited or damaged)")
    if bundle["code_version"] != jobs.code_version():
        problems.append(f"made with code {bundle['code_version']}, this code is {jobs.code_version()}: "
                        "check out the commit in runner.git_commit to reproduce it")
        return problems
    for sym, p in bundle["data"].items():
        f = data_dir / p.get("file", "")
        if "error" in p or not f.exists():
            problems.append(f"{sym}: candle file missing")
        elif _crc_bytes(f.read_bytes()) != p["file_crc32"]:
            problems.append(f"{sym}: candle file changed since the run ({p['file']})")
    if problems:
        return problems
    again = run_batch(resolve_config(bundle["config"]), data_dir, jobs_n=jobs_n, progress=lambda _m: None)
    if again["content_crc"] != bundle["content_crc"]:
        old = {(r["strategy_id"], r["symbol"]): canonical(r) for r in bundle["runs"]}
        diff = [f"{k[0]} {k[1]}" for k, v in ((
            (r["strategy_id"], r["symbol"]), canonical(r)) for r in again["runs"]) if old.get(k) != v]
        problems.append(f"re-run differs (content_crc {again['content_crc']}); runs that differ: {diff[:10]}")
    return problems


# ---- import (switched off until the workflow is verified) ----------------

def _local_database(url: str) -> bool:
    from sqlalchemy.engine import make_url
    return make_url(url).host in (None, "", "localhost", "127.0.0.1", "::1")


def check_bundle(bundle: dict[str, Any]) -> list[str]:
    """What an import refuses: anything not reproducible with this code."""
    if bundle.get("format") != FORMAT or bundle.get("format_version") != FORMAT_VERSION:
        return ["not a YonixAlpha backtest bundle (or an unsupported version)"]
    out = []
    if content_crc(bundle) != bundle.get("content_crc"):
        out.append("content does not match its checksum (edited or damaged)")
    if bundle["code_version"] != jobs.code_version():
        out.append(f"made with code {bundle['code_version']}; this installation runs {jobs.code_version()}")
    for r in bundle["runs"]:
        m = BY_ID.get(r["strategy_id"])
        if m is None:
            out.append(f"unknown strategy {r['strategy_id']}")
        elif m.version != r["module_version"]:
            out.append(f"{r['strategy_id']}: module version {r['module_version']}, installed {m.version}")
        if r["status"] == "DONE" and jobs.summary(r["result"]) != r["summary"]:
            out.append(f"{r['strategy_id']} {r['symbol']}: summary does not match its result")
    return out


async def import_bundle(sf, bundle: dict[str, Any], *, actor: str = "local-runner import") -> str:
    from sqlalchemy import select

    from yonixalpha_crypto.db import BacktestBatch, BacktestRun

    problems = check_bundle(bundle)
    if problems:
        raise LocalError("refused: " + "; ".join(problems))
    async with sf() as s:
        dup = (await s.execute(select(BacktestBatch.id).where(
            BacktestBatch.config["bundle_crc"].astext == bundle["content_crc"]))).first()
        if dup:
            raise LocalError(f"this bundle is already imported (batch {dup[0]})")
        c, now = bundle["config"], datetime.now(timezone.utc)
        created = _utc(bundle["created_at"])
        b = BacktestBatch(status="DONE", total=len(bundle["runs"]), created_at=created, created_by=actor,
                          finished_at=now, note=(c.get("name") or "")[:300],
                          config={"source": "local-runner", "bundle_crc": bundle["content_crc"],
                                  "code_version": bundle["code_version"], "runner": bundle["runner"],
                                  "strategies": c["strategies"], "symbols": c["symbols"], "interval": c["interval"],
                                  "exchange": c["exchange"], "start": c["start"], "end": c["end"],
                                  "costs": c["costs"], "risk": c["risk"], "optimize": c["optimize"],
                                  "data": bundle["data"]})
        s.add(b)
        await s.flush()
        for r in bundle["runs"]:
            s.add(BacktestRun(batch_id=b.id, strategy_id=r["strategy_id"], exchange=r["exchange"], symbol=r["symbol"],
                              interval=r["interval"], status=r["status"], config=r["config"],
                              code_version=bundle["code_version"], data=r["data"], summary=r["summary"],
                              result=r["result"], error=r["error"], created_at=created, finished_at=created))
        await s.commit()
        return str(b.id)


async def _import_cli(bundle_path: str, database_url: str | None) -> str:
    from yonixalpha_crypto.config import load_settings
    from yonixalpha_crypto.db import make_session_factory, schema_ready

    url = database_url or load_settings().database_url
    if not policy.RESULT_IMPORT_ENABLED and not _local_database(url):
        raise LocalError("importing results is switched off until the local-runner workflow is verified "
                         "(backtest.policy.RESULT_IMPORT_ENABLED); only a database on this computer (localhost) "
                         "can receive a bundle, for verification")
    engine, sf = make_session_factory(url)
    try:
        async with sf() as s:
            if not await schema_ready(s):
                raise LocalError("the crypto schema is not installed in that database "
                                 "(python -m yonixalpha_crypto.migrate upgrade)")
        return await import_bundle(sf, json.loads(Path(bundle_path).read_text()))
    finally:
        await engine.dispose()


# ---- command line --------------------------------------------------------

def _estimate(cfg: dict[str, Any], data_dir: Path) -> str:
    specs = plan_runs(cfg)
    n = sum(1 for s in specs if not s["skip"])
    expected = int((_utc(cfg["end"]) - _utc(cfg["start"])).total_seconds() // INTERVAL_SECONDS[cfg["interval"]])
    have = []
    for sym in cfg["symbols"]:
        f = data_dir / f"{file_stem(cfg['exchange'], sym, cfg['interval'])}.csv"
        have.append(f"{sym}: {'present' if f.exists() else 'MISSING'} ({f.name})")
    return (f"{len(specs)} runs planned ({n} to compute, {len(specs) - n} skipped); about {expected} candles per "
            f"symbol (limit {data.MAX_BARS}); roughly {max(1, round(n * expected / 17520 * 15 / 60))} minutes on one "
            "core, under 100 MB of memory (measured: about 15 s per run for 17,520 candles with the grid on)\n"
            + "\n".join(have))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m yonixalpha_crypto.backtest.local",
                                description="Local historical backtests (never on the production server)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("example-config")
    f = sub.add_parser("fetch")
    f.add_argument("--config", required=True)
    f.add_argument("--data", required=True)
    cv = sub.add_parser("convert")
    cv.add_argument("--symbol", required=True)
    cv.add_argument("--interval", required=True)
    cv.add_argument("--data", required=True)
    cv.add_argument("--exchange", default="binance")
    cv.add_argument("files", nargs="+")
    pl = sub.add_parser("plan")
    pl.add_argument("--config", required=True)
    pl.add_argument("--data", required=True)
    r = sub.add_parser("run")
    r.add_argument("--config", required=True)
    r.add_argument("--data", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--jobs", type=int, default=1)
    v = sub.add_parser("verify")
    v.add_argument("--bundle", required=True)
    v.add_argument("--data", required=True)
    v.add_argument("--jobs", type=int, default=1)
    im = sub.add_parser("import")
    im.add_argument("--bundle", required=True)
    im.add_argument("--database-url")
    a = p.parse_args(argv)
    try:
        if a.cmd == "example-config":
            print(json.dumps(EXAMPLE_CONFIG, indent=2))
        elif a.cmd == "fetch":
            for m in asyncio.run(fetch(load_config(a.config), Path(a.data))):
                print(f"{m['symbol']}: {m['rows']} candles {m['first']} .. {m['last']} -> {m['file']} "
                      f"(crc32 {m['file_crc32']})")
        elif a.cmd == "convert":
            m = convert_binance_vision(a.files, symbol=a.symbol, interval=a.interval, data_dir=Path(a.data),
                                       exchange=a.exchange)
            print(f"{m['symbol']}: {m['rows']} candles {m['first']} .. {m['last']} -> {m['file']}")
        elif a.cmd == "plan":
            print(_estimate(load_config(a.config), Path(a.data)))
        elif a.cmd == "run":
            cfg = load_config(a.config)
            bp, rp = write_bundle(run_batch(cfg, Path(a.data), jobs_n=max(1, a.jobs)), Path(a.out))
            print(f"bundle: {bp}\nreport: {rp}")
        elif a.cmd == "verify":
            problems = verify(json.loads(Path(a.bundle).read_text()), Path(a.data), jobs_n=max(1, a.jobs))
            print("REPRODUCED: the same code and data give the same results" if not problems
                  else "NOT REPRODUCED:\n- " + "\n- ".join(problems))
            return 0 if not problems else 1
        elif a.cmd == "import":
            print(f"imported as batch {asyncio.run(_import_cli(a.bundle, a.database_url))}")
    except LocalError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
