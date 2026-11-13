data "google_project" "lab" {
  project_id = var.project
}

# Budget notifications go here; the kill switch listens on it. Pub/Sub stays inside its free tier.
resource "google_pubsub_topic" "budget" {
  name = "lab-budget-notifications"
}

# A $1.00 budget on the lab project, credits included, alerting at 1, 3 and 5 cents and at the
# dollar. Every notification, alert or not, also goes to the topic for the kill switch.
resource "google_billing_budget" "lab" {
  billing_account = var.billing_account
  display_name    = "ledger-lab spend fence"

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
