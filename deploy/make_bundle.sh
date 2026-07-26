#!/usr/bin/env bash
# Produce the clean source bundle (mnre-chatbot.zip) to hand to a customer.
#
# The zip contains the project at its ROOT (deploy.py, infra/, src/, ui/, data/,
# deploy/, docs, etc.) and EXCLUDES runtime state, secrets, virtualenvs, build
# artifacts, scratch scripts, and git metadata. The customer uploads this zip to
# an S3 bucket in their account and launches deploy/codebuild-deploy.yaml
# pointing at it (SourceBucket / SourceKey).
#
# Run from the repo root:  bash deploy/make_bundle.sh [output.zip]
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${1:-${ROOT}/mnre-chatbot.zip}"
STAGE="$(mktemp -d)/mnre-chatbot"

echo "[bundle] staging clean copy at ${STAGE}"
mkdir -p "${STAGE}"
rsync -a \
  --exclude '.git' \
  --exclude '.venv' \
  --exclude '.hypothesis' \
  --exclude '.pytest_cache' \
  --exclude '__pycache__' \
  --exclude '*.py[cod]' \
  --exclude '.DS_Store' \
  --exclude 'temp/' \
  --exclude 'infra/network_ids.json' \
  --exclude 'infra/_bootstrap_build/' \
  --exclude 'infra/_tool_build/' \
  --exclude 'infra/_loader_build/' \
  --exclude '*.zip' \
  --exclude 'deploy.config.json' \
  --exclude 'progress.md' \
  --exclude 'loader/prep_partial.py' \
  "${ROOT}/" "${STAGE}/"

# Guard: never ship account-specific state.
if grep -rqn "network_ids.json" "${STAGE}/infra" 2>/dev/null && [ -f "${STAGE}/infra/network_ids.json" ]; then
  echo "ERROR: network_ids.json leaked into the bundle" >&2
  exit 1
fi

echo "[bundle] zipping -> ${OUT}"
rm -f "${OUT}"
# Zip with the project at the archive ROOT (so CodeBuild unpacks deploy.py at top).
( cd "${STAGE}" && zip -rq "${OUT}" . )

echo "[bundle] done: ${OUT}"
echo
echo "Next steps for the customer:"
echo "  aws s3 cp ${OUT##*/} s3://<their-bucket>/mnre-chatbot.zip --region ap-south-1"
echo "  Launch deploy/codebuild-deploy.yaml with SourceBucket=<their-bucket> SourceKey=mnre-chatbot.zip"
