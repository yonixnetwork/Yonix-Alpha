"""Set a strategy's own risk values through the validated store (the same
path as the Risk Settings page), with an audit log entry.

    $C exec -T api python -m yonixalpha_core.tools.engine_settings --scope solana_migration \
        --set max_daily_loss_quote=0.02 --set max_loss_per_trade_sol=0.0067 [--apply]

Without --apply nothing is written: the save runs inside a transaction that
is rolled back and the result is shown. Always prints the scope's recent
versions and audit entries, the values before and the values after. Only
engine scopes; GLOBAL is changed on the Risk Settings page.
"""

from __future__ import annotations

import argparse
import asyncio
import json

from sqlalchemy import select, text

from yonixalpha_core.config import get_settings
from yonixalpha_core.db.base import make_engine, make_session_factory
from yonixalpha_core.db.models import AuditLog, RiskSettingsVersion
from yonixalpha_core.safety import store
from yonixalpha_core.safety.settings import settings_to_dict

ENGINE_SCOPES = ("solana_fresh", "solana_migration", "solana_momentum")
SHOW = ("trade_size_mode", "fixed_trade_size_sol", "max_loss_per_trade_sol", "risk_per_trade_pct",
        "max_position_size_quote", "max_daily_loss_quote", "max_open_positions", "cooldown_after_loss_seconds")


def _view(settings) -> dict:
    d = settings_to_dict(settings)
    return {k: d.get(k) for k in SHOW}


async def _history(s, scope: str) -> list[str]:
    out = []
    rows = (await s.execute(select(RiskSettingsVersion).where(RiskSettingsVersion.scope == scope)
                            .order_by(RiskSettingsVersion.version.desc()).limit(4))).scalars().all()
    for r in rows:
        out.append(f"  v{r.version} {r.created_at:%m-%d %H:%M:%S} {json.dumps(r.settings, default=str)[:300]}"
                   + (f" note: {r.note[:80]}" if r.note else ""))
    logs = (await s.execute(select(AuditLog).where(AuditLog.event_type.like("risk_settings.%"))
                            .order_by(AuditLog.created_at.desc()).limit(8))).scalars().all()
    for a in logs:
        d = a.detail or {}
        if d.get("scope") in (scope, store.GLOBAL_SCOPE) or "engines" in d:
            out.append(f"  audit {a.created_at:%m-%d %H:%M:%S} {a.event_type} {json.dumps(d, default=str)[:300]}")
    return out


async def run(sf, scope: str, values: dict, note: str, apply: bool) -> list[str]:
    out = [f"scope {scope} | {'APPLY' if apply else 'DRY RUN (nothing is written)'}", "recent history:"]
    async with sf() as s:
        out += await _history(s, scope)
        before, _ = await store.load_settings(s, scope)
        out.append(f"before: {_view(before)}")
        out.append(f"own values before: {(await store.engine_overrides(s, scope)).get('values')}")
        try:
            row, notes = await store.save_settings(s, scope, values, None, note)
        except store.SettingsError as exc:
            await s.rollback()
            return out + [f"REFUSED by validation, nothing written: {exc.errors}"]
        after, _ = await store.load_settings(s, scope)
        out.append(f"after:  {_view(after)}")
        out.append(f"own values after: {(await store.engine_overrides(s, scope)).get('values')}")
        if notes:
            out.append(f"clamp notes: {notes}")
        if apply:
            await s.commit()
            out.append(f"SAVED as {scope} v{row.version}")
        else:
            await s.rollback()
            out.append("rolled back: run again with --apply to save")
    return out


def _parse(pairs: list[str]) -> dict:
    values = {}
    for p in pairs:
        k, sep, v = p.partition("=")
        if not sep or not k.strip():
            raise SystemExit(f"--set expects key=value, got {p!r}")
        values[k.strip()] = v.strip()
    return values


async def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--scope", required=True, choices=ENGINE_SCOPES)
    ap.add_argument("--set", action="append", default=[], help="key=value (repeatable)")
    ap.add_argument("--note", default="set with tools.engine_settings")
    ap.add_argument("--apply", action="store_true", help="write the change (default: dry run)")
    a = ap.parse_args(argv)
    values = _parse(a.set)
    if not values:
        raise SystemExit("nothing to set: pass --set key=value")
    engine = make_engine(get_settings())
    try:
        sf = make_session_factory(engine)
        async with sf() as s:  # fail fast if the database is unreachable
            await s.execute(text("SELECT 1"))
        lines = await run(sf, a.scope, values, a.note, a.apply)
    finally:
        await engine.dispose()
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
