#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-esip-reference-project}"
REGION="${REGION:-us-central1}"
LOCATION="${LOCATION:-us-central1}"
JOB_NAME="${JOB_NAME:-esip-service-desk-kpi-snapshot}"
SCHEDULER_NAME="${SCHEDULER_NAME:-esip-service-desk-kpi-snapshot-daily}"
# Daily KPI delivery default: one ordinary cron trigger at the configured morning send time.
SCHEDULE="${SCHEDULE:-30 10 * * *}"
TIME_ZONE="${TIME_ZONE:-UTC}"
SCHEDULER_SA="${SCHEDULER_SA:-dialogflow-api-reader@${PROJECT_ID}.iam.gserviceaccount.com}"
PAUSE_AFTER_CREATE="${PAUSE_AFTER_CREATE:-false}"

GCLOUD="${GCLOUD:-/opt/homebrew/bin/gcloud}"

URI="https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_ID}/jobs/${JOB_NAME}:run"

COMMON_FLAGS=(
  "--project=${PROJECT_ID}"
  "--location=${LOCATION}"
  "--schedule=${SCHEDULE}"
  "--time-zone=${TIME_ZONE}"
  "--uri=${URI}"
  "--http-method=POST"
  "--oauth-service-account-email=${SCHEDULER_SA}"
  "--oauth-token-scope=https://www.googleapis.com/auth/cloud-platform"
  "--attempt-deadline=320s"
)

if "${GCLOUD}" scheduler jobs describe "${SCHEDULER_NAME}" --project="${PROJECT_ID}" --location="${LOCATION}" >/dev/null 2>&1; then
  "${GCLOUD}" scheduler jobs update http "${SCHEDULER_NAME}" "${COMMON_FLAGS[@]}" --quiet --format='value(name)'
else
  "${GCLOUD}" scheduler jobs create http "${SCHEDULER_NAME}" "${COMMON_FLAGS[@]}" --quiet --format='value(name)'
fi

if [[ "${PAUSE_AFTER_CREATE}" == "true" ]]; then
  "${GCLOUD}" scheduler jobs pause "${SCHEDULER_NAME}" --project="${PROJECT_ID}" --location="${LOCATION}" --quiet
fi

echo "Configured Scheduler ${SCHEDULER_NAME} -> ${JOB_NAME}"
echo "Schedule: ${SCHEDULE} ${TIME_ZONE}"
echo "Daily KPI delivery runs as an ordinary cron trigger."
echo "Scheduler service account: ${SCHEDULER_SA}"
if [[ "${PAUSE_AFTER_CREATE}" == "true" ]]; then
  echo "State: paused after create/update"
fi
