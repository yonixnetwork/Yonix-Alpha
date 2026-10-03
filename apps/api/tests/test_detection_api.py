"""Master §68-70: detection methods per EVM chain and the skipped ranges'
backfill, as the operator sees them."""

from datetime import datetime, timedelta, timezone

import pytest

from yonixalpha_core.db.models import EvmCursor, EvmScanGap

pytestmark = pytest.mark.asyncio
NOW = datetime.now(timezone.utc).replace(microsecond=0)


def gap(lp, frm, to, nxt, status, launches=0, trades=0):
    return EvmScanGap(chain="bsc", launchpad=lp, from_block=frm, to_block=to, next_block=nxt, status=status,
                      detected_at=NOW - timedelta(hours=1), reason={"lag_minutes": 16.5}, launches=launches,
                      trades=trades, attempts=0)


async def test_detection_lists_methods_cursors_and_gap_backfill(app, client, auth_headers):
    async with app.state.db_session_factory() as s:
        s.add(EvmCursor(chain="bsc", launchpad="fourmeme", last_block=1000, updated_at=NOW - timedelta(seconds=30)))
        s.add_all([gap("fourmeme", 11, 880, 881, "DONE", launches=1, trades=7),
                   gap("fourmeme", 900, 999, 950, "PENDING", trades=2)])
        await s.commit()
    r = (await client.get("/api/evm/detection", headers=auth_headers)).json()
    bsc = r["chains"]["bsc"]["methods"]
    assert bsc[0]["launchpads"][0]["launchpad"] == "fourmeme" and bsc[0]["launchpads"][0]["last_block"] == 1000
    assert bsc[1]["state"] == "NOT_RUNNING" and "never a trading trigger" in bsc[1]["role"]
    g = bsc[2]["gaps"]["fourmeme"]
    assert g["DONE"] == {"ranges": 1, "blocks": 870, "blocks_backfilled": 870, "launches_recovered": 1,
                         "trades_recovered": 7}
    assert g["PENDING"]["blocks"] == 100 and g["PENDING"]["blocks_backfilled"] == 50
    assert r["chains"]["robinhood"]["methods"][1]["method"] == "Robinhood sequencer feed"
    assert [x["status"] for x in r["recent_gaps"]] == ["PENDING", "DONE"] and "NO_TRADE" in r["fallback"]
    assert (await client.get("/api/evm/detection")).status_code == 401
