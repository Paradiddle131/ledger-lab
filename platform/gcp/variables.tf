variable "project" {
  description = "The lab's own project id: pass LEDGER_LAB_GCP_PROJECT, never the session's GCP_PROJECT."
  type        = string
}

variable "billing_account" {
  description = "The billing account the budget watches, as its bare id."
  type        = string
}

variable "region" {
  description = "Where the regional pieces run: one of the regions Google's free tier names."
  type        = string
  default     = "us-central1"
  validation {
    condition     = contains(["us-west1", "us-central1", "us-east1"], var.region)
    error_message = "Use a free-tier region: us-west1, us-central1 or us-east1."
  }
}

variable "kill_threshold_usd" {
  description = "Month-to-date cost at which the kill switch unlinks billing: far above the first cent, which only alerts."
  type        = number
  default     = 5
  validation {
    condition     = var.kill_threshold_usd >= 1 && var.kill_threshold_usd <= 10
    error_message = "Keep the kill switch between $1 and $10."
  }
}
