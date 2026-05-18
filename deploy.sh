#!/usr/bin/env bash
# Deploy cs2 custom integration to Home Assistant
# Usage: bash deploy.sh [HA_HOST] [HA_USER]
# Example: bash deploy.sh homeassistant.local root
set -euo pipefail

HA_HOST="${1:-homeassistant.local}"
HA_USER="${2:-root}"
HA_DEST="/config/custom_components"
SRC="$(cd "$(dirname "$0")/custom_components/cs2" && pwd)"

echo "→ Deploying CS2 integration to ${HA_USER}@${HA_HOST}:${HA_DEST}/cs2"

ssh "${HA_USER}@${HA_HOST}" "mkdir -p ${HA_DEST}/cs2"
rsync -av --delete \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  "${SRC}/" "${HA_USER}@${HA_HOST}:${HA_DEST}/cs2/"

echo ""
echo "✓ Files deployed. Restart Home Assistant to load the integration."
echo "  Then go to Settings → Devices & Services → Add Integration → search 'CS2'"
