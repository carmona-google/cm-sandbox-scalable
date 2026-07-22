#!/usr/bin/env bash
set -e

if [ -z "$PROJECT_ID" ]; then
  echo "Error: PROJECT_ID environment variable is not set."
  exit 1
fi

LOCATION=${LOCATION:-us-central1}
PREFIX=${PREFIX:-cm-sandbox}
REPO_NAME="${PREFIX}-repo"
IMAGE_NAME="${LOCATION}-docker.pkg.dev/${PROJECT_ID}/${REPO_NAME}/codemender-worker:latest"

echo "Authenticating Docker to Artifact Registry..."
gcloud auth configure-docker ${LOCATION}-docker.pkg.dev --quiet

echo "Building Docker image..."
docker build -t ${IMAGE_NAME} .

echo "Pushing Docker image to Artifact Registry..."
docker push ${IMAGE_NAME}

echo "Successfully built and pushed ${IMAGE_NAME}"
