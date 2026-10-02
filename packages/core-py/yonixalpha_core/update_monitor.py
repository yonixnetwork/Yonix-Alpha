"""GitHub / provider / dependency update monitor (master §64-66, M15).

Watches the repositories YonixAlpha integrates with or relies on, and its
critical Python dependencies. Uses only official APIs, never HTML:

  GitHub REST  GET /repos/{repo}/commits?per_page=1      newest commit
               GET /repos/{repo}/releases/latest         newest release (404: none)
               GET /repos/{repo}/compare/{old}...{new}   commits and files changed
  PyPI JSON    GET /pypi/{package}/json                  latest version
               GET /pypi/{package}/{installed}/json      known vulnerabilities of
                                                         the installed version

Classification (rule based, from commit / release text and changed paths;
read the diff before acting):

  SECURITY_UPDATE    security wording (security, vulnerability, CVE, GHSA,
                     exploit, advisory) or a vulnerable installed dependency
  BREAKING_CHANGE    "breaking" / "BREAKING CHANGE" / deprecation wording, a
                     major-version release, or a removed file on a path we use
  PROVIDER_CHANGE    a provider repository changed an API path or wording
  UPGRADE_AVAILABLE  a new release or a newer dependency version
  INFO               other changes
  ACTION_REQUIRED    any of the first three on something YonixAlpha uses
                     directly (an IDL / ABI / contract source, a pinned
                     dependency): the integration must be re-checked

Commits that change no files (repositories that refresh their activity date
with empty commits) are recorded but raise no event. The first check of a
watch is a baseline: stored, never notified. Nothing is ever deployed or
upgraded automatically: the monitor only notifies (master §66).
"""

from __future__ import annotations

import asyncio
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from importlib import metadata
from typing import Any

import httpx

from yonixalpha_core.logging import get_logger

log = get_logger("update_monitor")

INFO, UPGRADE, BREAKING, SECURITY, PROVIDER, ACTION = (
    "INFO", "UPGRADE_AVAILABLE", "BREAKING_CHANGE", "SECURITY_UPDATE", "PROVIDER_CHANGE", "ACTION_REQUIRED")
CLASSES = (ACTION, SECURITY, BREAKING, PROVIDER, UPGRADE, INFO)
NOTIFY = {ACTION: "critical", SECURITY: "critical", BREAKING: "warning", PROVIDER: "warning", UPGRADE: "info"}
TELEGRAM = {ACTION, SECURITY, BREAKING, PROVIDER}  # UPGRADE AVAILABLE: in-app only (routine, frequent)
CHECK_SECONDS = 6 * 3600
GITHUB = "https://api.github.com"
PYPI = "https://pypi.org/pypi"


@dataclass(frozen=True)
class Watch:
    key: str  # github:owner/repo | pypi:package
    category: str  # solana | bsc | robinhood | provider | dependency
    why: str
    used_directly: bool = False  # an IDL / ABI / contract source or a pinned dependency we run
    interest: tuple[str, ...] = ()  # path fragments whose change is an API change for us

    @property
    def kind(self) -> str:
        return self.key.split(":", 1)[0]

    @property
    def target(self) -> str:
        return self.key.split(":", 1)[1]


WATCHES: tuple[Watch, ...] = (
    Watch("github:pump-fun/pump-public-docs", "solana", "Pump.fun / PumpSwap program IDLs and docs", True, ("idl", ".json")),
    Watch("github:anza-xyz/agave", "solana", "Solana validator releases (RPC behaviour)"),
    Watch("github:helius-labs/helius-sdk", "provider", "Helius RPC / streaming provider SDK"),
    Watch("github:jito-labs/jito-ts", "provider", "Jito bundles / block engine client"),
    Watch("github:raydium-io/raydium-idl", "solana", "Raydium LaunchLab IDL (activity probe)", True, ("raydium_launchpad",)),
    Watch("github:MeteoraAg/dynamic-bonding-curve-sdk", "solana", "Meteora DBC IDL (activity probe)", True,
          ("idl/dynamic-bonding-curve",)),
    Watch("github:four-meme-community/four-meme-ai", "bsc", "Four.meme contracts, events and token modes", True,
          ("contract-addresses", "event-listening", "errors.md")),
    Watch("github:1chimaruGin/bsc-mempool", "bsc", "BSC mempool copy-trading reference"),
    Watch("github:ponsdotdev/ponsfamily", "robinhood", "Pons V1 / V2 contract source (events, ABI)", True,
          ("contractsV1", "contractsV2", "abi.json")),
    Watch("github:chainstacklabs/robinhood-chain-sequencer-feed", "robinhood",
          "Robinhood sequencer feed format, signer and compression", True, ("verify.py", "codec.py", "README")),
    Watch("github:OffchainLabs/nitro", "robinhood", "Nitro broadcast feed format (Robinhood Chain sequencer)"),
    Watch("github:0xProject/0x-settler", "provider", "0x settlement contracts"),
    Watch("github:madeonsol/madeonsol-sdk", "provider", "MadeOnSol API SDK (enrichment, not connected)"),
    Watch("github:nansen-ai/nansen-cli", "provider", "Nansen CLI / API (enrichment, not connected)"),
    Watch("pypi:solders", "dependency", "Solana transactions and signing", True),
    Watch("pypi:eth-account", "dependency", "EVM transaction decoding / signing", True),
    Watch("pypi:eth-abi", "dependency", "EVM ABI encoding / decoding", True),
    Watch("pypi:websockets", "dependency", "every WebSocket stream", True),
    Watch("pypi:httpx", "dependency", "every HTTP / RPC call", True),
    Watch("pypi:sqlalchemy", "dependency", "database access", True),
    Watch("pypi:cryptography", "dependency", "provider-secret encryption", True),
    Watch("pypi:coincurve", "dependency", "signature recovery", True),
)

