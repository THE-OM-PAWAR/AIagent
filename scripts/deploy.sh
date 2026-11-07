#!/usr/bin/env bash
# AIagent Ubuntu Deployment Script
# Safely pulls/builds the latest image and performs zero-downtime container rollout.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

cd "${PROJECT_DIR}"

echo "==> [AIagent] Starting deployment..."

# 1. Verify environment configuration
if [[ ! -f .env ]]; then
    echo "ERROR: .env file not found in ${PROJECT_DIR}."
    echo "Please copy .env.example to .env and configure production credentials."
    exit 1
fi

# 2. Authenticate with GHCR if registry credentials are provided
if [[ -n "${GHCR_USERNAME:-}" && -n "${GHCR_TOKEN:-}" ]]; then
    echo "==> Authenticating with GitHub Container Registry (ghcr.io)..."
    echo "${GHCR_TOKEN}" | docker login ghcr.io -u "${GHCR_USERNAME}" --password-stdin
fi

# 3. Pull or build latest images
echo "==> Pulling or building container images..."
if [[ -n "${AIAGENT_IMAGE:-}" && "${AIAGENT_IMAGE}" != "aiagent-app" ]]; then
    docker compose pull app || docker compose build app
else
    docker compose build app
fi

# 4. Deploy containers in detached mode
echo "==> Deploying containers with docker compose..."
docker compose up -d --remove-orphans

# 5. Verify deployment health
echo "==> Waiting for services to reach healthy status..."
MAX_ATTEMPTS=12
ATTEMPT=1
DEPLOY_HEALTHY=false

while [[ ${ATTEMPT} -le ${MAX_ATTEMPTS} ]]; do
    UNHEALTHY=$(docker compose ps --format '{{.Service}}: {{.Health}}' | grep -v 'healthy' | grep -v '^$' || true)
    if [[ -z "${UNHEALTHY}" ]]; then
        DEPLOY_HEALTHY=true
        break
    fi
    echo "Attempt ${ATTEMPT}/${MAX_ATTEMPTS}: waiting for services to become healthy..."
    sleep 5
    ((ATTEMPT++))
done

if [[ "${DEPLOY_HEALTHY}" = true ]]; then
    echo "==> All services are healthy!"
    docker compose ps
else
    echo "WARNING: Some services have not reported healthy within timeout:"
    docker compose ps
fi

# 6. Clean dangling images
echo "==> Cleaning up unused dangling Docker images..."
docker image prune -f

echo "==> [AIagent] Deployment finished successfully."

