"""Activity probe for Solana launchpads beyond Pump.fun / PumpSwap (master §5-7).

Observe only: these venues are not traded, quoted or copied. The probe
answers one question per venue, from the chain itself: is it being used?

Every PROBE_SECONDS, per venue program:
  1. getSignaturesForAddress(program, limit=1000): the newest transactions
     touching the program. Gives the time of the last successful one and an
     exact transaction rate over the span those 1,000 cover.
  2. getTransaction for the SAMPLE newest successful ones; their
     "Program log: Instruction: <Name>" lines, attributed to the program
     through the invoke stack (CPI calls from aggregators count for the
     program that ran the instruction), classify launch / trade / migration.
     Names the probe does not know are counted and reported, never dropped.

Launches are rare next to trades, so a sample can miss them: the last
launch time is the newest launch SEEN (None when none was seen) and 7-day
launch counts are not measured (None, never 0). Polling instead of a
logsSubscribe stream keeps provider usage to about 30 calls per venue per
probe; a DBC / LaunchLab log stream carries every swap.

Program ids and instruction names come from the venues' own sources:
  Raydium LaunchLab  LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj
                     raydium-io/raydium-idl raydium_launchpad.json 0.2.0
                     (e7e0c96); raydium-sdk-V2 LAUNCHPAD_PROGRAM (cc33ec2).
                     LetsBONK / bonk.fun launches run on it.
  Meteora DBC        dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN
                     MeteoraAg dynamic-bonding-curve-sdk IDL 0.2.1 (a07966d).
                     Several launch sites use it (one program, many configs).
  Moonshot           MoonCVVNZFSYkqNXP6bxHLPL6QQJiMagDL3qcqUQTrG
                     wen-moon-ser/moonshot-sdk IDL V4 (be46cc5, 2025-04).
Anchor logs instruction names in UpperCamelCase ("swap2" -> "Swap2"); that
convention is assumed and the unknown-name report shows it if wrong.
"""

from __future__ import annotations

import asyncio
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from yonixalpha_core.logging import get_logger

log = get_logger("solana.venue_probe")

LAUNCH, TRADE, MIGRATION = "launch", "trade", "migration"
PROBE_SECONDS = 300
SIGNATURES = 1000
SAMPLE = 25
PROBE_SOURCE = "venue probe (signatures + sampled txs)"  # launchpad_checks.source is varchar(48)


def _camel(name: str) -> str:
    return "".join(p[:1].upper() + p[1:] for p in name.split("_"))


def _kinds(launch: tuple[str, ...], trade: tuple[str, ...], migration: tuple[str, ...]) -> dict[str, str]:
    out = {}
    for kind, names in ((LAUNCH, launch), (TRADE, trade), (MIGRATION, migration)):
        for n in names:
            out[_camel(n)] = kind
    return out


VENUES: dict[str, dict[str, Any]] = {
    "raydium_launchlab": {
        "program": "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj",
        "kinds": _kinds(("initialize", "initialize_v2", "initialize_with_token_2022"),
                        ("buy_exact_in", "buy_exact_out", "sell_exact_in", "sell_exact_out"),
                        ("migrate_to_amm", "migrate_to_cpswap")),
    },
    "meteora_dbc": {
        "program": "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN",
        "kinds": _kinds(("initialize_virtual_pool_with_spl_token", "initialize_virtual_pool_with_token2022",
                         "initialize_virtual_pool_with_token2022_transfer_hook"),
                        ("swap", "swap2", "swap2_with_transfer_hook"),
                        ("migration_damm_v2", "migrate_meteora_damm")),
    },
    "moonshot": {
        "program": "MoonCVVNZFSYkqNXP6bxHLPL6QQJiMagDL3qcqUQTrG",
        "kinds": _kinds(("token_mint",), ("buy", "sell"), ("migrate_funds",)),
    },
}

_INVOKE = re.compile(r"^Program (\w+) invoke \[\d+\]")
_EXIT = re.compile(r"^Program (\w+) (success|failed)")
_IX = re.compile(r"^Program log: Instruction: (\w+)")


def classify_logs(program: str, logs: list[str], kinds: dict[str, str]) -> tuple[Counter, Counter]:
    """(kind counts, unknown instruction names) for the instructions `program`
    itself executed in one transaction's logs."""
    found, unknown = Counter(), Counter()
    stack: list[str] = []
    for line in logs or []:
        if m := _INVOKE.match(line):
            stack.append(m.group(1))
        elif m := _EXIT.match(line):
            if stack and stack[-1] == m.group(1):
                stack.pop()
        elif (m := _IX.match(line)) and stack and stack[-1] == program:
            name = m.group(1)
            if name in kinds:
                found[kinds[name]] += 1
            else:
                unknown[name] += 1
    return found, unknown


