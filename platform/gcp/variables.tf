variable "project" {
  description = "The lab's own project id: pass LEDGER_LAB_GCP_PROJECT, never the session's GCP_PROJECT."
  type        = string
}

variable "billing_account" {
  description = "The billing account the budget watches, as its bare id."
  type        = string
}

variable "agent_account_id" {
  description = "The account id (the part before the @) of the agent service account, the session's injected credential, created by hand in the lab project."
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

variable "kill_threshold" {
  description = "Month-to-date cost, in the billing account's currency, at which the kill switch unlinks billing: far above the first hundredth, which only alerts."
  type        = number
  default     = 5
  validation {
    condition     = var.kill_threshold >= 1 && var.kill_threshold <= 10
    error_message = "Keep the kill switch between 1 and 10 units of the account's currency."
  }
}