_SECURITY = re.compile(r"\b(security|vulnerab\w*|cve-\d{4}-\d+|ghsa-[\w-]+|exploit\w*|advisory)\b", re.I)
_BREAKING = re.compile(r"(breaking[ _-]?change|\bbreaking\b|\bdeprecat\w+)", re.I)
_API = re.compile(r"\b(api|idl|abi|endpoint|instruction|event|schema|rpc)\b", re.I)
_PERF = re.compile(r"\b(perf|performance|latency|faster|speed\s?up|throughput)\b", re.I)


def _major(v: str | None) -> int | None:
    m = re.match(r"^[vV]?(\d+)", (v or "").strip())
    return int(m.group(1)) if m else None


@dataclass
class Change:
    """What changed since the previous check (inputs of classify)."""

    messages: list[str] = field(default_factory=list)
    files: list[dict[str, Any]] = field(default_factory=list)  # {"filename", "status"}
    commits: int = 0
    old_release: str | None = None
    new_release: str | None = None
    release_text: str = ""
    old_version: str | None = None  # dependency: installed
    new_version: str | None = None  # dependency: latest on PyPI
    vulnerabilities: list[dict[str, Any]] = field(default_factory=list)


def classify(w: Watch, ch: Change) -> tuple[str | None, dict[str, bool], dict[str, Any]]:
    """(classification or None when there is nothing to report, flags, summary)."""
    text = "\n".join(ch.messages + [ch.release_text])
    interest = [f["filename"] for f in ch.files if any(p in f["filename"] for p in w.interest)]
    removed = [f["filename"] for f in ch.files if f.get("status") == "removed" and f["filename"] in interest]
    new_release = ch.new_release and ch.new_release != ch.old_release
    newer_dep = ch.new_version and ch.old_version and ch.new_version != ch.old_version
    major_bump = (new_release and _major(ch.new_release) is not None and _major(ch.old_release) is not None
                  and _major(ch.new_release) > _major(ch.old_release)) or \
                 (newer_dep and (_major(ch.new_version) or 0) > (_major(ch.old_version) or 0))
    flags = {"security": bool(_SECURITY.search(text)) or bool(ch.vulnerabilities),
             "breaking": bool(_BREAKING.search(text)) or bool(major_bump) or bool(removed),
             "api": bool(interest) or (w.category == "provider" and bool(_API.search(text))),
             "performance": bool(_PERF.search(text))}
    reasons = []
    if ch.vulnerabilities:
        reasons.append(f"installed {ch.old_version} has {len(ch.vulnerabilities)} known vulnerabilities")
    if _SECURITY.search(text):
        reasons.append("security wording in commits / release")
    if major_bump:
        reasons.append("major version change")
    if removed:
        reasons.append(f"removed {len(removed)} file(s) on paths we use")
    if _BREAKING.search(text):
        reasons.append("breaking / deprecation wording")
    if interest:
        reasons.append(f"{len(interest)} changed file(s) on paths we use")
    if not (ch.files or new_release or newer_dep or ch.vulnerabilities):
        return None, flags, {"commits": ch.commits, "note": "no file changes (empty commits)"} if ch.commits else {}
    if flags["security"]:
        cls = SECURITY
    elif flags["breaking"]:
        cls = BREAKING
    elif w.category == "provider" and flags["api"]:
        cls = PROVIDER
    elif new_release or newer_dep:
        cls = UPGRADE
    else:
        cls = INFO
    if w.used_directly and (cls in (SECURITY, BREAKING) or interest):
        cls = ACTION
    summary = {"commits": ch.commits, "files_changed": len(ch.files), "files_on_paths_we_use": interest[:20],
               "messages": [m.splitlines()[0][:140] for m in ch.messages[:8]],
               "release": ch.new_release if new_release else None,
               "version": {"installed": ch.old_version, "latest": ch.new_version} if ch.new_version else None,
               "vulnerabilities": [{"id": v.get("id"), "fixed_in": v.get("fixed_in")} for v in ch.vulnerabilities][:10],
               "reasons": reasons, "basis": "keywords and changed paths; read the diff before acting"}
    return cls, flags, summary


