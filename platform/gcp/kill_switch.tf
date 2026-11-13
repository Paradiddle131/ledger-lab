# The kill switch: a budget notification on the topic starts a workflow through Eventarc, and the
# workflow unlinks billing from the project once the month's cost passes the kill threshold.
# Eventarc's Pub/Sub source is free and the workflow takes a few steps per notification, inside
# Workflows' free monthly steps; no Cloud Run function, whose image would count against Artifact
# Registry's free storage. Unlinking billing stops every paid resource and may delete some.

resource "google_service_account" "kill_switch" {
  account_id   = "lab-kill-switch"
  display_name = "Unlinks billing when the lab's spend passes the kill threshold"
}

# Unlinking billing needs the project billing manager role, and only that.
resource "google_project_iam_member" "kill_switch_billing" {
  project = var.project
  role    = "roles/billing.projectManager"
  member  = "serviceAccount:${google_service_account.kill_switch.email}"
}

resource "google_project_iam_member" "kill_switch_receives_events" {
  project = var.project
  role    = "roles/eventarc.eventReceiver"
  member  = "serviceAccount:${google_service_account.kill_switch.email}"
}

resource "google_project_iam_member" "kill_switch_invokes_workflows" {
  project = var.project
  role    = "roles/workflows.invoker"
  member  = "serviceAccount:${google_service_account.kill_switch.email}"
}

resource "google_workflows_workflow" "kill_switch" {
  name            = "lab-kill-switch"
  region          = var.region
  service_account = google_service_account.kill_switch.id
  source_contents = file("${path.module}/kill_switch.workflow.yaml")
  user_env_vars = {
    KILL_THRESHOLD_USD = tostring(var.kill_threshold_usd)
  }
}

resource "google_eventarc_trigger" "budget_to_kill_switch" {
  name            = "lab-budget-to-kill-switch"
  location        = var.region
  service_account = google_service_account.kill_switch.email

  matching_criteria {
    attribute = "type"
    value     = "google.cloud.pubsub.topic.v1.messagePublished"
  }

  transport {
    pubsub {
      topic = google_pubsub_topic.budget.id
    }
  }

  destination {
    workflow = google_workflows_workflow.kill_switch.id
  }
}
