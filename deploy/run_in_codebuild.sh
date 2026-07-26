#!/usr/bin/env bash
# Entry point executed inside the CodeBuild container by the one-click
# CloudFormation launcher (deploy/codebuild-deploy.yaml).
#
# CodeBuild provides Docker, git, and Python, so it can do the one thing
# CloudShell / a pure CFN template cannot: build + push the Agent_Lambda
# container image. The project source (the mnre-chatbot.zip the customer
# uploaded to their S3 bucket) is already downloaded and UNPACKED at the build
# root by CodeBuild, so this script just runs deploy.py / cleanup.py from there.
#
# Environment (set by the CFN project):
#   ACTION          deploy | cleanup           (default: deploy)
#   WITH_WEBSOCKET  true | false               (default: false)
#   TARGET_ACCOUNT  12-digit account id        (the account CodeBuild runs in)
#   AWS_DEFAULT_REGION is forced to ap-south-1 by the project.
set -euo pipefail

ACTION="${ACTION:-deploy}"
WITH_WEBSOCKET="${WITH_WEBSOCKET:-false}"
export CONTAINER_TOOL=docker          # CodeBuild has Docker, not finch

echo "=== MNRE chatbot CodeBuild runner ==="
echo "action=${ACTION} ws=${WITH_WEBSOCKET}"
echo "account=${TARGET_ACCOUNT} region=${AWS_DEFAULT_REGION:-ap-south-1}"

# --- tooling -----------------------------------------------------------------
# uv (fast Python package manager) — install if the image doesn't have it.
if ! command -v uv >/dev/null 2>&1; then
  echo "[setup] installing uv"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="${HOME}/.local/bin:${HOME}/.cargo/bin:${PATH}"
fi
uv --version

# The unpacked bundle root is the current working directory (CODEBUILD_SRC_DIR).
# Sanity-check we are in the project root.
if [ ! -f "deploy.py" ]; then
  echo "ERROR: deploy.py not found in $(pwd). Is the source bundle laid out with"
  echo "the project at its root? (zip contents should include deploy.py at top level)"
  ls -la
  exit 1
fi

uv sync

# --- run ---------------------------------------------------------------------
if [ "${ACTION}" = "cleanup" ]; then
  echo "[run] cleanup.py"
  uv run python cleanup.py --account "${TARGET_ACCOUNT}" --yes --delete-bucket
else
  WS_FLAG=""
  if [ "${WITH_WEBSOCKET}" = "true" ]; then WS_FLAG="--with-websocket"; fi
  echo "[run] deploy.py ${WS_FLAG}"
  uv run python deploy.py --account "${TARGET_ACCOUNT}" ${WS_FLAG}

  # Surface the key outputs at the end of the build log.
  echo "=== deployment outputs ==="
  uv run python - <<'PY'
import json, os
p = os.path.join("infra", "network_ids.json")
ids = json.load(open(p)) if os.path.isfile(p) else {}
print("amplify_url :", ids.get("amplify_url", "(n/a)"))
print("rest_api_url:", ids.get("rest_api_url", "(n/a)"))
PY
fi

echo "=== done ==="
