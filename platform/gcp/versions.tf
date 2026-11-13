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

# Bill every API call to the lab project: with a person's own login, APIs such as Budgets otherwise
# charge their quota to whatever project the login defaults to, where the API may be off.
provider "google" {
  project               = var.project
  region                = var.region
  billing_project       = var.project
  user_project_override = true
}

provider "google-beta" {
  project               = var.project
  region                = var.region
  billing_project       = var.project
  user_project_override = true
}
