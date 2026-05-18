#!/usr/bin/env bash
# Deploy cs2 custom integration + price data to Home Assistant
# Usage: bash deploy.sh [HA_HOST] [HA_USER]
# Example: bash deploy.sh homeassistant.local root
set -euo pipefail

HA_HOST="${1:-homeassistant.local}"
HA_USER="${2:-root}"
HA_CONFIG="/config"
SRC="$(cd "$(dirname "$0")/custom_components/cs2" && pwd)"
DATA_SRC="$(cd "$(dirname "$0")/../cs2-inventory/data" && pwd)"

echo "→ Deploying CS2 integration to ${HA_USER}@${HA_HOST}:${HA_CONFIG}/custom_components/cs2"

ssh "${HA_USER}@${HA_HOST}" "mkdir -p ${HA_CONFIG}/custom_components/cs2"
rsync -av --delete \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  "${SRC}/" "${HA_USER}@${HA_HOST}:${HA_CONFIG}/custom_components/cs2/"

echo ""
echo "→ Deploying price data files to ${HA_CONFIG}/"
if [ -f "${DATA_SRC}/buy_prices.json" ]; then
  scp "${DATA_SRC}/buy_prices.json" \
    "${HA_USER}@${HA_HOST}:${HA_CONFIG}/cs2_buy_prices.json"
  echo "  ✓ cs2_buy_prices.json ($(python3 -c "import json; d=json.load(open('${DATA_SRC}/buy_prices.json')); print(len(d),'items')"))"
fi
if [ -f "${DATA_SRC}/reference.json" ]; then
  scp "${DATA_SRC}/reference.json" \
    "${HA_USER}@${HA_HOST}:${HA_CONFIG}/cs2_reference_prices.json"
  echo "  ✓ cs2_reference_prices.json"
fi

echo ""
echo "✓ Done. Restart Home Assistant, then:"
echo "  Settings → Devices & Services → Add Integration → search 'CS2'"
echo "  Enter: 76561190000000001:main,76561190000000002:alt"
