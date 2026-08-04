#!/usr/bin/env bash
set -e

if [ -z "$PROJECT_ID" ]; then
  echo "Error: PROJECT_ID environment variable is not set."
  exit 1
fi

export LOCATION=${LOCATION:-us-central1}
export PREFIX=${PREFIX:-cm-sandbox}

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
echo "2. Building and Pushing Docker Image"
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

echo "======================================"
echo "4. Deploying Kubernetes Manifests"
echo "======================================"

# Apply substituting env vars for the image repository and GCS buckets
# Note: we use envsubst to inject LOCATION, PROJECT_ID, SRC_BUCKET, and OUT_BUCKET into sandbox-template.yaml
cat k8s/sandbox-template.yaml | envsubst '${LOCATION} ${PROJECT_ID} ${SRC_BUCKET} ${OUT_BUCKET}' | kubectl apply -f -

# Also inject PROJECT_ID into rbac.yaml
cat k8s/rbac.yaml | envsubst '${PROJECT_ID}' | kubectl apply -f -

kubectl apply -f k8s/network-policy.yaml
kubectl apply -f k8s/configmap.yaml
kubectl apply -f k8s/pvc.yaml
kubectl apply -f k8s/sandbox-warmpool.yaml

echo "Waiting for Warm Pool to create the sandbox..."
sleep 10
kubectl apply -f k8s/sandbox-claim.yaml

echo "======================================"
echo "Deployment Complete!"
echo "Check sandbox status with: kubectl get sandbox,pod"
echo "======================================"
