data "google_project" "lab" {
  project_id = var.project
}

# Budget notifications go here; the kill switch listens on it. Pub/Sub stays inside its free tier.
resource "google_pubsub_topic" "budget" {
  name = "lab-budget-notifications"
}

# The Budgets API takes amounts only in the billing account's own currency (any other is a bare
# 400), so the budget and the kill threshold are in it; the fences hold only for a currency whose
# unit is worth about a dollar, which the precondition names before the call.
locals {
  dollar_like_currencies = ["USD", "EUR", "GBP", "CHF"]
}

data "google_billing_account" "lab" {
  provider        = google.billing
  billing_account = var.billing_account
}

resource "google_billing_budget" "lab" {
  provider        = google.billing
  billing_account = var.billing_account

  lifecycle {
    precondition {
      condition     = contains(local.dollar_like_currencies, data.google_billing_account.lab.currency_code)
      error_message = "The billing account bills in ${data.google_billing_account.lab.currency_code}: the budget and the kill threshold assume a unit worth about a dollar."
    }
  }

  # One unit of the account's currency on the lab project, credits included, alerting at 1, 3 and
  # 5 hundredths and at the unit. Every notification, alert or not, also goes to the topic for the
  # kill switch.
  display_name = "ledger-lab spend fence"

  budget_filter {
    projects               = ["projects/${data.google_project.lab.number}"]
    credit_types_treatment = "INCLUDE_ALL_CREDITS"
  }

  amount {
    specified_amount {
      currency_code = data.google_billing_account.lab.currency_code
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
