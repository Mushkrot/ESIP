#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-esip-reference-project}"
REGION="${REGION:-us-central1}"
SERVICE_NAME="${SERVICE_NAME:-esip-backend}"
JOB_NAME="${JOB_NAME:-esip-service-desk-kpi-snapshot}"
REPORT_BUCKET="${REPORT_BUCKET:-esip-reference-data}"
REPORT_PREFIX="${REPORT_PREFIX:-reference_kpi_reports}"
REPORT_DATE="${REPORT_DATE:-}"
OVERWRITE="${OVERWRITE:-false}"
JOB_MODE="${JOB_MODE:-snapshot}"
FIREBASE_PROJECT_ID="${FIREBASE_PROJECT_ID:-esip-reference-project}"
ALLOWED_EMAIL_DOMAINS="${ALLOWED_EMAIL_DOMAINS:-example.com,example.com}"
CXONE_ACCESS_KEY_ID="${CXONE_ACCESS_KEY_ID:-}"
CXONE_ACCESS_KEY_SECRET="${CXONE_ACCESS_KEY_SECRET:-}"
CXONE_REGION="${CXONE_REGION:-na1}"
CXONE_TENANT_ID="${CXONE_TENANT_ID:-}"
CXONE_USER_ID="${CXONE_USER_ID:-}"
CXONE_ACCOUNT_ID="${CXONE_ACCOUNT_ID:-}"

TASK_TIMEOUT="${TASK_TIMEOUT:-3600}"
MEMORY="${MEMORY:-2Gi}"
CPU="${CPU:-1}"

ZENDESK_SUBDOMAIN="${ZENDESK_SUBDOMAIN:-example}"
ZENDESK_EMAIL="${ZENDESK_EMAIL:priya.shah@northstar.example.com}"
ZENDESK_API_TOKEN_SECRET="${ZENDESK_API_TOKEN_SECRET:-ESIP_ZENDESK_API_TOKEN:latest}"
SA_CREDENTIALS_SECRET="${SA_CREDENTIALS_SECRET:-ESIP_SA_CREDENTIALS:latest}"
GOOGLE_APPLICATION_CREDENTIALS_PATH="/secrets/sa_key.json"
EMAIL_TRANSPORT="${EMAIL_TRANSPORT:-}"
EMAIL_SENDGRID_FALLBACK_ENABLED="${EMAIL_SENDGRID_FALLBACK_ENABLED:-}"
GMAIL_DELEGATED_SUBJECT="${GMAIL_DELEGATED_SUBJECT:-}"
GMAIL_SENDER_EMAIL="${GMAIL_SENDER_EMAIL:-}"
GMAIL_SENDER_NAME="${GMAIL_SENDER_NAME:-}"
ALERT_FROM_EMAIL="${ALERT_FROM_EMAIL:-}"

GCLOUD="${GCLOUD:-/opt/homebrew/bin/gcloud}"

IMAGE="$("${GCLOUD}" run services describe "${SERVICE_NAME}" \
  --region="${REGION}" \
  --project="${PROJECT_ID}" \
  --format='value(spec.template.spec.containers[0].image)')"

SOURCE_REVISION="${SOURCE_REVISION:-$("${GCLOUD}" run services describe "${SERVICE_NAME}" \
  --region="${REGION}" \
  --project="${PROJECT_ID}" \
  --format='value(status.latestReadyRevisionName)')}"

SERVICE_ACCOUNT="$("${GCLOUD}" run services describe "${SERVICE_NAME}" \
  --region="${REGION}" \
  --project="${PROJECT_ID}" \
  --format='value(spec.template.spec.serviceAccountName)')"

SERVICE_JSON="$("${GCLOUD}" run services describe "${SERVICE_NAME}" \
  --region="${REGION}" \
  --project="${PROJECT_ID}" \
  --format=json)"

