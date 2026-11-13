# The lab project's spend fences, as definitions the owner applies from their own shell; a session
# only validates them (`make fences-validate`) and never plans or applies. No backend: the owner
# chooses where state lives when applying.
terraform {
  required_version = "~> 1.11"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "8.10.0"
    }
    google-beta = {
      source  = "hashicorp/google-beta"
      version = "8.10.0"
    }
  }
}

provider "google" {
  project = var.project
  region  = var.region
}

provider "google-beta" {
  project = var.project
  region  = var.region
}

# Billing-account APIs (Budgets) charge their quota to the caller's default project, where the API
# may be off: this alias bills them to the lab project. Only they use it: Eventarc forwards the
# override to Pub/Sub as its own service agent, which may not use the project's quota (403).
provider "google" {
  alias                 = "billing"
  project               = var.project
  region                = var.region
  billing_project       = var.project
  user_project_override = true
}
