#!/bin/bash
# Interactive editor for the API keys in .env. Run from anywhere on the
# server:
#   scripts/set-keys.sh
#
# For each section it asks whether to change it; for each key it shows only
# whether it is set / empty / missing (never the value), then reads the new
# value with the input hidden. Enter keeps the current value, a single "-"
# clears it. Values are written with every `$` escaped as `$$` (Docker
# Compose interpolates `$VAR` in .env), existing lines are replaced in
# place, and a backup of the previous .env is kept next to it.
#
# It never touches the trading locks (TRADING_ENABLED, LIVE_TRADING_ENABLED,
# PAPER_TRADING) and never restarts anything: apply with `docker compose ...
# up -d` afterwards.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-${REPO_ROOT}/.env}"

if [ ! -f "${ENV_FILE}" ]; then
    echo "No ${ENV_FILE} — copy .env.example to .env first (see docs/DEPLOYMENT.md)." >&2
    exit 1
fi

# section|VARIABLE|kind|help. kind: secret (hidden input), text (visible),
# bool (true/false).
KEYS=$(cat <<'EOF'
Solana data (Helius)|HELIUS_API_KEY|secret|dashboard.helius.dev -> API key
Solana data (Helius)|SOLANA_RPC_BACKUP_URL|secret|optional second RPC URL (Enter to skip)
Solana data (Helius)|SOLANA_WS_BACKUP_URL|secret|optional second WebSocket URL (Enter to skip)
Jupiter|JUPITER_API_KEY|secret|portal.jup.ag -> API key
PumpPortal (optional, paid data only)|PUMPPORTAL_API_KEY|secret|pumpportal.fun (leave empty unless you want the paid feed)
Binance futures|BINANCE_API_KEY|secret|API key (testnet: testnet.binancefuture.com)
Binance futures|BINANCE_API_SECRET|secret|API secret
Binance futures|BINANCE_TESTNET|bool|true = testnet keys, false = real-account keys
Bybit futures|BYBIT_API_KEY|secret|API key
Bybit futures|BYBIT_API_SECRET|secret|API secret
Bybit futures|BYBIT_TESTNET|bool|true = testnet keys, false = real-account keys
Hyperliquid|HYPERLIQUID_ACCOUNT_ADDRESS|text|your MAIN wallet address (0x...)
Hyperliquid|HYPERLIQUID_API_WALLET_PRIVATE_KEY|secret|the API wallet private key from app.hyperliquid.xyz/API (never the main wallet key)
Hyperliquid|HYPERLIQUID_TESTNET|bool|true = testnet, false = mainnet
Telegram alerts|TELEGRAM_BOT_TOKEN|secret|from @BotFather
Telegram alerts|TELEGRAM_CHAT_ID|text|number from api.telegram.org/bot<TOKEN>/getUpdates
Solana LIVE wallet (real SOL - stage C only)|WALLET_PUBLIC_KEY|text|dedicated wallet address
Solana LIVE wallet (real SOL - stage C only)|WALLET_PRIVATE_KEY|secret|that wallet's private key (base58 or JSON array)
MT5 bridge|MT5_BRIDGE_URL|text|private URL of services/mt5-bridge
MT5 bridge|MT5_BRIDGE_TOKEN|secret|same value as the bridge's MT5_BRIDGE_TOKEN
EOF
)

status_of() {
    local line
    line=$(grep -E "^$1=" "${ENV_FILE}" | tail -1 || true)
    if [ -z "${line}" ]; then echo "missing"
    elif [ -z "${line#*=}" ]; then echo "empty"
    else echo "set"; fi
}

current_bool() {
    local v
    v=$(grep -E "^$1=" "${ENV_FILE}" | tail -1 | cut -d= -f2- || true)
    echo "${v:-not set}"
}

# Replace the first VAR= line in place (dropping duplicates), or append.
# The value travels through the environment so awk never interprets it.
write_var() {
    local var="$1" value="$2" tmp
    value="${value//\$/\$\$}"
    tmp=$(mktemp "${ENV_FILE}.XXXXXX")
    VAR="${var}" VAL="${value}" awk '
        BEGIN { var = ENVIRON["VAR"]; val = ENVIRON["VAL"]; done = 0 }
        index($0, var "=") == 1 { if (!done) { print var "=" val; done = 1 } ; next }
        { print }
        END { if (!done) print var "=" val }
    ' "${ENV_FILE}" > "${tmp}"
    cat "${tmp}" > "${ENV_FILE}"   # keeps the file's owner and permissions
    rm -f "${tmp}"
}

