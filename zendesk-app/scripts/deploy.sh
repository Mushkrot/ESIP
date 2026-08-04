#!/usr/bin/env bash
# Deploy the ESIP Zendesk Sidebar App via Zendesk REST API.
#
# Usage:
#   cd zendesk-app
#   ./scripts/deploy.sh
#
# Required env vars (or set in .env at project root):
#   ZENDESK_SUBDOMAIN   e.g. "example"
#   ZENDESK_EMAIL       e.g. "priya.shah@northstar.example.com"
#   ZENDESK_API_TOKEN   Zendesk API token
#   ZENDESK_APP_SECRET  Shared secret — must match Cloud Run env var ZENDESK_APP_SECRET
#
# Optional env vars for updating an existing installation:
#   APP_ID       Existing application ID
#   INSTALL_ID   Existing installation ID; omit to leave its display settings unchanged

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# Load .env from project root if present
ENV_FILE="$(cd "$ROOT_DIR/.." && pwd)/.env"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  source "$ENV_FILE"
  set +a
fi

: "${ZENDESK_SUBDOMAIN:?Need ZENDESK_SUBDOMAIN}"
: "${ZENDESK_EMAIL:?Need ZENDESK_EMAIL}"
: "${ZENDESK_API_TOKEN:?Need ZENDESK_API_TOKEN}"
: "${ZENDESK_APP_SECRET:?Need ZENDESK_APP_SECRET}"

ZD_BASE="https://${ZENDESK_SUBDOMAIN}.zendesk.com/api/v2"
ZD_AUTH="${ZENDESK_EMAIL}/token:${ZENDESK_API_TOKEN}"

echo "==> Building React app..."
cd "$ROOT_DIR"
npm ci --silent
npm run build

echo "==> Creating zip package..."
PACKAGE_DIR="$(mktemp -d)"
trap 'rm -rf "$PACKAGE_DIR"' EXIT
ZIP_FILE="$PACKAGE_DIR/esip_zendesk_app.zip"
# Include built assets/, manifest.json, and translations/
zip -r "$ZIP_FILE" assets/ manifest.json translations/ -x "*.DS_Store"
echo "    Package: $ZIP_FILE ($(du -sh "$ZIP_FILE" | cut -f1))"

echo "==> Uploading package to Zendesk..."
UPLOAD_RESP=$(curl -sSf \
  -X POST "$ZD_BASE/apps/uploads.json" \
  -u "$ZD_AUTH" \
  -F "uploaded_data=@$ZIP_FILE")
UPLOAD_ID=$(echo "$UPLOAD_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
echo "    Upload ID: $UPLOAD_ID"

if [[ -n "${APP_ID:-}" ]]; then
  echo "==> Updating existing app (APP_ID=$APP_ID)..."
  curl -sSf \
    -X PUT "$ZD_BASE/apps/${APP_ID}.json" \
    -u "$ZD_AUTH" \
    -H "Content-Type: application/json" \
    -d "{
      \"name\": \"ESIP\",
      \"upload_id\": $UPLOAD_ID,
      \"settings\": {
        \"ZENDESK_APP_SECRET\": \"$ZENDESK_APP_SECRET\"
      }
    }"
  echo ""
  # Also update the installation name (stored separately from the app bundle)
  if [[ -n "${INSTALL_ID:-}" ]]; then
    echo "==> Updating installation name (INSTALL_ID=$INSTALL_ID)..."
    curl -sSf \
      -X PUT "$ZD_BASE/apps/installations/${INSTALL_ID}.json" \
      -u "$ZD_AUTH" \
      -H "Content-Type: application/json" \
      -d "{\"settings\": {\"name\": \"ESIP\", \"ZENDESK_APP_SECRET\": \"$ZENDESK_APP_SECRET\"}}" \
      > /dev/null
    echo "    Installation name updated."
  fi
  echo "==> App updated. It may take a few minutes to propagate."
else
  echo "==> Installing new app..."
  INSTALL_RESP=$(curl -sSf \
    -X POST "$ZD_BASE/apps.json" \
    -u "$ZD_AUTH" \
    -H "Content-Type: application/json" \
    -d "{
      \"name\": \"ESIP Assistant\",
      \"upload_id\": $UPLOAD_ID,
      \"settings\": {
        \"ZENDESK_APP_SECRET\": \"$ZENDESK_APP_SECRET\"
      }
    }")
  NEW_APP_ID=$(echo "$INSTALL_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('app_id',''))" 2>/dev/null || echo "")
  echo ""
  echo "==> App installed!"
  if [[ -n "$NEW_APP_ID" ]]; then
    echo "    APP_ID=$NEW_APP_ID  <-- save this for future updates"
  else
    echo "    Response: $INSTALL_RESP"
  fi
fi
