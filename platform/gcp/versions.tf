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
