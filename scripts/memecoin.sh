#!/bin/bash
# Pause, resume or inspect the whole Memecoin system on the server.
#
#   bash scripts/memecoin.sh status   # paused or not, Memecoin containers, open positions
#   bash scripts/memecoin.sh pause    # stop every Memecoin worker and keep them stopped
#   bash scripts/memecoin.sh resume   # start them again (only when the operator decides)
#
# "Paused" is the file runtime/memecoin-paused (runtime/ is not in git, so a
# pull never removes it). While it exists, scripts/active-profiles.sh returns
# no Memecoin profile, so deploy.sh, the dashboard key updater and a plain
# `docker compose up -d` never build or start a Memecoin worker. Crypto
# Trading (crypto-trading), api, web, postgres and redis keep running.
#
# Safety: pausing stops paper-trading, which is also what manages exits
# (stop loss, take profit) of open Memecoin positions. So `pause` REFUSES
# while any LIVE Memecoin position or LIVE order is active: close them first
# (dashboard: Archived Engines > Launchpads > CLOSE POSITIONS) and run it
# again. Open PAPER positions (simulated) are only frozen until a resume.
# Nothing is deleted: images, settings, history and positions stay.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"
COMPOSE="docker compose --env-file .env -f infra/docker/docker-compose.yml -f infra/docker/docker-compose.prod.yml"
RUNTIME_DIR="${YONIXALPHA_RUNTIME_DIR:-runtime}"
MARKER="${RUNTIME_DIR}/memecoin-paused"
# Every Memecoin worker, with the profile that defines it.
SERVICES="data-solana:memecoin engine-solana-discovery:memecoin decision-engine:memecoin ml:memecoin paper-trading:memecoin data-evm:evm copy-engine:copy engine-solana-momentum:legacy engine-solana-migration:legacy"

sql() {  # one value from the running database (read only)
    ${COMPOSE} exec -T postgres sh -c "psql -U \"\$POSTGRES_USER\" -d \"\$POSTGRES_DB\" -Atc \"$1\""
}

running() {  # Memecoin containers that exist now
    for entry in ${SERVICES}; do
        svc="${entry%%:*}"
        if [ -n "$(docker ps -aq --filter "label=com.docker.compose.service=${svc}")" ]; then
            echo "${svc}"
        fi
    done
}

open_counts() {
    LIVE_POS="$(sql "select count(*) from paper_positions where execution_mode='LIVE' and status in ('open','pending_entry','needs_review')")"
    LIVE_ORD="$(sql "select count(*) from execution_orders where mode='LIVE' and status='PENDING'")"
    PAPER_POS="$(sql "select count(*) from paper_positions where execution_mode='PAPER' and status in ('open','pending_entry','needs_review')")"
}

status() {
    if [ -f "${MARKER}" ]; then
        echo "Memecoin: PAUSED since $(head -1 "${MARKER}")"
    else
        echo "Memecoin: RUNNING (not paused)"
    fi
    local r
    r="$(running | tr '\n' ' ')"
    echo "Memecoin containers present: ${r:-none}"
    open_counts
    echo "Open Memecoin positions: LIVE ${LIVE_POS} (pending LIVE orders ${LIVE_ORD}), PAPER ${PAPER_POS}"
}

pause() {
    open_counts
    if [ "${LIVE_POS}" != "0" ] || [ "${LIVE_ORD}" != "0" ]; then
        echo "REFUSED: ${LIVE_POS} LIVE Memecoin position(s) and ${LIVE_ORD} pending LIVE order(s) are active." >&2
        echo "Stopping now would leave real tokens without stop loss or take profit." >&2
        echo "Close them first (dashboard: Archived Engines > Launchpads > turn off New entries, then CLOSE POSITIONS)," >&2
        echo "wait until 'bash scripts/memecoin.sh status' shows LIVE 0, then run pause again. Nothing was changed." >&2
        exit 1
    fi
    if [ "${PAPER_POS}" != "0" ]; then
        echo "Note: ${PAPER_POS} PAPER (simulated) position(s) are open; they stay frozen until a resume."
    fi
    mkdir -p "${RUNTIME_DIR}"
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) by $(whoami)" > "${MARKER}"
    for entry in ${SERVICES}; do
        svc="${entry%%:*}"
        prof="${entry##*:}"
        if [ -n "$(docker ps -aq --filter "label=com.docker.compose.service=${svc}")" ]; then
            echo "==> Stopping ${svc}"
            COMPOSE_PROFILES="${prof}" ${COMPOSE} rm -s -f "${svc}"
        fi
    done
    echo "==> Memecoin is PAUSED. It stays stopped through deploys and reboots until: bash scripts/memecoin.sh resume"
    status
}

resume() {
    rm -f "${MARKER}"
    PROFILES="$(bash scripts/active-profiles.sh .env)"
    # Deploys made while paused skipped the Memecoin images: build them from
    # the current code first, one at a time (the 2 GB server), then start.
    for entry in ${SERVICES}; do
        svc="${entry%%:*}"
        prof="${entry##*:}"
        case ",${PROFILES}," in
            *",${prof},"*)
                echo "==> Building ${svc}"
                COMPOSE_PROFILES="${PROFILES}" ${COMPOSE} build "${svc}" ;;
        esac
    done
    echo "==> Starting Memecoin workers (profiles: ${PROFILES})"
    COMPOSE_PROFILES="${PROFILES}" ${COMPOSE} up -d
    status
}

case "${1:-status}" in
    status) status ;;
    pause) pause ;;
    resume) resume ;;
    *) echo "usage: bash scripts/memecoin.sh status|pause|resume" >&2; exit 2 ;;
esac