service_env_value() {
  local env_name="$1"
  SERVICE_JSON="${SERVICE_JSON}" ENV_NAME="${env_name}" /usr/bin/python3 -c '
import json
import os

data = json.loads(os.environ["SERVICE_JSON"])
target = os.environ["ENV_NAME"]
containers = data.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
for env in (containers[0].get("env", []) if containers else []):
    if env.get("name") == target:
        print(env.get("value", ""), end="")
        break
'
}

if [[ -z "${IMAGE}" ]]; then
  echo "ERROR: could not resolve image from ${SERVICE_NAME}" >&2
  exit 1
fi

if [[ -z "${SERVICE_ACCOUNT}" ]]; then
  echo "ERROR: could not resolve service account from ${SERVICE_NAME}" >&2
  exit 1
fi

SOURCE_REVISION="${SOURCE_REVISION:-${IMAGE}}"

FIREBASE_PROJECT_ID="${FIREBASE_PROJECT_ID:-$(service_env_value FIREBASE_PROJECT_ID)}"
ALLOWED_EMAIL_DOMAINS="${ALLOWED_EMAIL_DOMAINS:-$(service_env_value ALLOWED_EMAIL_DOMAINS)}"
CXONE_ACCESS_KEY_ID="${CXONE_ACCESS_KEY_ID:-$(service_env_value CXONE_ACCESS_KEY_ID)}"
CXONE_ACCESS_KEY_SECRET="${CXONE_ACCESS_KEY_SECRET:-$(service_env_value CXONE_ACCESS_KEY_SECRET)}"
CXONE_REGION="${CXONE_REGION:-$(service_env_value CXONE_REGION)}"
CXONE_TENANT_ID="${CXONE_TENANT_ID:-$(service_env_value CXONE_TENANT_ID)}"
CXONE_USER_ID="${CXONE_USER_ID:-$(service_env_value CXONE_USER_ID)}"
CXONE_ACCOUNT_ID="${CXONE_ACCOUNT_ID:-$(service_env_value CXONE_ACCOUNT_ID)}"
EMAIL_TRANSPORT="${EMAIL_TRANSPORT:-$(service_env_value EMAIL_TRANSPORT)}"
EMAIL_SENDGRID_FALLBACK_ENABLED="${EMAIL_SENDGRID_FALLBACK_ENABLED:-$(service_env_value EMAIL_SENDGRID_FALLBACK_ENABLED)}"
GMAIL_DELEGATED_SUBJECT="${GMAIL_DELEGATED_SUBJECT:-$(service_env_value GMAIL_DELEGATED_SUBJECT)}"
GMAIL_SENDER_EMAIL="${GMAIL_SENDER_EMAIL:-$(service_env_value GMAIL_SENDER_EMAIL)}"
GMAIL_SENDER_NAME="${GMAIL_SENDER_NAME:-$(service_env_value GMAIL_SENDER_NAME)}"
ALERT_FROM_EMAIL="${ALERT_FROM_EMAIL:-$(service_env_value ALERT_FROM_EMAIL)}"

FIREBASE_PROJECT_ID="${FIREBASE_PROJECT_ID:-${PROJECT_ID}}"
ALLOWED_EMAIL_DOMAINS="${ALLOWED_EMAIL_DOMAINS:-example.com,example.com}"
CXONE_REGION="${CXONE_REGION:-na1}"
EMAIL_TRANSPORT="${EMAIL_TRANSPORT:-gmail_api}"
EMAIL_SENDGRID_FALLBACK_ENABLED="${EMAIL_SENDGRID_FALLBACK_ENABLED:-false}"
GMAIL_DELEGATED_SUBJECT="${GMAIL_DELEGATED_SUBJECT:morgan.chen@northstar.example.com}"
GMAIL_SENDER_EMAIL="${GMAIL_SENDER_EMAIL:morgan.chen@northstar.example.com}"
GMAIL_SENDER_NAME="${GMAIL_SENDER_NAME:-ESIP Alerts}"
ALERT_FROM_EMAIL="${ALERT_FROM_EMAIL:morgan.chen@northstar.example.com}"

