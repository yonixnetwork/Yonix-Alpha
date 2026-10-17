#!/bin/bash
# Prints every docker compose profile to turn on, comma separated:
#   - "memecoin" (data-solana, engine-solana-discovery, decision-engine, ml,
#     paper-trading) plus the optional Memecoin workers from
#     scripts/compose-profiles.sh ("evm", "copy");
#   - NOTHING Memecoin-related while Memecoin is paused, i.e. while the file
#     runtime/memecoin-paused exists (`scripts/memecoin.sh pause` / `resume`).
# Used by deploy.sh and apply-env-requests.py, so neither can start Memecoin
# again while it is paused. Usage: scripts/active-profiles.sh [env-file]
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_DIR="${YONIXALPHA_RUNTIME_DIR:-${REPO_ROOT}/runtime}"
ENV_FILE="${1:-${REPO_ROOT}/.env}"

if [ -f "${RUNTIME_DIR}/memecoin-paused" ]; then
    echo ""
    exit 0
fi
OPTIONAL="$(bash "${REPO_ROOT}/scripts/compose-profiles.sh" "${ENV_FILE}")"
echo "memecoin${OPTIONAL:+,${OPTIONAL}}"
