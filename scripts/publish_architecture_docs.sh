#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-esip-reference-project}"
ARCHITECTURE_BUCKET="${ARCHITECTURE_BUCKET:-esip-architecture}"
SHORT_SHA="$(git rev-parse --short HEAD)"
FULL_SHA="$(git rev-parse HEAD)"

gcloud builds submit . \
  --project="${PROJECT_ID}" \
  --config=cloudbuild.architecture-docs.yaml \
  --ignore-file=architecture-docs.gcloudignore \
  --substitutions="_ARCHITECTURE_BUCKET=${ARCHITECTURE_BUCKET},_SOURCE_COMMIT=${SHORT_SHA},_SOURCE_COMMIT_FULL=${FULL_SHA}"