# --- fetching ------------------------------------------------------------------------------------------

class RateLimited(Exception):
    pass


async def _get(client: httpx.AsyncClient, url: str, headers: dict[str, str]) -> Any | None:
    r = await client.get(url, headers=headers, timeout=20.0)
    if r.status_code == 404:
        return None
    if r.status_code in (403, 429) and (r.headers.get("x-ratelimit-remaining") == "0" or r.status_code == 429):
        raise RateLimited(f"rate limited ({r.status_code}); resets at {r.headers.get('x-ratelimit-reset')}")
    r.raise_for_status()
    return r.json()


def _github_headers(token: str | None) -> dict[str, str]:
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
         "User-Agent": "yonixalpha-update-monitor"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


async def fetch_github(client: httpx.AsyncClient, w: Watch, prev_commit: str | None, prev_release: str | None,
                       token: str | None) -> tuple[dict[str, Any], Change]:
    h = _github_headers(token)
    commits = await _get(client, f"{GITHUB}/repos/{w.target}/commits?per_page=1", h) or []
    head = commits[0] if commits else {}
    rel = await _get(client, f"{GITHUB}/repos/{w.target}/releases/latest", h)
    state = {"latest_commit": head.get("sha"),
             "latest_commit_at": ((head.get("commit") or {}).get("committer") or {}).get("date"),
             "latest_release": (rel or {}).get("tag_name")}
    ch = Change(old_release=prev_release, new_release=state["latest_release"],
                release_text=f"{(rel or {}).get('name') or ''}\n{(rel or {}).get('body') or ''}"[:4000])
    if prev_commit and state["latest_commit"] and state["latest_commit"] != prev_commit:
        cmp_ = await _get(client, f"{GITHUB}/repos/{w.target}/compare/{prev_commit}...{state['latest_commit']}", h) or {}
        ch.commits = int(cmp_.get("total_commits") or len(cmp_.get("commits") or []))
        ch.messages = [((c.get("commit") or {}).get("message") or "") for c in (cmp_.get("commits") or [])][-50:]
        ch.files = [{"filename": f.get("filename", ""), "status": f.get("status")} for f in (cmp_.get("files") or [])]
    return state, ch


def installed_version(package: str) -> str | None:
    try:
        return metadata.version(package)
    except metadata.PackageNotFoundError:
        return None


async def fetch_pypi(client: httpx.AsyncClient, w: Watch) -> tuple[dict[str, Any], Change]:
    h = {"Accept": "application/json", "User-Agent": "yonixalpha-update-monitor"}
    inst = installed_version(w.target)
    latest = ((await _get(client, f"{PYPI}/{w.target}/json", h)) or {}).get("info", {}).get("version")
    vulns = []
    if inst:
        vulns = ((await _get(client, f"{PYPI}/{w.target}/{inst}/json", h)) or {}).get("vulnerabilities") or []
    return ({"installed_version": inst, "latest_release": latest},
            Change(old_version=inst, new_version=latest, vulnerabilities=vulns))


# --- one pass ------------------------------------------------------------------------------------------

def _dt(v) -> datetime | None:
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")) if v else None
    except ValueError:
        return None


