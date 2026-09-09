#!/usr/bin/env bash
# A unique Compose project/run per invocation prevents cross-run collisions.
set -euo pipefail
cd "$(dirname "$0")/.."
export SENTINEL_RUN_ID="$(python3 -c 'import uuid; print(uuid.uuid4().hex)')"
export COMPOSE_PROJECT_NAME="sentinel-${SENTINEL_RUN_ID:0:12}"
workers="${WORKERS:-2}"
report_directory="reports/docker-${SENTINEL_RUN_ID}"
mkdir -p "$report_directory"
cleanup() { docker compose down --volumes --remove-orphans >/dev/null 2>&1 || true; }
trap cleanup EXIT
# Build once; --wait checks service health before submitting the run.
docker compose build
docker compose up -d --wait --scale "worker=$workers" redis demo-api worker
set +e
docker compose --profile run up --no-deps --exit-code-from coordinator coordinator
status=$?
set -e
docker compose cp coordinator:/app/reports/. "$report_directory/"
docker compose logs --no-color > "$report_directory/containers.log"
echo "Reports: $report_directory"
exit "$status"
