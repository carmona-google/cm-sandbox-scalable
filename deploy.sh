#!/usr/bin/env bash
set -e

if [ -z "$PROJECT_ID" ]; then
  echo "Error: PROJECT_ID environment variable is not set."
  exit 1
fi

export LOCATION=${LOCATION:-us-central1}
export PREFIX=${PREFIX:-cm-sandbox}
export WARMPOOL_REPLICAS=${WARMPOOL_REPLICAS:-3}

echo "======================================"
echo "1. Applying Terraform Configuration"
echo "======================================"
echo "Enabling base APIs required for Terraform..."
gcloud services enable cloudresourcemanager.googleapis.com serviceusage.googleapis.com --project=${PROJECT_ID}

cd terraform
set +e
terraform init
if [ $? -eq 0 ]; then
  echo "Terraform init succeeded!"
else
  echo "Terraform init failed!"
  exit 1
fi
set -e
terraform apply -auto-approve -var="project_id=${PROJECT_ID}" -var="region=${LOCATION}" -var="prefix=${PREFIX}"
cd ..

echo "======================================"
echo "2. Building and Pushing Docker Images"
echo "======================================"
./build_and_push.sh

echo "======================================"
echo "3. Fetching GKE Credentials"
echo "======================================"
CLUSTER_NAME="${PREFIX}-cluster"
gcloud container clusters get-credentials ${CLUSTER_NAME} --location=${LOCATION} --project=${PROJECT_ID}

# Extract Terraform outputs
export SRC_BUCKET=$(cd terraform && terraform output -raw src_bucket_name)
export OUT_BUCKET=$(cd terraform && terraform output -raw out_bucket_name)

echo "Source Bucket: $SRC_BUCKET"
echo "Output Bucket: $OUT_BUCKET"
echo "WarmPool Replicas: $WARMPOOL_REPLICAS"

echo "======================================"
echo "4. Deploying Kubernetes Manifests"
echo "======================================"

# Apply RBAC (worker and orchestrator ServiceAccounts + Roles)
cat k8s/rbac.yaml | envsubst '${PROJECT_ID}' | kubectl apply -f -

# Apply NetworkPolicy and ConfigMap
kubectl apply -f k8s/network-policy.yaml
kubectl apply -f k8s/configmap.yaml

# Remove legacy static claim and existing warmpool so pre-warmed pods
# are recreated with the updated SandboxTemplate and latest worker image
echo "Recycling legacy SandboxClaim and SandboxWarmPool (if present)..."
kubectl delete sandboxclaim codemender-claim --ignore-not-found
kubectl delete sandboxwarmpool codemender-warmpool --ignore-not-found

# Apply SandboxTemplate with local emptyDir staging and GCS FUSE caching
cat k8s/sandbox-template.yaml | envsubst '${LOCATION} ${PROJECT_ID} ${SRC_BUCKET} ${OUT_BUCKET}' | kubectl apply -f -

# Apply WarmPool with configurable replicas for concurrent execution
cat k8s/sandbox-warmpool.yaml | envsubst '${WARMPOOL_REPLICAS}' | kubectl apply -f -

# Deploy the Python/Flask Orchestrator API and restart pods to pull the latest image
cat k8s/orchestrator.yaml | envsubst '${LOCATION} ${PROJECT_ID} ${OUT_BUCKET}' | kubectl apply -f -
kubectl rollout restart deployment/codemender-orchestrator

echo "======================================"
echo "Deployment Complete!"
echo "Check warm pool and orchestrator status with:"
echo "  kubectl get sandboxwarmpool,sandbox,pod,svc"
echo "======================================"