async def check_all(session, client: httpx.AsyncClient, now: datetime, token: str | None = None,
                    watches: tuple[Watch, ...] = WATCHES) -> list[dict[str, Any]]:
    """Checks every watch once; returns the new events (caller commits and
    notifies). A rate limit stops the pass (the rest keep their state)."""
    from yonixalpha_core.db.models import UpdateEvent, UpdateWatch

    events: list[dict[str, Any]] = []
    for w in watches:
        row = await session.get(UpdateWatch, w.key)
        baseline = row is None
        if row is None:
            row = UpdateWatch(key=w.key, category=w.category)
            session.add(row)
        try:
            if w.kind == "github":
                state, ch = await fetch_github(client, w, row.latest_commit, row.latest_release, token)
            else:
                state, ch = await fetch_pypi(client, w)
                ch.old_release = row.latest_release  # PyPI: report a new latest version only once
                if row.latest_release == state["latest_release"] and row.installed_version == state["installed_version"]:
                    ch.new_version = ch.old_version  # nothing new since the last check
                    known = {v.get("id") for v in ((row.change_summary or {}).get("vulnerabilities") or [])}
                    ch.vulnerabilities = [v for v in ch.vulnerabilities if v.get("id") not in known]
        except RateLimited as exc:
            row.error = str(exc)[:300]
            row.last_checked = now
            log.warning("update_monitor.rate_limited", key=w.key)
            break
        except Exception as exc:  # noqa: BLE001 - one failing watch never stops the others
            row.error = f"{type(exc).__name__}: {str(exc)[:200]}"
            row.last_checked = now
            continue
        cls, flags, summary = classify(w, ch)
        old_ref = row.latest_commit if w.kind == "github" else row.installed_version
        if w.kind == "github":
            if row.latest_commit and state["latest_commit"] != row.latest_commit:
                row.previous_commit = row.latest_commit
            row.latest_commit = state["latest_commit"]
            row.latest_commit_at = _dt(state.get("latest_commit_at"))
        else:
            row.installed_version = state["installed_version"]
        if state.get("latest_release") != row.latest_release:
            row.previous_release = row.latest_release
            row.latest_release = state.get("latest_release")
        row.last_checked, row.error = now, None
        if baseline:
            row.classification, row.flags = None, flags
            row.change_summary = {"note": "baseline: first check, nothing compared yet", **(
                {"vulnerabilities": summary.get("vulnerabilities")} if summary.get("vulnerabilities") else {})}
            if w.kind == "pypi" and ch.vulnerabilities:  # a vulnerable installed dependency is reported at once
                cls, flags, summary = classify(w, ch)
            else:
                continue
        if cls is None:
            if summary:
                row.change_summary = {**(row.change_summary or {}), "last_check": summary}
            continue
        row.classification, row.flags, row.change_summary = cls, flags, summary
        new_ref = row.latest_commit if w.kind == "github" else (state.get("latest_release") or row.installed_version)
        ev = UpdateEvent(key=w.key, detected_at=now, classification=cls, from_ref=(old_ref or "")[:128] or None,
                         to_ref=(new_ref or "")[:128] or None, summary={**summary, "why": w.why, "category": w.category})
        session.add(ev)
        await session.flush()
        events.append({"id": str(ev.id), "key": w.key, "classification": cls, "why": w.why, "summary": summary})
    return events


def message(ev: dict[str, Any]) -> tuple[str, str]:
    s = ev["summary"]
    title = f"{ev['classification'].replace('_', ' ')}: {ev['key'].split(':', 1)[1]}"
    parts = [ev["why"]]
    if s.get("release"):
        parts.append(f"release {s['release']}")
    if s.get("version"):
        parts.append(f"installed {s['version']['installed']}, latest {s['version']['latest']}")
    if s.get("reasons"):
        parts.append("; ".join(s["reasons"]))
    if s.get("messages"):
        parts.append("e.g. " + s["messages"][0])
    parts.append("Not applied automatically: review and deploy by hand.")
    return title, ". ".join(parts)


async def run(session_factory, redis, settings, stop: asyncio.Event, interval: float = CHECK_SECONDS) -> None:
    from yonixalpha_core import events as ev_mod

    token = settings.GITHUB_TOKEN.get_secret_value() if getattr(settings, "GITHUB_TOKEN", None) else None
    async with httpx.AsyncClient() as client:
        while not stop.is_set():
            try:
                async with session_factory() as session:
                    found = await check_all(session, client, datetime.now(timezone.utc), token)
                    for e in found:
                        sev = NOTIFY.get(e["classification"])
                        if sev:
                            title, body = message(e)
                            await ev_mod.notify(session, redis, settings if e["classification"] in TELEGRAM else None,
                                                "infrastructure_update", title, body,
                                                severity=sev, data={"event_id": e["id"], "key": e["key"]})
                            from yonixalpha_core.db.models import UpdateEvent
                            row = await session.get(UpdateEvent, uuid.UUID(e["id"]))
                            if row is not None:
                                row.notified = True
                    await session.commit()
                log.info("update_monitor.pass", events=len(found))
            except Exception as exc:  # noqa: BLE001 - never stops the host service
                log.warning("update_monitor.pass_failed", error=f"{type(exc).__name__}: {exc}"[:200])
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
