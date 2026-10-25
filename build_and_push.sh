#!/usr/bin/env bash
set -e

if [ -z "$PROJECT_ID" ]; then
  echo "Error: PROJECT_ID environment variable is not set."
  exit 1
fi

LOCATION=${LOCATION:-us-central1}
PREFIX=${PREFIX:-cm-sandbox}
REPO_NAME="${PREFIX}-repo"
WORKER_IMAGE="${LOCATION}-docker.pkg.dev/${PROJECT_ID}/${REPO_NAME}/codemender-worker:latest"
ORCHESTRATOR_IMAGE="${LOCATION}-docker.pkg.dev/${PROJECT_ID}/${REPO_NAME}/codemender-orchestrator:latest"

echo "Authenticating Docker to Artifact Registry..."
gcloud auth configure-docker ${LOCATION}-docker.pkg.dev --quiet

echo "Building CodeMender Worker Docker image..."
docker build -t ${WORKER_IMAGE} .

echo "Pushing CodeMender Worker Docker image to Artifact Registry..."
docker push ${WORKER_IMAGE}

echo "Building Flask Orchestrator Docker image..."
docker build -f orchestrator/Dockerfile.orchestrator -t ${ORCHESTRATOR_IMAGE} .

echo "Pushing Flask Orchestrator Docker image to Artifact Registry..."
docker push ${ORCHESTRATOR_IMAGE}

echo "Successfully built and pushed:"
echo " - ${WORKER_IMAGE}"
echo " - ${ORCHESTRATOR_IMAGE}"
