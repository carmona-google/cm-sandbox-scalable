terraform {
  required_version = ">= 1.3.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
    google-beta = {
      source  = "hashicorp/google-beta"
      version = "~> 5.0"
    }
    time = {
      source  = "hashicorp/time"
      version = "~> 0.11"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

provider "google-beta" {
  project = var.project_id
  region  = var.region
}

data "google_project" "project" {}

# ---------------------------------------------------------
# Enable necessary APIs
# ---------------------------------------------------------
resource "google_project_service" "compute" {
  service            = "compute.googleapis.com"
  disable_on_destroy = false
}

resource "google_project_service" "container" {
  service            = "container.googleapis.com"
  disable_on_destroy = false
}

resource "google_project_service" "artifactregistry" {
  service            = "artifactregistry.googleapis.com"
  disable_on_destroy = false
}

resource "google_project_service" "servicedirectory" {
  service            = "servicedirectory.googleapis.com"
  disable_on_destroy = false
}

resource "google_project_service" "dns" {
  service            = "dns.googleapis.com"
  disable_on_destroy = false
}

resource "google_project_service" "storage" {
  service            = "storage.googleapis.com"
  disable_on_destroy = false
}

resource "google_project_service" "iam" {
  service            = "iam.googleapis.com"
  disable_on_destroy = false
}

# ---------------------------------------------------------
# Random suffix for globally unique bucket names
# ---------------------------------------------------------
#resource "random_string" "bucket_suffix" {
#  length  = 6
#  special = false
#  upper   = false
#}
# ---------------------------------------------------------
# Timestamp suffix for globally unique names
# ---------------------------------------------------------
resource "time_static" "bucket_suffix" {}

# ---------------------------------------------------------
# Random suffix for unique psc  names
# ---------------------------------------------------------
resource "random_string" "psc_suffix" {
  length  = 6
  special = false
  upper   = false
  numeric = true
}

# ---------------------------------------------------------
# VPC Network and Subnets
# ---------------------------------------------------------
resource "google_compute_network" "vpc_network" {
  name                    = "${var.prefix}-vpc"
  auto_create_subnetworks = false
  depends_on              = [google_project_service.compute]
}

resource "google_compute_subnetwork" "subnet" {
  name                     = "${var.prefix}-subnet"
  ip_cidr_range            = "10.0.0.0/16"
  region                   = var.region
  network                  = google_compute_network.vpc_network.id
  private_ip_google_access = true

  secondary_ip_range {
    range_name    = "${var.prefix}-pods"
    ip_cidr_range = "10.1.0.0/16"
  }

  secondary_ip_range {
    range_name    = "${var.prefix}-services"
    ip_cidr_range = "10.2.0.0/20"
  }
}


# ---------------------------------------------------------
# Artifact Registry
# ---------------------------------------------------------
resource "google_artifact_registry_repository" "repo" {
  location      = var.region
  repository_id = "${var.prefix}-repo"
  description   = "Docker repository for Codemender images"
  format        = "DOCKER"
  depends_on    = [google_project_service.artifactregistry]
}

resource "google_artifact_registry_repository_iam_member" "repo_reader" {
  project    = google_artifact_registry_repository.repo.project
  location   = google_artifact_registry_repository.repo.location
  repository = google_artifact_registry_repository.repo.name
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:${data.google_project.project.number}-compute@developer.gserviceaccount.com"
}

# ---------------------------------------------------------
# GKE Autopilot Cluster
# ---------------------------------------------------------
resource "google_container_cluster" "autopilot_cluster" {
  provider = google-beta
  name     = "${var.prefix}-cluster"
  location = var.region

  network    = google_compute_network.vpc_network.id
  subnetwork = google_compute_subnetwork.subnet.id

  # Enable Autopilot
  enable_autopilot = true

  # Enable Agent Sandbox
  # The google-beta provider is needed for this feature block currently
  # Note: The terraform provider attribute for agent sandbox might not explicitly exist as a high-level block
  # but setting it via the corresponding addons config or letting Autopilot default/beta flags handle it.
  # However, GKE Autopilot supports --enable-agent-sandbox at creation. 
  # We can configure it in terraform if supported or it is enabled via CLI. 
  # Currently, the terraform provider might need to be explicitly configured.
  # According to docs, Agent sandbox is an addon or cluster setting.
  # If it's not yet in the TF provider, we might have to use local-exec or beta provider attributes.
  # Wait, docs say: "gcloud beta container clusters create-auto ... --enable-agent-sandbox"
  
  # For Terraform, let's try the settings. If not natively supported, we'll use a local-exec provisioner to update it.
  # We will just deploy a standard Autopilot cluster and use a local-exec script to ensure Agent Sandbox is enabled,
  # or rely on the beta provider's secret/advanced fields if they exist.
  
  ip_allocation_policy {
    cluster_secondary_range_name  = google_compute_subnetwork.subnet.secondary_ip_range[0].range_name
    services_secondary_range_name = google_compute_subnetwork.subnet.secondary_ip_range[1].range_name
  }

  private_cluster_config {
    enable_private_nodes    = true
    enable_private_endpoint = false
    master_ipv4_cidr_block  = "172.16.0.0/28"
  }

  # Ensure Workload Identity is enabled (Autopilot does this by default, but explicit is good)
  workload_identity_config {
    workload_pool = "${var.project_id}.svc.id.goog"
  }

  depends_on = [google_project_service.container]
}

# Null resource to enable Agent Sandbox if the provider doesn't support it directly yet.
# Since the docs state it requires gcloud beta and the provider might lag.
resource "null_resource" "enable_agent_sandbox" {
  depends_on = [google_container_cluster.autopilot_cluster]

  provisioner "local-exec" {
    command = <<EOF
      gcloud beta container clusters update ${google_container_cluster.autopilot_cluster.name} \
        --location=${var.region} \
        --enable-agent-sandbox \
        --project=${var.project_id}
    EOF
  }
}

# ---------------------------------------------------------
# Workload Identity IAM
# ---------------------------------------------------------
# Service Account for the GKE Pod
resource "google_service_account" "codemender_sa" {
  account_id   = "${var.prefix}-sa"
  display_name = "Codemender Service Account"
  depends_on   = [google_project_service.iam]
}

# Allow K8s Service Account to impersonate Google Service Account
resource "google_service_account_iam_member" "workload_identity_binding" {
  service_account_id = google_service_account.codemender_sa.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${var.project_id}.svc.id.goog[default/codemender-sa]"
  depends_on         = [google_container_cluster.autopilot_cluster]
}

# ---------------------------------------------------------
# GCS Buckets for Codemender (Source and Output)
# ---------------------------------------------------------
resource "google_storage_bucket" "src_bucket" {
  name                        = "${var.prefix}-src-${formatdate("YYYYMMDDhhmmss", time_static.bucket_suffix.rfc3339)}"
  location                    = var.region
  force_destroy               = true
  uniform_bucket_level_access = true
  depends_on                  = [google_project_service.storage]
}

resource "google_storage_bucket" "out_bucket" {
  name                        = "${var.prefix}-out-${formatdate("YYYYMMDDhhmmss", time_static.bucket_suffix.rfc3339)}"
  location                    = var.region
  force_destroy               = true
  uniform_bucket_level_access = true
  depends_on                  = [google_project_service.storage]
}

# Grant the Codemender Service Account access to the source bucket
resource "google_storage_bucket_iam_member" "src_bucket_access" {
  bucket = google_storage_bucket.src_bucket.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.codemender_sa.email}"
}

# Grant the Codemender Service Account access to the output bucket
resource "google_storage_bucket_iam_member" "out_bucket_access" {
  bucket = google_storage_bucket.out_bucket.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.codemender_sa.email}"
}
