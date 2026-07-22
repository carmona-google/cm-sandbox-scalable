variable "project_id" {
  description = "The Google Cloud Project ID"
  type        = string
}

variable "region" {
  description = "The Google Cloud region to deploy resources to"
  type        = string
  default     = "us-central1"
}

variable "prefix" {
  description = "A prefix for naming resources"
  type        = string
  default     = "cm-sandbox"
}
