"""Host resource readings and the resource level (low-resource operation,
2026-10-08: a 2 GB / 2 vCPU droplet with a 16 GB database).

Read from /proc, which inside a Docker container (no lxcfs) shows the host:
memory and swap (/proc/meminfo), load average (/proc/loadavg), CPU busy
share between two calls (/proc/stat), swap and major-fault activity
(/proc/vmstat) and, where the kernel provides it, Linux pressure-stall
information (/proc/pressure/{cpu,memory,io}: the share of time tasks waited
for that resource, averaged by the kernel itself).

`level()` turns a reading into NORMAL / WARNING / CRITICAL with the reasons,
from thresholds in Settings (RESOURCE_*). CRITICAL pauses priority-3 work
only (copy trading, ML training, heavy historical analytics); nothing here
is ever read by execution, position management, exits or the risk engine.
A missing /proc file reads as None, never as 0.
"""

from __future__ import annotations

import os
import shutil
import time
from typing import Any

NORMAL, WARNING, CRITICAL, UNKNOWN = "NORMAL", "WARNING", "CRITICAL", "UNKNOWN"

_prev_cpu: tuple[float, int, int] | None = None  # (monotonic, busy, total) of the previous /proc/stat reading
_prev_vm: tuple[float, dict[str, int]] | None = None


def _read(path: str) -> str | None:
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None


def _meminfo(text: str | None) -> dict[str, int]:
    out: dict[str, int] = {}
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1].isdigit():
            out[parts[0].rstrip(":")] = int(parts[1])  # kB
    return out


def _pressure(text: str | None) -> dict[str, dict[str, float]] | None:
    """`some avg10=1.23 avg60=... total=...` lines -> {"some": {"avg10": 1.23, ...}}."""
    if not text:
        return None
    out: dict[str, dict[str, float]] = {}
    for line in text.splitlines():
        kind, *fields = line.split()
        vals = {}
        for f in fields:
            k, _, v = f.partition("=")
            if k.startswith("avg"):
                try:
                    vals[k] = float(v)
                except ValueError:
                    pass
        out[kind] = vals
    return out


def _cpu_busy_pct(stat: str | None) -> float | None:
    """Busy share of all CPUs since the previous call in this process (the
    first call has no previous reading: None)."""
    global _prev_cpu
    if not stat:
        return None
    first = stat.splitlines()[0].split()
    if first[0] != "cpu":
        return None
    vals = [int(x) for x in first[1:]]
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)  # idle + iowait
    total = sum(vals[:8])
    now = time.monotonic()
    prev, _prev_cpu = _prev_cpu, (now, total - idle, total)
    if prev is None or total <= prev[2]:
        return None
    return round(100 * ((total - idle) - prev[1]) / (total - prev[2]), 1)


def _vm_rates(vmstat: str | None) -> dict[str, float] | None:
    """Pages swapped in / out and major page faults per second since the
    previous call (None on the first)."""
    global _prev_vm
    if not vmstat:
        return None
    cur = {}
    for line in vmstat.splitlines():
        k, _, v = line.partition(" ")
        if k in ("pswpin", "pswpout", "pgmajfault") and v.strip().isdigit():
            cur[k] = int(v)
    now = time.monotonic()
    prev, _prev_vm = _prev_vm, (now, cur)
    if prev is None or now <= prev[0]:
        return None
    dt = now - prev[0]
    return {f"{k}_per_s": round((cur[k] - prev[1].get(k, cur[k])) / dt, 1) for k in cur}


def sample() -> dict[str, Any]:
    """One reading of the host. Cheap: a handful of small /proc files."""
    mem = _meminfo(_read("/proc/meminfo"))
    load = (_read("/proc/loadavg") or "").split()
    cpus = os.cpu_count() or 1
    try:
        disk = shutil.disk_usage("/")
        disk_d = {"total_gb": round(disk.total / 2**30, 1), "used_gb": round(disk.used / 2**30, 1),
                  "free_gb": round(disk.free / 2**30, 1), "used_pct": round(100 * disk.used / disk.total, 1)}
    except OSError:
        disk_d = None

    def mb(key: str) -> int | None:
        return mem[key] // 1024 if key in mem else None

    swap_total, swap_free = mb("SwapTotal"), mb("SwapFree")
    return {
        "at": time.time(),
        "cpus": cpus,
        "memory": {"total_mb": mb("MemTotal"), "available_mb": mb("MemAvailable"), "free_mb": mb("MemFree"),
                   "used_mb": (mb("MemTotal") - mb("MemAvailable")) if mem.get("MemTotal") and "MemAvailable" in mem
                   else None,
                   "swap_total_mb": swap_total,
                   "swap_used_mb": (swap_total - swap_free) if swap_total is not None and swap_free is not None else None},
        "load": {"1m": float(load[0]), "5m": float(load[1]), "15m": float(load[2])} if len(load) >= 3 else None,
        "cpu_busy_pct": _cpu_busy_pct(_read("/proc/stat")),
        "vm": _vm_rates(_read("/proc/vmstat")),
        "pressure": {k: _pressure(_read(f"/proc/pressure/{k}")) for k in ("cpu", "memory", "io")},
        "disk": disk_d,
    }


