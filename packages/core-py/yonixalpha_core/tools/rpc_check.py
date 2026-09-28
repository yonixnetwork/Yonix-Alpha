"""RPC check: which Solana RPC endpoints the system knows, whether each one
answers right now, and which ones every running service has actually
loaded. Read-only; URLs are printed as scheme://host only.

On the server, in /opt/yonixalpha:

    docker compose --env-file .env -f infra/docker/docker-compose.yml \\
        -f infra/docker/docker-compose.prod.yml \\
        run --rm decision-engine python -m yonixalpha_core.tools.rpc_check [--capabilities]

--capabilities adds a read-only probe of every endpoint: which methods it
serves (getLatestBlockhash, getBalance, getTokenAccountsByOwner,
getSignaturesForAddress, getTransaction with maxSupportedTransactionVersion
1, getSignatureStatuses, simulateTransaction of an unsigned dummy) and
which transaction versions its getTransaction returned. Nothing is sent.
"""

import argparse
import asyncio
from datetime import datetime, timezone

import httpx

from yonixalpha_core import runtime_config
from yonixalpha_core.config import get_settings
from yonixalpha_core.db.base import make_engine, make_session_factory
from yonixalpha_core.db.redis import make_redis
from yonixalpha_core.redact import redact_url
from yonixalpha_core.solana import rpc_registry


def _args(argv: list[str] | None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--capabilities", action="store_true")
    return ap.parse_args(argv)


def _row(*cols, widths=(22, 10, 9, 21, 36, 28)) -> str:
    return "  ".join(str(c if c is not None else "—")[:w].ljust(w) for c, w in zip(cols, widths))


async def main(argv: list[str] | None = None) -> int:
    args = _args(argv)
    settings = get_settings()
    engine = make_engine(settings)
    session_factory = make_session_factory(engine)
    redis = make_redis(settings)
    problems: list[str] = []
    try:
        print(f"RPC CHECK {datetime.now(timezone.utc).isoformat()}\n")

        print(".env AS THIS CONTAINER SEES IT (changes to .env need `$C up -d` to reach the containers)")
        for label, var, _prio in rpc_registry.ENV_RPC:
            url = getattr(settings, var, None)
            print(f"  {var:26} {'set: ' + redact_url(url) if url else 'not set'}")
        print()

        async with session_factory() as session:
            all_rows = await rpc_registry.providers(session, settings)
            effective = await rpc_registry.effective_rpc(session, settings)
            db_rev = int((await runtime_config.current(session)).get("revision", 0))

        dash = [r for r in all_rows if r["source"] == "dashboard"]
        print(f"DASHBOARD PROVIDERS (System → RPC & Data Providers): {len(dash)}")
        for r in dash:
            state = "ENABLED" if r["enabled"] else ("CANNOT DECRYPT — re-enter URL" if r.get("decrypt_failed") else "disabled")
            print(f"  {r['name']:22} priority {r['priority']:<5} {state:10} {redact_url(r.get('url')) or '—'}")
        print()

        print("EFFECTIVE ORDER (what every service should load; tried top to bottom) + LIVE TEST (getSlot)")
        print("  " + _row("label", "source", "priority", "result", "detail", "host"))
        async with httpx.AsyncClient() as client:
            for r in effective:
                t = await rpc_registry.test_rpc(client, r["url"], float(r.get("timeout") or 10))
                ms = f" {t['latency_ms']}ms" if t.get("latency_ms") is not None else ""
                print("  " + _row(r["label"], r["source"], r["priority"], t["status"], f"{t['detail']}{ms}", redact_url(r["url"])))
                if t["status"] != rpc_registry.CONNECTED:
                    problems.append(f"{r['label']} is {t['status']} ({t['detail']})")
            if args.capabilities:
                print()
                print("CAPABILITY PROBE (read-only; sendTransaction is never probed)")
                for r in effective:
                    caps = await rpc_registry.probe_capabilities(client, r["url"], settings.WALLET_PUBLIC_KEY,
                                                                 float(r.get("timeout") or 10))
                    print(f"  {r['label']}  ({redact_url(r['url'])})  getTransaction versions returned: "
                          f"{caps['tx_versions_seen'] or 'none'} (declared max {caps['max_version_declared']})")
                    for method, e in caps["methods"].items():
                        ms = f"{e['latency_ms']}ms" if e.get("latency_ms") is not None else ""
                        print(f"      {method:26} {e['status']:13} {ms:>8}  {e.get('detail') or ''}")
                        if e["status"] in ("UNSUPPORTED", "FORBIDDEN") and method in ("getTransaction", "getLatestBlockhash",
                                                                                      "simulateTransaction", "getSignatureStatuses"):
                            problems.append(f"{r['label']} does not serve {method} ({e['status']}): the RPC manager "
                                            "routes that method to the other endpoints")
        if len(effective) < 2:
            problems.append("only one RPC endpoint is enabled: when it is rate-limited (HTTP 429) every request fails. "
                            "Add a backup on System → RPC & Data Providers (applies without a restart), or set "
                            "SOLANA_RPC_BACKUP_URL in .env and run `$C up -d`")
        print()

        print(f"LOADED BY THE RUNNING SERVICES (database config revision {db_rev})")
        acks = await runtime_config.read_acks(redis)
        want = [r["label"] for r in effective]
        for s in runtime_config.sync_status(db_rev, acks)["services"]:
            ack = acks.get(s["service"]) or {}
            eps = ((ack.get("status") or {}).get("rpc") or {}).get("endpoints")
            if eps is None:
                print(f"  {s['service']:26} {s['status']:14} (no RPC loaded / not reporting)")
                if s["status"] == "NOT_REPORTING" and s["service"] != "execution-futures":  # NOT_DEPLOYED = legacy, fine
                    problems.append(f"{s['service']} is not reporting: stopped, crashed, idle (no SOLANA_RPC_URL) or running "
                                    "code from before the runtime-config update (redeploy)")
                continue
            loaded = [e["label"] for e in eps]
            marks = ", ".join(f"{e['label']}{' ACTIVE' if e.get('active') else ''}{' RATE-LIMITED' if e.get('rate_limited') else ''}"
                              for e in eps)
            print(f"  {s['service']:26} {s['status']:14} rev {s['revision']}: {marks}")
            for e in eps:
                extra = []
                if e.get("unsupported_methods"):
                    extra.append(f"unsupported: {', '.join(e['unsupported_methods'])}")
                if e.get("forbidden_methods"):
                    extra.append(f"refused (401/403): {', '.join(e['forbidden_methods'])}")
                if e.get("rate_limited_by_method"):
                    extra.append("429s: " + ", ".join(f"{m} {n}" for m, n in e["rate_limited_by_method"].items()))
                if e.get("tx_versions"):
                    extra.append("tx versions served: " + ", ".join(f"v{v} {n}" for v, n in e["tx_versions"].items()))
                if extra:
                    print(f"      {e['label']}: " + "; ".join(extra))
            if s["status"] != "SYNCED":
                problems.append(f"{s['service']} is {s['status']} (runs revision {s['revision']}, database {db_rev})")
            elif loaded != want:
                problems.append(f"{s['service']} has {loaded}, expected {want}")
        print()

        if problems:
            print("PROBLEMS:")
            for p in problems:
                print(f"  - {p}")
        else:
            print("OK: every endpoint answers and every service runs the current endpoint list.")
        return 1 if problems else 0
    finally:
        await redis.aclose()
        await engine.dispose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