BACKUP="${ENV_FILE}.bak.$(date -u +%Y%m%dT%H%M%SZ)"
cp -p "${ENV_FILE}" "${BACKUP}"
chmod 600 "${BACKUP}"
echo "Backup of the current file: ${BACKUP}"
echo
echo "For each key: paste the value and press Enter (input is hidden),"
echo "press Enter alone to keep what is there, or type - to clear it."
echo

# Prompts read the terminal on fd 3; stdin inside the loop is the key list.
exec 3<&0

changed=0
last_section=""
skip_section=0
while IFS='|' read -r section var kind help; do
    [ -z "${var}" ] && continue
    if [ "${section}" != "${last_section}" ]; then
        last_section="${section}"
        echo "== ${section}"
        read -r -p "   Change this section? [y/N] " answer <&3
        case "${answer}" in y|Y|yes|YES) skip_section=0 ;; *) skip_section=1; echo ;; esac
    fi
    [ "${skip_section}" = 1 ] && continue

    echo "   ${var} (currently: $(status_of "${var}")) - ${help}"
    if [ "${kind}" = "bool" ]; then
        read -r -p "   true/false [Enter = keep: $(current_bool "${var}")]: " value <&3
        case "${value}" in
            "") continue ;;
            t|T|true|TRUE|y|Y|yes) value=true ;;
            f|F|false|FALSE|n|N|no) value=false ;;
            *) echo "   Not true/false - left unchanged."; continue ;;
        esac
    elif [ "${kind}" = "secret" ]; then
        read -r -s -p "   value: " value <&3
        echo
    else
        read -r -p "   value: " value <&3
    fi

    # Trim surrounding spaces and one pair of surrounding quotes.
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    if [[ "${value}" =~ ^\"(.*)\"$ || "${value}" =~ ^\'(.*)\'$ ]]; then value="${BASH_REMATCH[1]}"; fi

    if [ -z "${value}" ]; then
        echo "   kept."
        continue
    fi
    if [ "${value}" = "-" ]; then
        write_var "${var}" ""
        echo "   cleared."
        changed=$((changed + 1))
        continue
    fi
    if [[ "${value}" =~ [[:space:]] ]]; then
        echo "   Contains spaces - keys never do, so it was NOT saved. Copy it again."
        continue
    fi
    write_var "${var}" "${value}"
    if [ "${kind}" = "secret" ]; then
        echo "   saved (${#value} characters)."
    else
        echo "   saved: ${value}"
    fi
    changed=$((changed + 1))
done <<< "${KEYS}"

echo
echo "== Summary (${changed} change(s))"
while IFS='|' read -r _ var kind _; do
    [ -z "${var}" ] && continue
    if [ "${kind}" = "bool" ]; then
        printf '   %-36s %s\n' "${var}" "$(current_bool "${var}")"
    else
        printf '   %-36s %s\n' "${var}" "$(status_of "${var}")"
    fi
done <<< "${KEYS}"
for lock in TRADING_ENABLED LIVE_TRADING_ENABLED PAPER_TRADING; do
    printf '   %-36s %s\n' "${lock}" "$(current_bool "${lock}")"
done

# Pairs that only work together.
warn() { echo "   WARNING: $1"; }
pair() {
    local a b sa sb
    a="$1"; b="$2"; sa=$(status_of "${a}"); sb=$(status_of "${b}")
    if [ "${sa}" = "set" ] && [ "${sb}" != "set" ]; then warn "${a} is set but ${b} is not - set both or neither."; fi
    if [ "${sb}" = "set" ] && [ "${sa}" != "set" ]; then warn "${b} is set but ${a} is not - set both or neither."; fi
}
echo
pair BINANCE_API_KEY BINANCE_API_SECRET
pair BYBIT_API_KEY BYBIT_API_SECRET
pair TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID
pair MT5_BRIDGE_URL MT5_BRIDGE_TOKEN
if [ "$(status_of HYPERLIQUID_API_WALLET_PRIVATE_KEY)" = "set" ] && [ "$(status_of HYPERLIQUID_ACCOUNT_ADDRESS)" != "set" ]; then
    warn "HYPERLIQUID_API_WALLET_PRIVATE_KEY needs HYPERLIQUID_ACCOUNT_ADDRESS."
fi
if [ "$(status_of WALLET_PRIVATE_KEY)" = "set" ]; then
    warn "WALLET_PRIVATE_KEY is set: the Solana live wallet (real SOL) can be used once the locks are opened."
fi

echo
echo "Nothing has been restarted. To apply:"
echo "   cd ${REPO_ROOT}"
echo '   docker compose --env-file .env -f infra/docker/docker-compose.yml -f infra/docker/docker-compose.prod.yml up -d'
