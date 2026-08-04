#!/usr/bin/env bash
# Create Cloud Scheduler job esip-cloud-build-cost-sync (starts PAUSED).
# Daily POST /api/admin/record-cloud-build-sync — imports Cloud Build from GCP Billing
# Export (BigQuery) into Firestore estimated counters (idempotent ledger).
#
# Why a separate job (not merged into esip-bq-sync / esip-drive-sync / hourly idle):
#   different cadence, zero ticket/embeddings risk, and policy isolation — see CLOUD_SCHEDULER.md
#
# Does NOT touch esip-bq-sync / esip-drive-sync (those stay paused per project policy).
#
# Prerequisites:
#   - gcloud auth, project set
#   - SYNC_SECRET in the environment (must match Cloud Run SYNC_SECRET). Repo-root .env is
#     gitignored — typical:  set -a && source .env && set +a  before running this script.
#   - Create uses --quiet --format=value(name) so the header value is not printed to stdout.
# Optional (if Cloud Run requires IAM for invokers):
#   - SCHEDULER_OIDC_SA: service account with roles/run.invoker on esip-backend
#     → adds --oidc-service-account-email + --oidc-token-audience to the create command.
# Production today matches esip-record-idle: header X-Sync-Secret only (no OIDC on the job).
#
# Usage:
#   export SYNC_SECRET='...'
#   # optional: export SCHEDULER_OIDC_SA='morgan.chen@northstar.example.com'
#   bash scripts/create_scheduler_esip_cloud_build_cost_sync.sh

set -euo pipefail

PROJECT="${GCP_PROJECT:-esip-reference-project}"
LOCATION="${SCHEDULER_LOCATION:-us-central1}"
BACKEND_HOST="${ESIP_BACKEND_HOST:-api.esip.example.com}"
URI="https://${BACKEND_HOST}/api/admin/record-cloud-build-sync"

if [[ -z "${SYNC_SECRET:-}" ]]; then
  echo "ERROR: set SYNC_SECRET (must match Cloud Run SYNC_SECRET)." >&2
  exit 1
fi

OIDC_ARGS=()
if [[ -n "${SCHEDULER_OIDC_SA:-}" ]]; then
  OIDC_ARGS+=(--oidc-service-account-email="${SCHEDULER_OIDC_SA}")
  OIDC_ARGS+=(--oidc-token-audience="https://${BACKEND_HOST}")
fi

if gcloud scheduler jobs describe esip-cloud-build-cost-sync \
  --project="${PROJECT}" --location="${LOCATION}" &>/dev/null; then
  echo "Job esip-cloud-build-cost-sync already exists in ${PROJECT}/${LOCATION}."
  exit 0
fi

gcloud scheduler jobs create http esip-cloud-build-cost-sync \
  --project="${PROJECT}" \
  --location="${LOCATION}" \
  --schedule="15 10 * * *" \
  --time-zone="UTC" \
  --uri="${URI}" \
  --http-method=POST \
  --headers="X-Sync-Secret=${SYNC_SECRET}" \
  --attempt-deadline=180s \
  --quiet \
  --format='value(name)' \
  "${OIDC_ARGS[@]}"

gcloud scheduler jobs pause esip-cloud-build-cost-sync \
  --project="${PROJECT}" \
  --location="${LOCATION}" \
  --quiet

echo "Created esip-cloud-build-cost-sync and left it PAUSED."
echo "When ready: gcloud scheduler jobs resume esip-cloud-build-cost-sync --project=${PROJECT} --location=${LOCATION}"
echo "Runbook for all schedulers: CLOUD_SCHEDULER.md (repo root)"
