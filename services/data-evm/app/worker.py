"""One chain's EVM pipeline: discovery → stats / category → safety → paper
entry → position management, plus continuously recorded launchpad evidence.

Every pass is isolated: a failure in one launchpad or one position never
stops the others. Nothing here signs or sends a transaction.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, delete, exists, or_, select
from sqlalchemy.exc import IntegrityError

from yonixalpha_core import copy_trading as ct
from yonixalpha_core import events, launch_coordination
from yonixalpha_core.chains import verification
from yonixalpha_core.chains.evm import observation, paper, safety, store
from yonixalpha_core.chains.evm import settings as evm_settings
from yonixalpha_core.chains.evm.rpc import EvmRpcUnavailableError
from yonixalpha_core.db.models import EvmObservation, EvmToken, EvmTrade, PaperPosition
from yonixalpha_core.logging import get_logger
from yonixalpha_core.notify import alert_error
from yonixalpha_core.safety.store import add_timeline_event

log = get_logger("data-evm.worker")
SERVICE = "data-evm"
SAFETY_RECHECK = timedelta(minutes=2)
SAFETY_PER_PASS = 8
EVIDENCE_EVERY = timedelta(minutes=30)
TRADE_RETENTION = timedelta(days=14)
RPC_OUTAGE_ALERT_SECONDS = 120  # discovery failing on an unavailable RPC this long is alerted
E18 = Decimal(10) ** 18


@dataclass
class Evidence:
    """What the service itself observed per launchpad since the last record."""

    launches: int = 0
    trades: int = 0
    decode_errors: int = 0
    rejected_foreign: int = 0
    migrations_confirmed: list = field(default_factory=list)
    safety: dict = field(default_factory=dict)  # verdict -> count
    quotes_ok: int = 0
    quotes_failed: int = 0
    liquidity_seen: list = field(default_factory=list)
    sample_trade: str | None = None
    sample_quote: dict | None = None


class ChainWorker:
    def __init__(self, chain: str, rpc, adapters: list, session_factory, redis, *, etherscan_key: str | None = None,
                 http=None) -> None:
        self.chain = chain
        self.etherscan_key = etherscan_key
        self.http = http  # explorer client for launch-coordination funding lookups (None: one per assessment)
        self.rpc = rpc
        self.adapters = {a.spec.key: a for a in adapters}
        self.session_factory = session_factory
        self.redis = redis
        self.evidence = {k: Evidence() for k in self.adapters}
        self.last_evidence_at: datetime | None = None
        self.block_seconds: float | None = None
        self.status: dict[str, Any] = {}
        self.failing_since: dict[str, float] = {}  # launchpad -> monotonic time its discovery started failing

    async def restore(self) -> None:
        async with self.session_factory() as session:
            for ad in self.adapters.values():
                n = await store.restore_adapter(session, ad)
                log.info("data-evm.restored", chain=self.chain, launchpad=ad.spec.key, registrations=n)

    async def _block_seconds(self, head: int) -> float:
        if self.block_seconds is None:
            top = max(0, head - 10)  # a few blocks under the head: every backend of a load-balanced node has them
            sample = min(1000, top)
            a = int((await self.rpc.get_block(top))["timestamp"], 16)
            b = int((await self.rpc.get_block(top - sample))["timestamp"], 16)
            self.block_seconds = max(0.05, (a - b) / sample) if sample else 1.0
        return self.block_seconds

    # --- discovery ----------------------------------------------------------------------------------

    async def discovery_pass(self, s: evm_settings.EvmTradingSettings, now: datetime) -> dict[str, Any]:
        cs = s.chain(self.chain)
        head = await self.rpc.block_number() - cs.confirmations
        out: dict[str, Any] = {"head": head}
        for key, ad in self.adapters.items():
            if not ad.spec.active:
                continue
            try:
                async with self.session_factory() as session:
                    cursor = await store.get_cursor(session, self.chain, key)
                skipped = None
                if cursor is None:
                    back = int(cs.backfill_minutes * 60 / await self._block_seconds(head))
                    cursor = max(0, head - back)
                elif cs.max_lag_minutes and (head - cursor) * await self._block_seconds(head) > cs.max_lag_minutes * 60:
                    back = int(cs.backfill_minutes * 60 / await self._block_seconds(head))
                    skipped = {"from": cursor + 1, "to": head - back, "blocks": head - back - cursor,
                               "lag_minutes": round((head - cursor) * await self._block_seconds(head) / 60, 1)}
                    cursor = head - back
                    log.warning("data-evm.discovery_gap_skipped", chain=self.chain, launchpad=key, **skipped)
                    await alert_error(SERVICE, f"{self.chain}.{key}.discovery_gap_skipped", skipped)
                fb, tb = cursor + 1, min(head, cursor + cs.max_blocks_per_pass)
                if fb > tb:
                    out[key] = {"lag": 0}
                    self.failing_since.pop(key, None)
                    continue
                before = len(await ad._emitters() or [])
                res = await ad.scan(fb, tb)
                if len(await ad._emitters() or []) != before:  # curves / pools announced in this range
                    again = await ad.scan(fb, tb)
                    res.trades, res.decode_errors = again.trades, res.decode_errors + again.decode_errors
                async with self.session_factory() as session:
                    counts = await store.persist_scan(session, ad, res, now)
                    touched = {t.token for t in res.trades} | {ln.token for ln in res.launches}
                    await store.refresh_stats(session, self.chain, touched, now, s)
                    await session.commit()
                ev = self.evidence[key]
                ev.launches += len(res.launches)
                ev.trades += len(res.trades)
                ev.decode_errors += len(res.decode_errors)
                ev.rejected_foreign += res.rejected_foreign
                if res.trades:
                    ev.sample_trade = res.trades[-1].event_id
                for m in res.migrations:
                    ev.migrations_confirmed.append({"token": m["token"], "tx": m.get("tx_hash"), "venue": m.get("venue")})
                if res.decode_errors:
                    await alert_error(SERVICE, f"{self.chain}.{key}.decode_errors",
                                      {"count": len(res.decode_errors), "sample": res.decode_errors[:2]})
                out[key] = {**counts, "from": fb, "to": tb, "lag": head - tb}
                if skipped:
                    out[key]["skipped"] = skipped
                self.failing_since.pop(key, None)
            except Exception as exc:  # noqa: BLE001 - one launchpad never stops the others
                out[key] = {"error": f"{type(exc).__name__}: {str(exc)[:160]}"}
                log.warning("data-evm.discovery_failed", chain=self.chain, launchpad=key, error=out[key]["error"])
                since = self.failing_since.setdefault(key, time.monotonic())
                failing_s = time.monotonic() - since
                # A public node's short rate-limit cooldown clears on a later pass and loses
                # nothing (the cursor resumes where it stopped): alerted once it persists.
                # Anything else is alerted at once.
                if not isinstance(exc, EvmRpcUnavailableError) or failing_s >= RPC_OUTAGE_ALERT_SECONDS:
                    await alert_error(SERVICE, f"{self.chain}.{key}.discovery_failed",
                                      {**out[key], "failing_for_s": round(failing_s)})
        return out

    # --- safety -------------------------------------------------------------------------------------

    async def safety_pass(self, s: evm_settings.EvmTradingSettings, now: datetime) -> int:
        cs = s.chain(self.chain)
        async with self.session_factory() as session:
            rows = (await session.execute(select(EvmToken).where(
                EvmToken.chain == self.chain, EvmToken.category.in_(s.entry_categories),
                EvmToken.last_trade_at >= now - timedelta(minutes=5),
                EvmToken.launchpad.in_(list(self.adapters)),
                or_(EvmToken.safety_at.is_(None), EvmToken.safety_at < now - SAFETY_RECHECK),
            ).order_by(EvmToken.last_trade_at.desc()).limit(SAFETY_PER_PASS))).scalars().all()
            tokens = [(r.token, r.launchpad) for r in rows]
        done = 0
        for token, key in tokens:
            ad = self.adapters[key]
            ev = self.evidence[key]
            try:
                try:
                    state = await ad.get_token_state(token)
                except EvmRpcUnavailableError:
                    raise
                except Exception:  # noqa: BLE001 - safety.check records the unreadable state itself
                    state = None
                report, rt = await safety.check(ad, token, int(cs.position_size * E18), s, state)
                async with self.session_factory() as session:
                    row = await session.get(EvmToken, (self.chain, token))
                    if row is None:
                        continue
                    if state is not None:
                        row.state = store._js({k: v for k, v in vars(state).items() if k not in ("chain", "category")})
                        row.state_at = now
                        if state.stage in ("CURVE", "DEX", "GRADUATING", "KILLED"):
                            row.stage = state.stage
                        if state.liquidity_quote and state.liquidity_quote > 0:
                            ev.liquidity_seen.append({"token": token, "liquidity": str(state.liquidity_quote)})
                    row.safety_verdict = report.verdict
                    row.safety = store._js({"findings": report.findings, "sellable": report.sellable,
                                            "sources": report.sources, "round_trip": rt})
                    row.safety_at = now
                    await self._coordinate(session, ad, row, now)
                    await session.commit()
                ev.safety[report.verdict] = ev.safety.get(report.verdict, 0) + 1
                if rt.get("sellable"):
                    ev.quotes_ok += 1
                    ev.sample_quote = {"token": token, "probe_in": rt.get("probe_in"), "returned": rt.get("returned"),
                                       "round_trip_loss_bps": rt.get("round_trip_loss_bps")}
                elif rt.get("buy") and not rt["buy"].get("ok"):
                    ev.quotes_failed += 1
                done += 1
            except EvmRpcUnavailableError as exc:
                log.warning("data-evm.safety_unavailable", chain=self.chain, token=token, error=str(exc)[:160])
                break  # the RPC is down: stop this pass, nothing is marked safe or unsafe
            except Exception as exc:  # noqa: BLE001
                log.warning("data-evm.safety_failed", chain=self.chain, token=token, error=str(exc)[:160])
                await alert_error(SERVICE, f"{self.chain}.safety_failed", {"token": token, "error": str(exc)[:300]})
        return done

    async def _coordinate(self, session, ad, row: EvmToken, now: datetime) -> None:
        """Launch-window coordination (master §11), refreshed with the safety
        check. A failed assessment leaves the old one to age out: entries are
        then blocked as COORDINATION_NOT_CHECKED, never let through."""
        cfg = await launch_coordination.load_config(session)
        if not cfg.enabled:
            return
        try:
            res = await launch_coordination.assess(session, ad, row, now, cfg, client=self.http,
                                                   etherscan_key=self.etherscan_key)
        except EvmRpcUnavailableError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("data-evm.coordination_failed", chain=self.chain, token=row.token, error=str(exc)[:160])
            await alert_error(SERVICE, f"{self.chain}.coordination_failed", {"token": row.token, "error": str(exc)[:300]})
            return
        row.coordination, row.coordination_at = store._js(res), now

    # --- entries ------------------------------------------------------------------------------------

    async def entry_pass(self, s: evm_settings.EvmTradingSettings, now: datetime) -> dict[str, int]:
        counts = {"evaluated": 0, "opened": 0}
        open_obs = exists().where(and_(  # master §14: only a token under observation can be entered
            EvmObservation.chain == EvmToken.chain, EvmObservation.token == EvmToken.token,
            EvmObservation.category == EvmToken.category, EvmObservation.state.not_in(observation.TERMINAL)))
        async with self.session_factory() as session:
            tokens = (await session.execute(select(EvmToken.token).where(
                EvmToken.chain == self.chain, EvmToken.category.in_(s.entry_categories),
                EvmToken.safety_at >= now - paper.SAFETY_MAX_AGE, EvmToken.launchpad.in_(list(self.adapters)),
                open_obs,
            ).order_by(EvmToken.last_trade_at.desc()).limit(20))).scalars().all()
        for token in tokens:
            try:
                async with self.session_factory() as session:
                    row = await session.get(EvmToken, (self.chain, token))
                    ad = self.adapters[row.launchpad]
                    d = await paper.evaluate_entry(session, self.redis, ad, row, s, now)
                    counts["evaluated"] += 1
                    row.extra = {**(row.extra or {}), "entry_decision": store._js(d.to_dict())}
                    obs = await observation.open_for(session, self.chain, token, row.category)
                    if d.ok:
                        p = await paper.open_position(session, d, row, now)
                        counts["opened"] += 1
                        log.info("data-evm.paper_entry", chain=self.chain, token=token, position_id=str(p.id))
                    if obs is not None:
                        observation.record_entry_decision(obs, d.to_dict(), d.ok, now)
                    await session.commit()
                if d.ok:
                    await events.publish(self.redis, "trade.opened", {"chain": self.chain, "token": token,
                                                                      "position_id": str(p.id)}, SERVICE)
            except IntegrityError:
                log.info("data-evm.duplicate_entry_refused", chain=self.chain, token=token)
            except Exception as exc:  # noqa: BLE001
                log.warning("data-evm.entry_failed", chain=self.chain, token=token, error=str(exc)[:160])
                await alert_error(SERVICE, f"{self.chain}.entry_failed", {"token": token, "error": str(exc)[:300]})
        return counts

    # --- observation (master §14-17) ----------------------------------------------------------------

    async def observation_pass(self, now: datetime) -> dict[str, int]:
        """Due snapshots, safety outcomes, adaptive windows and expiry."""
        async with self.session_factory() as session:
            cfg = await observation.load_config(session)
            counts = await observation.step(session, self.chain, now, cfg)
            await session.commit()
        return counts

    # --- positions ----------------------------------------------------------------------------------

    async def manage_pass(self, now: datetime) -> dict[str, int]:
        counts = {"managed": 0, "closed": 0, "unpriced": 0}
        async with self.session_factory() as session:
            ids = (await session.execute(select(PaperPosition.id).where(
                PaperPosition.engine == paper.engine_for(self.chain), PaperPosition.status == "open"))).scalars().all()
        for pid in ids:
            try:
                async with self.session_factory() as session:
                    p = await session.get(PaperPosition, pid)
                    key = ((p.plan or {}).get("venue") or {}).get("launchpad")
                    ad = self.adapters.get(key)
                    if ad is None:
                        counts["unpriced"] += 1
                        continue
                    queued = ct.pending_partial_exit(p.plan)  # a SELL_ONLY copy target sold this token
                    extra = None
                    if queued is not None:
                        remaining = p.remaining_quantity if p.remaining_quantity is not None else p.quantity
                        extra = (remaining * queued, "copy_sell")
                    r = await paper.manage_position(session, ad, p, now, extra_exit=extra)
                    if extra is not None and r["status"] != "UNPRICED":
                        p.plan = ct.clear_partial_exit(p.plan)
                        await add_timeline_event(session, "copy_partial_exit_filled", now,
                                                 {"quantity": str(extra[0]), "fraction": str(queued)}, position_id=p.id)
                    await session.commit()
                if r["status"] == "UNPRICED":
                    counts["unpriced"] += 1
                    await self.redis.set(f"yx:evm:unpriced:{pid}", json.dumps({"error": r.get("error"), "at": now.isoformat()}),
                                         ex=600)
                    continue
                counts["managed"] += 1
                if r["exits"]:
                    await events.publish(self.redis, "trade.closed" if r["status"] == "CLOSED" else "trade.updated",
                                         {"position_id": str(pid), "exits": r["exits"], "chain": self.chain}, SERVICE)
                else:
                    await events.publish(self.redis, "position.updated", {"position_id": str(pid), "price": str(r["price"])},
                                         SERVICE)
                if r["status"] == "CLOSED":
                    counts["closed"] += 1
            except Exception as exc:  # noqa: BLE001
                log.warning("data-evm.manage_failed", chain=self.chain, position_id=str(pid), error=str(exc)[:160])
                await alert_error(SERVICE, f"{self.chain}.manage_failed", {"position_id": str(pid), "error": str(exc)[:300]})
        return counts

    # --- evidence -----------------------------------------------------------------------------------

    async def evidence_pass(self, now: datetime, force: bool = False) -> int:
        """Records what this service observed on the real chain as launchpad
        evidence (source "data-evm"). A check is recorded only when there is
        something to judge; an RPC outage records nothing."""
        if not force and self.last_evidence_at and now - self.last_evidence_at < EVIDENCE_EVERY:
            return 0
        window = {"since": self.last_evidence_at.isoformat() if self.last_evidence_at else None, "until": now.isoformat()}
        n = 0
        async with self.session_factory() as session:
            for key, ad in self.adapters.items():
                if not ad.spec.active:
                    continue
                ev = self.evidence[key]
                rows: list[tuple[str, bool, dict]] = []
                try:
                    codes = {k: len((await self.rpc.get_code(a)) or "0x") // 2 - 1 for k, a in ad.spec.contracts.items()}
                    rows.append(("ACTIVE", all(v > 0 for v in codes.values()), {"code_bytes": codes}))
                except EvmRpcUnavailableError:
                    pass
                # A quiet window is not a failure: DISCOVERY is only ever recorded
                # as PASS, and a launchpad without launches for 24 h loses it
                # through the liveness expiry (chains/verification.py).
                if ev.launches:
                    rows.append(("DISCOVERY", True, {**window, "launches": ev.launches}))
                if ev.decode_errors:
                    rows.append(("EVENTS", False, {**window, "decode_errors": ev.decode_errors}))
                elif ev.trades:
                    rows.append(("EVENTS", True, {**window, "trades": ev.trades, "sample": ev.sample_trade,
                                                  "rejected_foreign": ev.rejected_foreign}))
                if ev.quotes_ok:
                    rows.append(("QUOTE", True, {**window, "round_trips": ev.quotes_ok, "sample": ev.sample_quote}))
                elif ev.quotes_failed >= 5:  # one odd token is not a broken quote path
                    rows.append(("QUOTE", False, {**window, "failed": ev.quotes_failed}))
                judged = {v: c for v, c in ev.safety.items() if v != "UNKNOWN"}
                if judged:
                    rows.append(("SAFETY", True, {**window, "verdicts": ev.safety,
                                                  "note": "the safety checks ran to a verdict on the real chain"}))
                if ev.liquidity_seen:
                    rows.append(("LIQUIDITY", True, {**window, "sample": ev.liquidity_seen[-1],
                                                     "tokens": len(ev.liquidity_seen)}))
                if ev.migrations_confirmed:
                    m = ev.migrations_confirmed[-1]
                    try:
                        det = await ad.detect_migration(m["token"])
                        rows.append(("MIGRATION_DETECTION", bool(det), {**m, "confirmed": store._js(det)}))
                    except EvmRpcUnavailableError:
                        pass
                for check, ok, evidence in rows:
                    await verification.record(session, key, check, ok, store._js(evidence), SERVICE, now)
                    n += 1
                self.evidence[key] = Evidence()
            await session.commit()
        self.last_evidence_at = now
        return n

    async def prune(self, now: datetime) -> int:
        async with self.session_factory() as session:
            r = await session.execute(delete(EvmTrade).where(EvmTrade.chain == self.chain,
                                                             EvmTrade.at < now - TRADE_RETENTION))
            await session.commit()
        return r.rowcount or 0


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
