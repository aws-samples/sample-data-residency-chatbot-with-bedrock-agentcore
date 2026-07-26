#!/usr/bin/env bash
# Container-tool runner for deploy_agent.py (login / build / tag / push).
#
# WHY this script exists: keeps the Python side's subprocess argv fully static
# (["bash", <this script>, "<literal subcommand>"]) — no dynamic values ever
# reach a Python-built command line. All dynamic inputs arrive as environment
# variables, validated here:
#   CONTAINER_TOOL  finch | docker  (anything else is rejected)
#   ECR_REGISTRY    <account>.dkr.ecr.<region>.amazonaws.com   (login)
#   DOCKERFILE      absolute path to agent.Dockerfile           (build)
#   BUILD_CONTEXT   absolute path to the repo root              (build)
#   LOCAL_TAG       local image tag                             (build, push)
#   REMOTE_TAG      ECR image tag                               (push)
# The ECR password is read from STDIN for "login" (never in argv/env).
set -euo pipefail

TOOL="${CONTAINER_TOOL:-finch}"
case "$TOOL" in
  finch|docker) ;;
  *) echo "unsupported CONTAINER_TOOL '$TOOL' (finch|docker)" >&2; exit 2 ;;
esac
command -v "$TOOL" >/dev/null 2>&1 || { echo "'$TOOL' not found on PATH" >&2; exit 3; }

case "${1:-}" in
  login)
    # Password arrives on stdin (from ecr get-authorization-token).
    exec "$TOOL" login --username AWS --password-stdin "$ECR_REGISTRY"
    ;;
  build)
    exec "$TOOL" build -f "$DOCKERFILE" -t "$LOCAL_TAG" --platform linux/amd64 "$BUILD_CONTEXT"
    ;;
  push)
    "$TOOL" tag "$LOCAL_TAG" "$REMOTE_TAG"
    exec "$TOOL" push "$REMOTE_TAG"
    ;;
  *)
    echo "usage: container_tool.sh {login|build|push}" >&2
    exit 2
    ;;
esac
