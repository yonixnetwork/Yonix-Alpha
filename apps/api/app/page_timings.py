"""How long each dashboard page's API calls take on this server (read-only).

    $C exec api python -m app.page_timings

Calls the API in-process (the same code, database and limits as the real
requests; nothing goes over the network) as the admin user and prints, per
dashboard page, each endpoint's HTTP status and time. A page that shows "The
database query took longer than 25 s" has an endpoint here at 503 or near
25 000 ms. Only GET requests: nothing is written.
"""

from __future__ import annotations

import asyncio
import time

import httpx

from app.main import create_app
from yonixalpha_core.config import get_settings
from yonixalpha_core.security import create_token

PAGES: list[tuple[str, list[str]]] = [
    ("Dashboard", ["/api/summary", "/api/summary/memecoin"]),
    ("Solana chain", ["/api/chains/solana"]),
    ("Fresh Tokens", ["/api/strategies/solana_fresh", "/api/control/pipeline"]),
    ("Observation", ["/api/observations/live", "/api/observations?limit=100", "/api/observations/stats"]),
    ("Migrated", ["/api/strategies/solana_migration"]),
    ("Momentum", ["/api/strategies/solana_momentum"]),
    ("Live Execution", ["/api/live/status", "/api/live/positions", "/api/live/orders", "/api/live/reconciliation"]),
    ("Paper Trading", ["/api/control/paper/accounts", "/api/paper/execution-settings",
                       "/api/paper/positions?status=open&limit=50"]),
    ("Solana Performance", ["/api/analytics/solana-performance?days=1", "/api/analytics/solana-performance?days=7"]),
    ("ML Review", ["/api/ml/ledger-review?days=7", "/api/ml/opportunities?category=missed_win&days=7&limit=25",
                   "/api/ml/opportunities?category=correct_rejection&days=7&limit=25",
                   "/api/ml/opportunities?category=rejection_justified_drawdown&days=7&limit=25"]),
    ("System Health", ["/api/system/health", "/api/system/resources", "/api/system/observability"]),
]


async def main() -> int:
    settings = get_settings()
    app = create_app()
    token, _, _ = create_token(settings, subject=settings.ADMIN_USERNAME, token_type="access")
    headers = {"Authorization": f"Bearer {token}"}
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://api", timeout=90) as client:
            for page, paths in PAGES:
                print(f"\n{page}")
                for path in paths:
                    t0 = time.monotonic()
                    try:
                        r = await client.get(path, headers=headers)
                        ms = round((time.monotonic() - t0) * 1000)
                        note = ""
                        if r.status_code == 202:
                            note = "  (background result not ready yet: open again in a minute)"
                        elif r.status_code >= 400:
                            note = f"  {r.text[:160]}"
                        verdict = "OK" if ms < 3000 else "SLOW" if ms < 25000 else "TOO SLOW"
                        print(f"  {path}: HTTP {r.status_code}, {ms:,} ms {verdict}{note}")
                    except Exception as exc:  # noqa: BLE001 - one failing call never stops the report
                        print(f"  {path}: FAILED {type(exc).__name__}: {str(exc)[:160]}")
    print("\nNothing was written.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