def level(s: dict[str, Any], settings: Any) -> tuple[str, list[str]]:
    """NORMAL / WARNING / CRITICAL and every threshold crossed (UNKNOWN when
    nothing could be read)."""
    crit: list[str] = []
    warn: list[str] = []
    avail = (s.get("memory") or {}).get("available_mb")
    if avail is not None:
        if avail < settings.RESOURCE_CRITICAL_AVAILABLE_MB:
            crit.append(f"available memory {avail} MB < {settings.RESOURCE_CRITICAL_AVAILABLE_MB} MB")
        elif avail < settings.RESOURCE_WARN_AVAILABLE_MB:
            warn.append(f"available memory {avail} MB < {settings.RESOURCE_WARN_AVAILABLE_MB} MB")
    load1 = (s.get("load") or {}).get("1m")
    if load1 is not None:
        per = load1 / max(1, s.get("cpus") or 1)
        if per >= settings.RESOURCE_CRITICAL_LOAD_PER_CPU:
            crit.append(f"load {load1:.2f} = {per:.2f} per CPU >= {settings.RESOURCE_CRITICAL_LOAD_PER_CPU}")
        elif per >= settings.RESOURCE_WARN_LOAD_PER_CPU:
            warn.append(f"load {load1:.2f} = {per:.2f} per CPU >= {settings.RESOURCE_WARN_LOAD_PER_CPU}")
    mp = (((s.get("pressure") or {}).get("memory") or {}).get("some") or {}).get("avg60")
    if mp is not None:
        if mp >= settings.RESOURCE_CRITICAL_MEMORY_PRESSURE_PCT:
            crit.append(f"memory pressure {mp}% of the last minute >= {settings.RESOURCE_CRITICAL_MEMORY_PRESSURE_PCT}%")
        elif mp >= settings.RESOURCE_WARN_MEMORY_PRESSURE_PCT:
            warn.append(f"memory pressure {mp}% of the last minute >= {settings.RESOURCE_WARN_MEMORY_PRESSURE_PCT}%")
    if avail is None and load1 is None:
        return UNKNOWN, ["host readings unavailable"]
    if crit:
        return CRITICAL, crit + warn
    return (WARNING, warn) if warn else (NORMAL, [])


def copy_resume_check(s: dict[str, Any], db_latency_ms: float | None, settings: Any) -> tuple[bool, list[str]]:
    """Whether copy trading may be resumed now (Settings.COPY_*). Every
    failed condition is listed; an unreadable value counts as failed."""
    why: list[str] = []
    m = s.get("memory") or {}
    avail, swap = m.get("available_mb"), m.get("swap_used_mb")
    load1 = (s.get("load") or {}).get("1m")
    if avail is None or avail < settings.COPY_RESUME_MIN_FREE_RAM_MB:
        why.append(f"available memory {avail if avail is not None else 'unknown'} MB, needs >= "
                   f"{settings.COPY_RESUME_MIN_FREE_RAM_MB} MB")
    if load1 is None or load1 / max(1, s.get("cpus") or 1) > settings.COPY_MAX_CPU_LOAD:
        per = None if load1 is None else round(load1 / max(1, s.get("cpus") or 1), 2)
        why.append(f"load per CPU {per if per is not None else 'unknown'}, needs <= {settings.COPY_MAX_CPU_LOAD}")
    if swap is None or swap > settings.COPY_MAX_SWAP_USAGE_MB:
        why.append(f"swap used {swap if swap is not None else 'unknown'} MB, needs <= {settings.COPY_MAX_SWAP_USAGE_MB} MB")
    if db_latency_ms is None or db_latency_ms > settings.COPY_MAX_DB_LATENCY_MS:
        why.append(f"database round trip {db_latency_ms if db_latency_ms is not None else 'unknown'} ms, needs <= "
                   f"{settings.COPY_MAX_DB_LATENCY_MS} ms")
    return not why, why


def process_usage() -> dict[str, Any]:
    """This process's resident memory and CPU time (for its heartbeat)."""
    import resource as _r

    rss = None
    for line in (_read("/proc/self/status") or "").splitlines():
        if line.startswith("VmRSS:"):
            rss = round(int(line.split()[1]) / 1024, 1)
    u = _r.getrusage(_r.RUSAGE_SELF)
    return {"rss_mb": rss, "peak_rss_mb": round(u.ru_maxrss / 1024, 1), "cpu_s": round(u.ru_utime + u.ru_stime, 1)}
