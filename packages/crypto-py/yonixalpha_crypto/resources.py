"""This process's memory and its container limit (cgroup v2, else v1).
Read only; used to pause new entries under pressure."""

from __future__ import annotations

from pathlib import Path


def _read(path: str) -> str | None:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def memory() -> dict:
    rss_mb = None
    status = _read("/proc/self/status") or ""
    for line in status.splitlines():
        if line.startswith("VmRSS:"):
            rss_mb = round(int(line.split()[1]) / 1024, 1)
    limit = _read("/sys/fs/cgroup/memory.max") or _read("/sys/fs/cgroup/memory/memory.limit_in_bytes")
    limit_mb = None
    if limit and limit.isdigit() and int(limit) < 1 << 50:  # "max" or a huge v1 value means no limit
        limit_mb = round(int(limit) / 1048576, 1)
    return {"rss_mb": rss_mb, "limit_mb": limit_mb}