@dataclass
class ProbeResult:
    venue: str
    program: str
    ok: bool
    error: str | None = None
    signatures: int = 0
    successful: int = 0
    last_tx_at: datetime | None = None
    span_s: float | None = None
    rate_per_min: float | None = None
    sampled: int = 0
    kinds: Counter = field(default_factory=Counter)
    unknown: Counter = field(default_factory=Counter)
    last_seen: dict[str, datetime] = field(default_factory=dict)  # kind -> newest sampled tx of that kind

    def evidence(self) -> dict[str, Any]:
        return {"program": self.program, "signatures": self.signatures, "successful": self.successful,
                "last_tx_at": self.last_tx_at.isoformat() if self.last_tx_at else None,
                "span_s": self.span_s, "rate_per_min": self.rate_per_min, "sampled": self.sampled,
                "sample_kinds": dict(self.kinds), "unknown_instructions": dict(self.unknown.most_common(10)),
                "last_seen": {k: v.isoformat() for k, v in self.last_seen.items()}, "error": self.error}


def _ts(t) -> datetime | None:
    return datetime.fromtimestamp(t, timezone.utc) if t else None


async def probe(rpc, venue: str, sample: int = SAMPLE) -> ProbeResult:
    spec = VENUES[venue]
    res = ProbeResult(venue, spec["program"], ok=False)
    try:
        sigs = await rpc.call("getSignaturesForAddress", [spec["program"], {"limit": SIGNATURES}], priority="background")
    except Exception as exc:  # noqa: BLE001 - reported as a failed probe
        res.error = f"getSignaturesForAddress: {type(exc).__name__}: {str(exc)[:160]}"
        return res
    sigs = sigs if isinstance(sigs, list) else []  # RpcManager.call returns the bare result
    res.ok = True
    res.signatures = len(sigs)
    good = [s for s in sigs if not s.get("err") and s.get("blockTime")]
    res.successful = len(good)
    if good:
        res.last_tx_at = _ts(good[0]["blockTime"])
        span = good[0]["blockTime"] - good[-1]["blockTime"]
        res.span_s = float(span)
        res.rate_per_min = round(len(good) / span * 60, 2) if span > 0 else None
    for s in good[:sample]:
        try:
            tx = await rpc.call("getTransaction", [s["signature"], {"encoding": "json",
                                                                    "maxSupportedTransactionVersion": 0}],
                                priority="background")
        except Exception as exc:  # noqa: BLE001 - one missing sample is not a failed probe
            log.info("venue_probe.tx_failed", venue=venue, error=type(exc).__name__)
            continue
        logs = ((tx or {}).get("meta") or {}).get("logMessages") or []
        res.sampled += 1
        found, unknown = classify_logs(spec["program"], logs, spec["kinds"])
        res.kinds.update(found)
        res.unknown.update(unknown)
        at = _ts(s.get("blockTime"))
        for k in found:
            if at and (k not in res.last_seen or at > res.last_seen[k]):
                res.last_seen[k] = at
    return res


async def record(session, res: ProbeResult, now: datetime) -> None:
    """Writes the probe as launchpad checks (caller commits):
    ACTIVE   the program answered (the probe works);
    EVENTS   a successful transaction within the last 24 hours, classified."""
    from yonixalpha_core.chains import verification

    ev = res.evidence()
    await verification.record(session, res.venue, "ACTIVE", res.ok, ev, PROBE_SOURCE, now=now)
    recent = res.last_tx_at is not None and (now - res.last_tx_at).total_seconds() <= 86400
    await verification.record(session, res.venue, "EVENTS", res.ok and recent and sum(res.kinds.values()) > 0, ev,
                              PROBE_SOURCE, now=now)


async def run(rpc, session_factory, stop: asyncio.Event, venues: tuple[str, ...] = tuple(VENUES),
              interval: float = PROBE_SECONDS) -> None:
    while not stop.is_set():
        for venue in venues:
            if stop.is_set():
                break
            res = await probe(rpc, venue)
            try:
                async with session_factory() as session:
                    await record(session, res, datetime.now(timezone.utc))
                    await session.commit()
            except Exception as exc:  # noqa: BLE001 - never stops data-solana
                log.warning("venue_probe.record_failed", venue=venue, error=f"{type(exc).__name__}: {exc}"[:200])
            log.info("venue_probe.done", venue=venue, ok=res.ok, last_tx_at=str(res.last_tx_at),
                     rate_per_min=res.rate_per_min, kinds=dict(res.kinds), unknown=dict(res.unknown.most_common(5)))
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