if [[ -z "${CXONE_ACCESS_KEY_ID}" || -z "${CXONE_ACCESS_KEY_SECRET}" ]]; then
  echo "WARNING: CXONE_ACCESS_KEY_ID / CXONE_ACCESS_KEY_SECRET are blank." >&2
  echo "The job will still be created, but the morning snapshot will not fully match the manual report path for phone data." >&2
fi

case "${JOB_MODE}" in
  snapshot|run)
    JOB_COMMAND="run"
    ;;
  delivery|deliver-due|dispatcher)
    JOB_COMMAND="deliver-due"
    ;;
  *)
    echo "ERROR: JOB_MODE must be snapshot or delivery, got ${JOB_MODE}" >&2
    exit 1
    ;;
esac

ARGS="^|^-m|app.jobs.service_desk_kpi_snapshot|${JOB_COMMAND}"
if [[ -n "${REPORT_DATE}" ]]; then
  ARGS+="|--date|${REPORT_DATE}"
fi
if [[ "${JOB_COMMAND}" == "run" && "${OVERWRITE}" == "true" ]]; then
  ARGS+="|--overwrite"
fi

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
  "--set-env-vars=^|^ZENDESK_SUBDOMAIN=${ZENDESK_SUBDOMAIN}|ZENDESK_EMAIL=${ZENDESK_EMAIL}|GOOGLE_CLOUD_PROJECT_ID=${PROJECT_ID}|FIREBASE_PROJECT_ID=${FIREBASE_PROJECT_ID}|ALLOWED_EMAIL_DOMAINS=${ALLOWED_EMAIL_DOMAINS}|SERVICE_DESK_KPI_REPORTS_BUCKET=${REPORT_BUCKET}|SERVICE_DESK_KPI_REPORTS_PREFIX=${REPORT_PREFIX}|SOURCE_REVISION=${SOURCE_REVISION}|GOOGLE_APPLICATION_CREDENTIALS=/secrets/sa_key.json|EMAIL_TRANSPORT=${EMAIL_TRANSPORT}|EMAIL_SENDGRID_FALLBACK_ENABLED=${EMAIL_SENDGRID_FALLBACK_ENABLED}|GMAIL_DELEGATED_SUBJECT=${GMAIL_DELEGATED_SUBJECT}|GMAIL_SENDER_EMAIL=${GMAIL_SENDER_EMAIL}|GMAIL_SENDER_NAME=${GMAIL_SENDER_NAME}|ALERT_FROM_EMAIL=${ALERT_FROM_EMAIL}|CXONE_ACCESS_KEY_ID=${CXONE_ACCESS_KEY_ID}|CXONE_ACCESS_KEY_SECRET=${CXONE_ACCESS_KEY_SECRET}|CXONE_REGION=${CXONE_REGION}|CXONE_TENANT_ID=${CXONE_TENANT_ID}|CXONE_USER_ID=${CXONE_USER_ID}|CXONE_ACCOUNT_ID=${CXONE_ACCOUNT_ID}"
  "--set-secrets=^|^ZENDESK_API_TOKEN=${ZENDESK_API_TOKEN_SECRET}|/secrets/sa_key.json=${SA_CREDENTIALS_SECRET}"
)

if "${GCLOUD}" run jobs describe "${JOB_NAME}" --region="${REGION}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
  "${GCLOUD}" run jobs delete "${JOB_NAME}" --region="${REGION}" --project="${PROJECT_ID}" --quiet
fi
"${GCLOUD}" run jobs create "${JOB_NAME}" "${COMMON_FLAGS[@]}"

echo "Configured Cloud Run Job ${JOB_NAME} with image ${IMAGE}"
echo "Source revision metadata: ${SOURCE_REVISION}"
echo "Job command: ${JOB_COMMAND}"
echo "Gmail credential mount path: ${GOOGLE_APPLICATION_CREDENTIALS_PATH}"
echo "Destination: gs://${REPORT_BUCKET}/${REPORT_PREFIX}/YYYY-MM/service-desk-kpi-YYYY-MM-DD.xlsx"
if [[ -n "${REPORT_DATE}" ]]; then
  echo "Pinned report date: ${REPORT_DATE}"
fi
