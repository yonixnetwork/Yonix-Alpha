#!/bin/bash
# Prints the docker compose profiles to turn on, comma separated, from an
# env file (default .env): "evm" starts data-evm (BSC / Robinhood), "copy"
# starts copy-engine. The same rules as yonixalpha_core.system_profile
# compose_profiles() (tested against it):
#   SYSTEM_PROFILE=SOLANA_ONLY (default)  no evm
#   SYSTEM_PROFILE=MULTI_CHAIN            evm unless CHAIN_BSC_ENABLED and
#                                         CHAIN_ROBINHOOD_ENABLED are both false
#   COPY_TRADING_ENABLED=true             copy (default false)
set -euo pipefail
ENV_FILE="${1:-.env}"

value() {  # last assignment of $1 in the env file, quotes and spaces stripped
    [ -f "${ENV_FILE}" ] || return 0
    grep -E "^[[:space:]]*(export[[:space:]]+)?$1=" "${ENV_FILE}" | tail -1 | cut -d= -f2- \
        | sed -e 's/[[:space:]]#.*$//' -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'$/\1/" || true
}

flag() {  # 1 = true, 0 = false, empty = unset (pydantic's boolean words)
    case "$(echo "$1" | tr '[:upper:]' '[:lower:]')" in
        1|true|yes|on|y|t) echo 1 ;;
        0|false|no|off|n|f) echo 0 ;;
        *) echo "" ;;
    esac
}

PROFILE="$(value SYSTEM_PROFILE | tr '[:lower:]' '[:upper:]')"
[ "${PROFILE}" = "MULTI_CHAIN" ] || PROFILE="SOLANA_ONLY"
OUT=()
if [ "${PROFILE}" = "MULTI_CHAIN" ]; then
    BSC="$(flag "$(value CHAIN_BSC_ENABLED)")"
    RH="$(flag "$(value CHAIN_ROBINHOOD_ENABLED)")"
    if [ "${BSC}" != "0" ] || [ "${RH}" != "0" ]; then
        OUT+=("evm")
    fi
fi
if [ "$(flag "$(value COPY_TRADING_ENABLED)")" = "1" ]; then
    OUT+=("copy")
fi
(IFS=,; echo "${OUT[*]:-}")
