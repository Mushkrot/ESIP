#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-esip-reference-project}"
REGION="${REGION:-us-central1}"
SERVICE_NAME="${SERVICE_NAME:-esip-backend}"
REPOSITORY="${REPOSITORY:-cloud-run-source-deploy}"
BACKEND_DIR="${BACKEND_DIR:-backend}"
DEPLOY_MODE="${DEPLOY_MODE:-deploy}" # deploy | build-only
CLOUD_RUN_TIMEOUT="${CLOUD_RUN_TIMEOUT:-1800}"

if [[ ! -f "${BACKEND_DIR}/requirements.txt" ]]; then
  echo "ERROR: ${BACKEND_DIR}/requirements.txt not found" >&2
  exit 1
fi

if [[ ! -f "${BACKEND_DIR}/cloudbuild.backend-base.yaml" || ! -f "${BACKEND_DIR}/cloudbuild.backend-runtime.yaml" ]]; then
  echo "ERROR: Cloud Build config files not found in ${BACKEND_DIR}" >&2
  exit 1
fi

REQ_HASH="$(shasum -a 256 "${BACKEND_DIR}/requirements.txt" | awk '{print substr($1,1,12)}')"
TIMESTAMP="$(date +%Y%m%d-%H%M%S)"

BASE_IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPOSITORY}/esip-backend-base:py312-${REQ_HASH}"
APP_IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPOSITORY}/esip-backend:app-${TIMESTAMP}"
BASE_PACKAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPOSITORY}/esip-backend-base"

echo "== ESIP backend deploy (dependency-base mode) =="
echo "Project:      ${PROJECT_ID}"
echo "Region:       ${REGION}"
echo "Service:      ${SERVICE_NAME}"
echo "Mode:         ${DEPLOY_MODE}"
echo "Timeout:      ${CLOUD_RUN_TIMEOUT}s"
echo "Req hash:     ${REQ_HASH}"
echo "Base image:   ${BASE_IMAGE}"
echo "App image:    ${APP_IMAGE}"

start_total="$(date +%s)"

existing_base="$(gcloud artifacts docker images list "${BASE_PACKAGE}" \
  --project="${PROJECT_ID}" \
  --include-tags \
  --filter="tags:py312-${REQ_HASH}" \
  --format='value(version)' \
  --limit=1 2>/dev/null || true)"

if [[ -n "${existing_base}" ]]; then
  echo "[1/4] Base image already exists for this requirements hash. Reusing."
else
  echo "[1/4] Building dependency base image (first run for this requirements hash)..."
  gcloud builds submit "${BACKEND_DIR}" \
    --project="${PROJECT_ID}" \
    --region="${REGION}" \
    --config="${BACKEND_DIR}/cloudbuild.backend-base.yaml" \
    --substitutions="_BASE_IMAGE=${BASE_IMAGE}"
fi

echo "[2/4] Building runtime app image..."
gcloud builds submit "${BACKEND_DIR}" \
  --project="${PROJECT_ID}" \
  --region="${REGION}" \
  --config="${BACKEND_DIR}/cloudbuild.backend-runtime.yaml" \
  --substitutions="_BASE_IMAGE=${BASE_IMAGE},_APP_IMAGE=${APP_IMAGE}"

if [[ "${DEPLOY_MODE}" == "build-only" ]]; then
  elapsed_total="$(( $(date +%s) - start_total ))"
  echo "[3/4] DEPLOY_MODE=build-only -> skipping Cloud Run deploy."
  echo "Done. Total elapsed: ${elapsed_total}s"
  echo "Built app image: ${APP_IMAGE}"
  exit 0
fi

echo "[3/4] Deploying Cloud Run revision from prebuilt image..."
gcloud run deploy "${SERVICE_NAME}" \
  --image="${APP_IMAGE}" \
  --region="${REGION}" \
  --project="${PROJECT_ID}" \
  --timeout="${CLOUD_RUN_TIMEOUT}" \
  --max-instances=1

SERVICE_URL="$(gcloud run services describe "${SERVICE_NAME}" \
  --region="${REGION}" \
  --project="${PROJECT_ID}" \
  --format='value(status.url)')"

if [[ -z "${SERVICE_URL}" ]]; then
  echo "ERROR: Could not resolve service URL after deploy" >&2
  exit 1
fi

echo "[4/4] Health check: ${SERVICE_URL}/health"
curl -fsS "${SERVICE_URL}/health"
echo

elapsed_total="$(( $(date +%s) - start_total ))"
echo "Done. Total elapsed: ${elapsed_total}s"
