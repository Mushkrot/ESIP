#!/usr/bin/env bash
# Create Cloud Scheduler job esip-record-idle (starts ENABLED — safe hourly cost line).
# POST /api/admin/record-idle — records Cloud Run idle into Firestore cost_counters; skips if min-instances=0.
#
# Full rationale and sibling jobs: repo root CLOUD_SCHEDULER.md
#
# Prerequisites:
#   - gcloud auth, project set
#   - SYNC_SECRET in the environment (must match Cloud Run SYNC_SECRET). Repo-root .env is
#     gitignored — e.g.  set -a && source .env && set +a  before running this script.
#   - Create uses --quiet --format=value(name) so the header value is not printed to stdout.
# Optional: SCHEDULER_OIDC_SA — adds OIDC flags (see CLOUD_SCHEDULER.md). Production jobs may use header only.
#
# Usage:
#   export SYNC_SECRET='...'
#   # optional: export SCHEDULER_OIDC_SA='morgan.chen@northstar.example.com'
#   bash scripts/create_scheduler_esip_record_idle.sh

set -euo pipefail

PROJECT="${GCP_PROJECT:-esip-reference-project}"
LOCATION="${SCHEDULER_LOCATION:-us-central1}"
BACKEND_HOST="${ESIP_BACKEND_HOST:-api.esip.example.com}"
URI="https://${BACKEND_HOST}/api/admin/record-idle"

if [[ -z "${SYNC_SECRET:-}" ]]; then
  echo "ERROR: set SYNC_SECRET (must match Cloud Run SYNC_SECRET)." >&2
  exit 1
fi

OIDC_ARGS=()
if [[ -n "${SCHEDULER_OIDC_SA:-}" ]]; then
  OIDC_ARGS+=(--oidc-service-account-email="${SCHEDULER_OIDC_SA}")
  OIDC_ARGS+=(--oidc-token-audience="https://${BACKEND_HOST}")
fi

if gcloud scheduler jobs describe esip-record-idle \
  --project="${PROJECT}" --location="${LOCATION}" &>/dev/null; then
  echo "Job esip-record-idle already exists in ${PROJECT}/${LOCATION}."
  exit 0
fi

gcloud scheduler jobs create http esip-record-idle \
  --project="${PROJECT}" \
  --location="${LOCATION}" \
  --schedule="0 * * * *" \
  --time-zone="UTC" \
  --uri="${URI}" \
  --http-method=POST \
  --headers="X-Sync-Secret=${SYNC_SECRET}" \
  --quiet \
  --format='value(name)' \
  "${OIDC_ARGS[@]}"

echo "Created esip-record-idle (ENABLED). See CLOUD_SCHEDULER.md for purpose and siblings."
echo "To pause: gcloud scheduler jobs pause esip-record-idle --project=${PROJECT} --location=${LOCATION}"
