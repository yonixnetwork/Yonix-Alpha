"""The copy engine: watches the operator's copy targets on Solana, BSC and
Robinhood Chain and turns each observed target trade into a recorded copy
event, then (by mode) a gated paper entry, a mirrored exit, a notification
or a skip with its reason. See yonixalpha_core.copy_trading for the rules.

Nothing here signs or sends a transaction: copy trading is paper only.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError

from yonixalpha_core import copy_trading as ct
from yonixalpha_core import events, kill_switch, paper_engine, wallet_profiles
from yonixalpha_core.chains import controls, verification
from yonixalpha_core.chains.base import Chain
from yonixalpha_core.chains.evm import paper, safety, store
from yonixalpha_core.chains.evm import settings as evm_settings
from yonixalpha_core.db.models import (CopyEvent, CopyPosition, CopyTarget, EvmToken, EvmTrade, PaperAccount, PaperPosition,
                                       RiskAssessment)
from yonixalpha_core.logging import get_logger
from yonixalpha_core.notify import alert_error
from yonixalpha_core.safety.liquidity import ConstantProductModel
from yonixalpha_core.safety.models import FinalDecision, ManualOverrides
from yonixalpha_core.safety.planning import plan_trade
from yonixalpha_core.safety.store import account_state as solana_account_state
from yonixalpha_core.safety.store import add_timeline_event, load_settings
from yonixalpha_core.solana import pump_stream
from yonixalpha_core.solana.flow import measured_volatility

log = get_logger("copy-engine")
SERVICE = "copy-engine"
LAMPORTS = Decimal(10) ** 9
PUMP_DECIMALS = 6
GATE_MAX_AGE = timedelta(minutes=10)
LOOKBACK = timedelta(minutes=10)
E18 = Decimal(10) ** 18


def evm_copy_engine(chain: str) -> str:
    return f"evm_copy_{chain}"


SOLANA_COPY_ENGINE = "copy_solana"


class Outcome:
    def __init__(self) -> None:
        self.decision = "SKIPPED"
        self.reason: str | None = None
        self.detail: dict[str, Any] = {}
        self.analyzed_at = self.planned_at = self.executed_at = None
        self.position_id: uuid.UUID | None = None

    def skip(self, code: str, message: str = "") -> "Outcome":
        self.decision, self.reason = "SKIPPED", f"{code}: {message}"[:300] if message else code
        return self


class CopyEngine:
    def __init__(self, session_factory, redis, evm_adapters: dict[str, dict[str, Any]], now_fn) -> None:
        self.session_factory = session_factory
        self.redis = redis
        self.evm_adapters = evm_adapters  # chain -> launchpad key -> adapter
        self.now = now_fn
        self.status: dict[str, Any] = {}

    async def refresh_adapters(self) -> None:
        async with self.session_factory() as session:
            for ads in self.evm_adapters.values():
                for ad in ads.values():
                    await store.restore_adapter(session, ad)

    async def _targets(self, chain: str) -> list[CopyTarget]:
        async with self.session_factory() as session:
            return list((await session.execute(select(CopyTarget).where(
                CopyTarget.chain == chain, CopyTarget.enabled.is_(True)))).scalars().all())

    async def _record(self, target: CopyTarget, token: str, side: str, source_event_id: str, t_tokens: Decimal,
                      t_quote: Decimal, target_at: datetime, detected_at: datetime) -> uuid.UUID | None:
        """Inserts the event before any decision; None if it was already seen."""
        async with self.session_factory() as session:
            r = await session.execute(insert(CopyEvent).values(
                id=uuid.uuid4(), target_id=target.id, chain=target.chain, wallet=target.wallet, token=token, side=side,
                source_event_id=source_event_id, target_token_amount=t_tokens, target_quote_amount=t_quote,
                target_at=target_at, detected_at=detected_at, decision="PENDING",
            ).on_conflict_do_nothing(constraint="uq_copy_events_target_source").returning(CopyEvent.id))
            eid = r.scalar_one_or_none()
            await session.commit()
            return eid

    async def _finish(self, eid: uuid.UUID, target: CopyTarget, o: Outcome, target_at: datetime, detected_at: datetime) -> None:
        now = self.now()
        async with self.session_factory() as session:
            ev = await session.get(CopyEvent, eid)
            ev.decision, ev.reason, ev.detail, ev.decided_at = o.decision, o.reason, store._js(o.detail), now
            ev.position_id = o.position_id
            ev.latency_ms = ct.latency(target_at, detected_at, o.analyzed_at, o.planned_at, o.executed_at)
            await session.commit()
        await events.publish(self.redis, "copy.event", {"target": target.wallet, "chain": target.chain, "token": ev.token,
                                                        "side": ev.side, "decision": o.decision, "reason": o.reason}, SERVICE)

    async def _common_blockers(self, session, target: CopyTarget, s: ct.CopySettings, t_quote_native: Decimal,
                               target_at: datetime, detected_at: datetime, o: Outcome) -> bool:
        if target.mode == "NOTIFY":
            o.decision, o.reason = "NOTIFIED", "notify-only target"
            return True
        if await kill_switch.is_engaged(self.redis):
            o.skip("KILL_SWITCH", "global kill switch engaged")
            return True
        blocked = controls.blocked_by(await controls.load(session), Chain(target.chain), "copy")
        if blocked:
            o.skip("TRADING_CONTROL_OFF", blocked)
            return True
        delay = (detected_at - target_at).total_seconds()
        if delay > s.max_delay_seconds:
            o.skip("TOO_LATE", f"seen {delay:.0f}s after the target's trade (limit {s.max_delay_seconds}s)")
            return True
        if t_quote_native < s.min_target_buy:
            o.skip("TARGET_BUY_TOO_SMALL", f"{t_quote_native} < {s.min_target_buy}")
            return True
        open_n = (await session.execute(select(func.count()).select_from(CopyPosition).join(
            PaperPosition, PaperPosition.id == CopyPosition.position_id).where(
            CopyPosition.target_id == target.id, PaperPosition.status == "open"))).scalar_one()
        if open_n >= s.max_open_positions:
            o.skip("MAX_OPEN_FOR_TARGET", f"{open_n} open copies of this target")
            return True
        return False

    async def _open_copy(self, session, target: CopyTarget, token: str) -> CopyPosition | None:
        return (await session.execute(select(CopyPosition).join(PaperPosition, PaperPosition.id == CopyPosition.position_id)
                                      .where(CopyPosition.target_id == target.id, CopyPosition.token == token,
                                             PaperPosition.status == "open"))).scalars().first()

    # --- EVM --------------------------------------------------------------------------------------

    async def watch_evm(self, chain: str) -> int:
        targets = await self._targets(chain)
        if not targets:
            return 0
        by_wallet = {t.wallet.lower(): t for t in targets}
        now = self.now()
        async with self.session_factory() as session:
            trades = (await session.execute(select(EvmTrade).where(
                EvmTrade.chain == chain, func.lower(EvmTrade.trader).in_(list(by_wallet)),
                EvmTrade.at >= now - LOOKBACK).order_by(EvmTrade.at))).scalars().all()
        n = 0
        for t in trades:
            target = by_wallet[t.trader.lower()]
            if t.at < target.created_at:
                continue  # trades before the operator added the target are never copied
            detected = self.now()
            eid = await self._record(target, t.token, "BUY" if t.is_buy else "SELL", t.event_id, Decimal(t.token_amount),
                                     Decimal(t.quote_amount), t.at, detected)
            if eid is None:
                continue
            n += 1
            try:
                o = await (self._evm_buy(target, t, detected) if t.is_buy else self._evm_sell(target, t))
            except Exception as exc:  # noqa: BLE001
                o = Outcome()
                o.decision, o.reason = "FAILED", f"{type(exc).__name__}: {str(exc)[:200]}"
                await alert_error(SERVICE, f"{chain}.copy_failed", {"target": target.wallet, "error": o.reason})
            await self._finish(eid, target, o, t.at, detected)
        return n

    async def _evm_buy(self, target: CopyTarget, t: EvmTrade, detected: datetime) -> Outcome:
        o = Outcome()
        s, _ = ct.parse_settings(target.settings)
        chain = target.chain
        t_quote = Decimal(t.quote_amount) / E18
        async with self.session_factory() as session:
            if await self._common_blockers(session, target, s, t_quote, t.at, detected, o):
                return o
            row = await session.get(EvmToken, (chain, t.token))
            if row is None:
                return o.skip("TOKEN_NOT_DISCOVERED", "data-evm has no record of this token")
            if s.launchpads and row.launchpad not in s.launchpads:
                return o.skip("LAUNCHPAD_FILTERED", row.launchpad)
            ad = self.evm_adapters.get(chain, {}).get(row.launchpad)
            if ad is None or not ad.spec.supports_trading:
                return o.skip("VENUE_NOT_TRADABLE", row.launchpad)
            existing = await self._open_copy(session, target, t.token)
            if existing is not None:
                existing.target_tokens = Decimal(existing.target_tokens) + Decimal(t.token_amount)
                await session.commit()
                return o.skip("ALREADY_COPIED", "adds to an open copy are not copied; the target's holding is tracked")
            ctl = await controls.load(session)
            st = await verification.status_for(session, self.redis, ad.spec, controls.launchpad_mode(ctl, ad.spec.key, ad.spec.chain))
            if not st["paper_allowed"]:
                return o.skip("LAUNCHPAD_NOT_VERIFIED", f"{ad.spec.name} is {st['status']}")
            es = await evm_settings.load(session)
            cs = es.chain(chain)
            if row.safety_verdict != "PASS" or row.safety_at is None or detected - row.safety_at > paper.SAFETY_MAX_AGE:
                report, rt = await safety.check(ad, t.token, int(cs.position_size * E18), es)
                row.safety_verdict, row.safety_at = report.verdict, self.now()
                row.safety = store._js({"findings": report.findings, "sellable": report.sellable, "round_trip": rt,
                                        "sources": report.sources, "checked_for": "copy"})
                await session.commit()
            o.analyzed_at = self.now()
            if row.safety_verdict != "PASS":
                return o.skip("SAFETY_NOT_PASSED", f"safety verdict {row.safety_verdict}")
            liq = Decimal(str((row.state or {}).get("liquidity_quote") or 0))
            if liq < cs.min_liquidity:
                return o.skip("LIQUIDITY_TOO_LOW", f"{liq} < {cs.min_liquidity}")
            engine = evm_copy_engine(chain)
            acct_state, _ = await paper.account_state(session, self.redis, chain, t.token, self.now(), None, engine=engine)
            d = paper.EntryDecision(chain, t.token, row.launchpad, row.category, self.now())
            await paper.build_plan(session, ad, row, ct.size_for(s, chain, t_quote), acct_state, liq, self.now(), d)
            o.planned_at = self.now()
            o.detail = {"plan_detail": d.detail, "target_price": str(Decimal(t.quote_amount) / Decimal(t.token_amount))}
            if d.blockers:
                return o.skip(d.blockers[0]["code"], d.blockers[0]["message"])
            guard = ct.chase_guard(Decimal(t.quote_amount) / Decimal(t.token_amount),
                                   Decimal(d.buy.amount_in) / Decimal(d.buy.amount_out), s.chase_guard_pct)
            if guard:
                return o.skip("CHASE_GUARD", guard)
            p = await paper.open_position(session, d, row, self.now(), engine=engine)
            session.add(CopyPosition(position_id=p.id, target_id=target.id, chain=chain, token=t.token,
                                     target_tokens=Decimal(t.token_amount)))
            await add_timeline_event(session, "copy_entry", self.now(), {"target": target.wallet, "mode": target.mode,
                                                                         "source_event": t.event_id}, position_id=p.id)
            try:
                await session.commit()
            except IntegrityError:
                return o.skip("DUPLICATE_POSITION", "an open copy position on this token already exists")
            o.executed_at, o.position_id, o.decision, o.reason = self.now(), p.id, "COPIED", "paper entry"
        return o

    async def _evm_sell(self, target: CopyTarget, t: EvmTrade) -> Outcome:
        o = Outcome()
        async with self.session_factory() as session:
            cp = await self._open_copy(session, target, t.token)
            if cp is None:
                return o.skip("NO_OPEN_COPY", "no open copy position for this token")
            frac = ct.sell_fraction(Decimal(t.token_amount), Decimal(cp.target_tokens))
            cp.target_tokens = max(Decimal(0), Decimal(cp.target_tokens) - Decimal(t.token_amount))
            o.detail = {"target_sold_fraction": str(frac.quantize(Decimal("0.0001")))}
            if target.mode != "MIRROR":
                await session.commit()
                return o.skip("NOT_MIRRORED", f"{target.mode} target: exits follow our own risk plan")
            p = await session.get(PaperPosition, cp.position_id)
            ad = self.evm_adapters.get(target.chain, {}).get(((p.plan or {}).get("venue") or {}).get("launchpad"))
            if ad is None:
                o.decision, o.reason = "FAILED", "no adapter for the position's launchpad"
                return o
            remaining = p.remaining_quantity if p.remaining_quantity is not None else p.quantity
            o.analyzed_at = o.planned_at = self.now()
            r = await paper.manage_position(session, ad, p, self.now(), extra_exit=(remaining * frac, "copy_sell"))
            if r["status"] == "UNPRICED":
                await session.rollback()
                o.decision, o.reason = "FAILED", f"exit not quotable: {r.get('error')}"
                return o
            await session.commit()
            o.executed_at, o.position_id = self.now(), p.id
            o.decision, o.reason = "COPIED", f"sold {frac:.0%} of the copy"
        return o

    async def manage_evm(self, chain: str) -> dict[str, int]:
        counts = {"managed": 0, "closed": 0, "unpriced": 0}
        async with self.session_factory() as session:
            ids = (await session.execute(select(PaperPosition.id).where(
                PaperPosition.engine == evm_copy_engine(chain), PaperPosition.status == "open"))).scalars().all()
        for pid in ids:
            try:
                async with self.session_factory() as session:
                    p = await session.get(PaperPosition, pid)
                    ad = self.evm_adapters.get(chain, {}).get(((p.plan or {}).get("venue") or {}).get("launchpad"))
                    if ad is None:
                        counts["unpriced"] += 1
                        continue
                    r = await paper.manage_position(session, ad, p, self.now())
                    await session.commit()
                counts["unpriced" if r["status"] == "UNPRICED" else "managed"] += 1
                counts["closed"] += r["status"] == "CLOSED"
            except Exception as exc:  # noqa: BLE001
                await alert_error(SERVICE, f"{chain}.manage_failed", {"position_id": str(pid), "error": str(exc)[:300]})
        return counts

    # --- Solana -----------------------------------------------------------------------------------

    async def watch_solana(self) -> int:
        targets = await self._targets("solana")
        if not targets:
            return 0
        by_wallet = {t.wallet: t for t in targets}  # base58 is case-sensitive
        now = self.now()
        n = 0
        for mint in await pump_stream.active_mints(self.redis, now, 30):
            trades = await pump_stream.load_trades(self.redis, mint)
            for t in trades:
                target = by_wallet.get(t.trader)
                if target is None or t.at < now - LOOKBACK or t.at < target.created_at:
                    continue
                detected = self.now()
                # The stream keeps no signature: the trade's own fields identify it,
                # hashed so two 44-character addresses fit the key.
                sid = "solana:" + hashlib.sha256(
                    f"{mint}:{int(t.at.timestamp())}:{t.trader}:{int(t.is_buy)}:{t.sol_lamports}:{t.token_raw}".encode()
                ).hexdigest()
                eid = await self._record(target, mint, "BUY" if t.is_buy else "SELL", sid, Decimal(t.token_raw),
                                         Decimal(t.sol_lamports), t.at, detected)
                if eid is None:
                    continue
                n += 1
                try:
                    o = await (self._solana_buy(target, mint, t, trades, detected) if t.is_buy else self._solana_sell(target, mint, t))
                except Exception as exc:  # noqa: BLE001
                    o = Outcome()
                    o.decision, o.reason = "FAILED", f"{type(exc).__name__}: {str(exc)[:200]}"
                    await alert_error(SERVICE, "solana.copy_failed", {"target": target.wallet, "error": o.reason})
                await self._finish(eid, target, o, t.at, detected)
        return n

    async def _solana_buy(self, target: CopyTarget, mint: str, t, trades: list, detected: datetime) -> Outcome:
        o = Outcome()
        s, _ = ct.parse_settings(target.settings)
        t_sol = Decimal(t.sol_lamports) / LAMPORTS
        async with self.session_factory() as session:
            if await self._common_blockers(session, target, s, t_sol, t.at, detected, o):
                return o
            if await self._open_copy(session, target, mint) is not None:
                return o.skip("ALREADY_COPIED", "adds to an open copy are not copied")
            ra = (await session.execute(select(RiskAssessment).where(
                RiskAssessment.asset_id == mint, RiskAssessment.engine.like("solana%"),
                RiskAssessment.evaluated_at >= detected - GATE_MAX_AGE).order_by(RiskAssessment.evaluated_at.desc()))).scalars().first()
            o.analyzed_at = self.now()
            if ra is None or not ra.executable:
                return o.skip("NO_GATE_APPROVAL", "the Solana gate has not approved this token in the last 10 minutes"
                              if ra is None else f"latest gate decision {ra.decision}: not executable")
            curve = await pump_stream.load_curve(self.redis, mint)
            if curve is None or curve.complete or curve.fee_bps is None:
                return o.skip("NO_CURVE", "bonding curve not tradable from the stream (complete or unknown fee)")
            q, tok = Decimal(curve.vsol) / LAMPORTS, Decimal(curve.vtok) / Decimal(10) ** PUMP_DECIMALS
            model = ConstantProductModel(q, tok, Decimal(curve.fee_bps),
                                         Decimal(curve.rsol) / LAMPORTS if curve.rsol is not None else None)
            price = q / tok
            acct = await self._solana_account(session)
            state = await solana_account_state(session, acct, mint, self.now(), await kill_switch.is_engaged(self.redis))
            settings, _ = await load_settings(session, ra.engine)
            size = ct.size_for(s, "solana", t_sol)
            from dataclasses import replace as _replace
            settings = _replace(settings, max_position_size_quote=min(size, settings.max_position_size_quote),
                                min_position_size_quote=min(settings.min_position_size_quote, size))
            vol, _src = measured_volatility(trades, self.now(), 900, PUMP_DECIMALS)
            plan = plan_trade(entry_price=price, volatility=vol, liquidity_quote=model.liquidity_quote, settings=settings,
                              account=state, overrides=ManualOverrides(), model=model, quote=None, transfer_fee_bps=None)
            o.planned_at = self.now()
            blockers = [f for f in plan.findings if f.action == FinalDecision.NO_TRADE or f.hard_block]
            if blockers or not plan.complete:
                return o.skip(blockers[0].code if blockers else "PLAN_INCOMPLETE", blockers[0].message if blockers else "")
            fill = paper_engine.entry_fill(plan.position_size.value, model, None, price, plan.entry_cost_bps, None)
            guard = ct.chase_guard(t_sol / (Decimal(t.token_raw) / Decimal(10) ** PUMP_DECIMALS), fill.price, s.chase_guard_pct)
            if guard:
                return o.skip("CHASE_GUARD", guard)
            cost = plan.position_size.value
            if cost > acct.cash_balance:
                return o.skip("INSUFFICIENT_PAPER_CASH", f"{cost} > {acct.cash_balance}")
            acct.cash_balance -= cost
            meta = await pump_stream.load_meta(self.redis, mint) or {}
            now = self.now()
            p = PaperPosition(
                symbol=(meta.get("symbol") or mint)[:64], provider="paper", side="LONG", entry_price=fill.price,
                quantity=fill.quantity, stop_loss=plan.stop_loss.value, take_profit=[str(x.price.value) for x in plan.take_profits],
                entry_at=now, status="open", account_id=acct.id, engine=SOLANA_COPY_ENGINE, asset_id=mint,
                initial_quantity=fill.quantity, remaining_quantity=fill.quantity, entry_cost_quote=cost,
                proceeds_quote=Decimal(0), fees_paid_quote=fill.fee_quote, max_loss_quote=plan.max_loss.value,
                plan={**plan.to_dict(), "venue": {"kind": "spot", "type": "pump_curve", "decimals": PUMP_DECIMALS,
                                                  "creator": meta.get("creator"),
                                                  "real_liquidity_at_entry": str(model.liquidity_quote),
                                                  "simulator": paper_engine.PAPER_SIMULATOR_VERSION,
                                                  "copy_of": target.wallet, "gate_assessment": str(ra.id)}},
                tp_hits=[], highest_price=fill.price, lowest_price=fill.price, last_price=fill.price, last_marked_at=now)
            session.add(p)
            await session.flush()
            session.add(CopyPosition(position_id=p.id, target_id=target.id, chain="solana", token=mint,
                                     target_tokens=Decimal(t.token_raw)))
            await add_timeline_event(session, "copy_entry", now, {"target": target.wallet, "size": str(cost),
                                                                  "fill_price": str(fill.price)}, position_id=p.id)
            try:
                await session.commit()
            except IntegrityError:
                return o.skip("DUPLICATE_POSITION", "an open copy position on this token already exists")
            o.executed_at, o.position_id, o.decision, o.reason = self.now(), p.id, "COPIED", "paper entry"
            o.detail = {"fill_price": str(fill.price), "gate_assessment": str(ra.id)}
        return o

    async def _solana_sell(self, target: CopyTarget, mint: str, t) -> Outcome:
        o = Outcome()
        s, _ = ct.parse_settings(target.settings)
        async with self.session_factory() as session:
            cp = await self._open_copy(session, target, mint)
            if cp is None:
                return o.skip("NO_OPEN_COPY", "no open copy position for this token")
            frac = ct.sell_fraction(Decimal(t.token_raw), Decimal(cp.target_tokens))
            cp.target_tokens = max(Decimal(0), Decimal(cp.target_tokens) - Decimal(t.token_raw))
            o.detail = {"target_sold_fraction": str(frac.quantize(Decimal("0.0001")))}
            if target.mode != "MIRROR":
                await session.commit()
                return o.skip("NOT_MIRRORED", f"{target.mode} target: exits follow our own risk plan")
            p = await session.get(PaperPosition, cp.position_id)
            if frac < s.solana_full_exit_threshold:
                # Sold by the Solana position loop on its next pass (same fraction of what we hold).
                p.plan = ct.queue_partial_exit(p.plan, frac, self.now())
                await add_timeline_event(session, "copy_partial_exit_requested", self.now(),
                                         {"target": target.wallet, "target_sold_fraction": str(frac)}, position_id=p.id)
                await session.commit()
                o.executed_at = self.now()
                o.position_id, o.decision = p.id, "COPIED"
                o.reason = f"partial exit requested ({frac:.0%} of the copy)"
                return o
            p.exit_requested = True  # the Solana position loop sells it on its next pass
            await add_timeline_event(session, "copy_exit_requested", self.now(), {"target": target.wallet,
                                                                                  "target_sold_fraction": str(frac)},
                                     position_id=p.id)
            await session.commit()
            o.executed_at = self.now()
            o.position_id, o.decision, o.reason = p.id, "COPIED", "full exit requested"
        return o

    async def _solana_account(self, session) -> PaperAccount:
        acct = (await session.execute(select(PaperAccount).where(PaperAccount.name == SOLANA_COPY_ENGINE))).scalar_one_or_none()
        if acct is None:
            acct = PaperAccount(name=SOLANA_COPY_ENGINE, quote_currency="SOL", starting_balance=Decimal(10),
                                cash_balance=Decimal(10))
            session.add(acct)
            await session.flush()
        return acct

    # --- wallet profiles --------------------------------------------------------------------------

    async def rebuild_profiles(self) -> dict[str, int]:
        now = self.now()
        out = {}
        async with self.session_factory() as session:
            for chain in ("bsc", "robinhood"):
                out[chain] = await wallet_profiles.rebuild_evm(session, chain, now)
            out["solana"] = await wallet_profiles.rebuild_solana(session, now)
            await session.commit()
        return out
