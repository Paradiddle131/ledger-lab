data "google_project" "lab" {
  project_id = var.project
}

# Budget notifications go here; the kill switch listens on it. Pub/Sub stays inside its free tier.
resource "google_pubsub_topic" "budget" {
  name = "lab-budget-notifications"
}

# A $1.00 budget on the lab project, credits included, alerting at 1, 3 and 5 cents and at the
# dollar. Every notification, alert or not, also goes to the topic for the kill switch.
# The budget's amount and the kill threshold are dollars, and the Budgets API refuses an amount in
# another currency than the account's with a bare 400: name that cause before the call instead.
data "google_billing_account" "lab" {
  provider        = google.billing
  billing_account = var.billing_account
}

resource "google_billing_budget" "lab" {
  provider        = google.billing
  billing_account = var.billing_account

  lifecycle {
    precondition {
      condition     = data.google_billing_account.lab.currency_code == "USD"
      error_message = "The billing account bills in ${data.google_billing_account.lab.currency_code}, not USD: the budget and the kill threshold are dollar amounts."
    }
  }
  display_name = "ledger-lab spend fence"

  budget_filter {
    projects               = ["projects/${data.google_project.lab.number}"]
    credit_types_treatment = "INCLUDE_ALL_CREDITS"
  }

  amount {
    specified_amount {
      currency_code = "USD"
      units         = "1"
    }
  }

  threshold_rules {
    threshold_percent = 0.01
  }
  threshold_rules {
    threshold_percent = 0.03
  }
  threshold_rules {
    threshold_percent = 0.05
  }
  threshold_rules {
    threshold_percent = 1.0
  }

  all_updates_rule {
    pubsub_topic   = google_pubsub_topic.budget.id
    schema_version = "1.0"
  }
}
