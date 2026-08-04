#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-esip-reference-project}"
REGION="${REGION:-us-central1}"
SERVICE_NAME="${SERVICE_NAME:-esip-backend}"
JOB_NAME="${JOB_NAME:-esip-zendesk-metadata-backfill}"
RUN_ID="${RUN_ID:-zendesk_metadata_backfill_20250115}"

TICKET_LIMIT="${TICKET_LIMIT:-21000}"
MAX_RUNTIME_SECONDS="${MAX_RUNTIME_SECONDS:-10500}"
CHUNK_SIZE="${CHUNK_SIZE:-25}"
WORKERS="${WORKERS:-2}"
SLEEP_SECONDS="${SLEEP_SECONDS:-0.1}"
AUDIT_MAX_PAGES="${AUDIT_MAX_PAGES:-3}"
TASK_TIMEOUT="${TASK_TIMEOUT:-10800}"
MEMORY="${MEMORY:-1Gi}"
CPU="${CPU:-1}"

ZENDESK_SUBDOMAIN="${ZENDESK_SUBDOMAIN:-example}"
ZENDESK_EMAIL="${ZENDESK_EMAIL:priya.shah@northstar.example.com}"
ZENDESK_API_TOKEN_SECRET="${ZENDESK_API_TOKEN_SECRET:-ESIP_ZENDESK_API_TOKEN:latest}"

GCLOUD="${GCLOUD:-/opt/homebrew/bin/gcloud}"

IMAGE="$("${GCLOUD}" run services describe "${SERVICE_NAME}" \
  --region="${REGION}" \
  --project="${PROJECT_ID}" \
  --format='value(spec.template.spec.containers[0].image)')"

SERVICE_ACCOUNT="$("${GCLOUD}" run services describe "${SERVICE_NAME}" \
  --region="${REGION}" \
  --project="${PROJECT_ID}" \
  --format='value(spec.template.spec.serviceAccountName)')"

if [[ -z "${IMAGE}" ]]; then
  echo "ERROR: could not resolve image from ${SERVICE_NAME}" >&2
  exit 1
fi

if [[ -z "${SERVICE_ACCOUNT}" ]]; then
  echo "ERROR: could not resolve service account from ${SERVICE_NAME}" >&2
  exit 1
fi

ARGS="^|^-m|app.jobs.zendesk_metadata_backfill|run-batch"
ARGS+="|--run-id|${RUN_ID}"
ARGS+="|--ticket-limit|${TICKET_LIMIT}"
ARGS+="|--max-runtime-seconds|${MAX_RUNTIME_SECONDS}"
ARGS+="|--chunk-size|${CHUNK_SIZE}"
ARGS+="|--workers|${WORKERS}"
ARGS+="|--sleep|${SLEEP_SECONDS}"
ARGS+="|--audit-max-pages|${AUDIT_MAX_PAGES}"
ARGS+="|--apply"

COMMON_FLAGS=(
  "--image=${IMAGE}"
  "--region=${REGION}"
  "--project=${PROJECT_ID}"
  "--service-account=${SERVICE_ACCOUNT}"
  "--tasks=1"
  "--parallelism=1"
  "--max-retries=0"
  "--task-timeout=${TASK_TIMEOUT}"
  "--memory=${MEMORY}"
  "--cpu=${CPU}"
  "--command=python"
  "--args=${ARGS}"
  "--set-env-vars=ZENDESK_SUBDOMAIN=${ZENDESK_SUBDOMAIN},ZENDESK_EMAIL=${ZENDESK_EMAIL},GOOGLE_CLOUD_PROJECT_ID=${PROJECT_ID}"
  "--set-secrets=ZENDESK_API_TOKEN=${ZENDESK_API_TOKEN_SECRET}"
)

if "${GCLOUD}" run jobs describe "${JOB_NAME}" --region="${REGION}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
  "${GCLOUD}" run jobs delete "${JOB_NAME}" --region="${REGION}" --project="${PROJECT_ID}" --quiet
fi
"${GCLOUD}" run jobs create "${JOB_NAME}" "${COMMON_FLAGS[@]}"

echo "Configured Cloud Run Job ${JOB_NAME} with image ${IMAGE}"
echo "Run id: ${RUN_ID}"
echo "Per execution cap: ${TICKET_LIMIT} tickets or ${MAX_RUNTIME_SECONDS}s"
