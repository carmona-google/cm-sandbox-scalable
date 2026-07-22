# cm-gke-as
# Codemender GKE Agent Sandbox

This repository provides a minimal, secure implementation for running the Google Cloud Codemender client (CMOC) in a highly isolated environment using Google Kubernetes Engine (GKE) Agent Sandbox.

## Architecture

The sandbox uses GKE Autopilot with the Agent Sandbox (gVisor) feature enabled. Network traffic is isolated using a private VPC and Private Google Access for Google APIs.

```mermaid
graph TD
    subgraph "Google Cloud Project"
        subgraph "VPC Network (cm-sandbox-vpc)"
            subgraph "Private Subnet (10.0.0.0/16)"
                GKE[GKE Autopilot Cluster\n(Agent Sandbox Enabled)]
                PVC[(PersistentVolumeClaim\n10Gi)]
                
                subgraph "Agent Sandbox Pool"
                    W[SandboxWarmPool]
                    Pod1[Codemender Sandbox Pod 1]
                    Pod2[Codemender Sandbox Pod n (Warm)]
                    W -.-> Pod2
                ]
                
                Pod1 --- PVC
                Pod1 -.->|GCS Fuse| FUSE[GCS Fuse Sidecar]
            end
        end
        
        GAR[(Artifact Registry)]
        GCS_SRC[(Source Code Bucket)]
        GCS_OUT[(Output Reports Bucket)]
        API[Codemender API]
    end
    
    GKE -->|Private Google Access| API
    GKE -.->|Pulls Image| GAR
    FUSE -->|Private Google Access| GCS_SRC
    FUSE -->|Private Google Access| GCS_OUT
```

## Prerequisites

1.  **Google Cloud SDK (`gcloud`)** installed and authenticated.
2.  **Terraform** (`>= 1.3.0`) installed.
3.  **Docker** installed (for building the Codemender image).
4.  **envsubst** installed (usually part of `gettext` package).
5.  A Google Cloud Project with billing enabled.

## Deployment Instructions

### 1. Set Environment Variables

```bash
export PROJECT_ID="your-google-cloud-project-id"
export LOCATION="us-central1" # Optional, defaults to us-central1
export PREFIX="cm-sandbox"    # Optional, defaults to cm-sandbox
```

### 2. Run the Deployment Script

The `deploy.sh` script automates the entire process:
1. Provision infrastructure via Terraform (VPC, Subnet, Artifact Registry, GKE Cluster, Workload Identity, enabling APIs, and GCS buckets).
2. Builds the Codemender Docker image (installing tools like C, C++, Java, Node.js, Python, Go) and pushes to Artifact Registry.
3. Deploys Kubernetes manifests (PVC, SandboxTemplate, SandboxWarmPool, SandboxClaim, RBAC) utilizing GCS Fuse to mount the created buckets.

```bash
./deploy.sh
```

## Using Codemender

### 1. Prepare Source Code
The Terraform deployment created two Cloud Storage buckets for you (with randomly generated suffixes). You can find their names in the Terraform output. 

Upload the source code you wish to analyze to the **source bucket**.

```bash
gcloud storage cp -r my-project/ gs://cm-sandbox-src-xxxxxx/
```

### 2. Connect to the Sandbox
Drop into an interactive shell inside the secure, isolated gVisor sandbox. The container environment has the source bucket mounted at `/workspace/src` and the output bucket mounted at `/workspace/out`.

```bash
./connect.sh
```

### 3. Run Codemender Commands
Inside the sandbox, you can run the Codemender client commands against the source code. The client will execute within the sandboxed context, compiling and verifying code.

```bash
# Find vulnerabilities in the source code
cm find /workspace/src

# Verify findings (can run exploits in the sandbox)
cm verify /workspace/src

# Attempt to automatically verify and fix findings
cm verify fix /workspace/src

# Generate reports
cm report
```

### 4. Retrieve Reports
Codemender outputs its HTML and MD reports to `~/.codemender/reports` by default. To retrieve them, copy them from the home directory into the mounted GCS output bucket.

```bash
cp -r ~/.codemender/reports/* /workspace/out/
```

You can then retrieve the reports from the Cloud Storage bucket to your local machine:
```bash
gcloud storage cp gs://cm-sandbox-out-xxxxxx/report.html .
```

## Clean Up

To avoid incurring charges, destroy the infrastructure when finished:

```bash
cd terraform
terraform destroy -var="project_id=${PROJECT_ID}"
```
