output "cluster_name" {
  description = "The name of the GKE Autopilot cluster"
  value       = google_container_cluster.autopilot_cluster.name
}

output "cluster_location" {
  description = "The location of the GKE Autopilot cluster"
  value       = google_container_cluster.autopilot_cluster.location
}

output "artifact_registry_repo" {
  description = "The Artifact Registry repository name"
  value       = google_artifact_registry_repository.repo.name
}

output "service_account_email" {
  description = "The Google Service Account email for Workload Identity"
  value       = google_service_account.codemender_sa.email
}

output "vpc_name" {
  description = "The VPC Network Name"
  value       = google_compute_network.vpc_network.name
}

output "src_bucket_name" {
  description = "The Source Code GCS Bucket name"
  value       = google_storage_bucket.src_bucket.name
}

output "out_bucket_name" {
  description = "The Output Reports GCS Bucket name"
  value       = google_storage_bucket.out_bucket.name
}
