# The native data agent (agent/native.py): Google's Conversational Analytics API, asked as each
# persona that reads the warehouse, on the persona's own token. Its tokens are free through
# 2026-12-31 by the Data Agents pricing page, and the mode stops calling in code a day before;
# its queries bill as BigQuery queries, each capped by the call. Enabling the two APIs and the
# role is the owner's apply, with the month's cost read before and after.

resource "google_project_service" "native_agent" {
  for_each = toset(["geminidataanalytics.googleapis.com", "cloudaicompanion.googleapis.com"])
  project  = var.project
  service  = each.value
  # Turning the API off is a separate owner decision, not a side effect of a destroy.
  disable_on_destroy = false
}

# The stateless chat permission (`geminidataanalytics.locations.chat`), in the predefined role that
# holds it and one other permission; not the setup page's Gemini for Google Cloud user role, which
# lacks that permission and holds sixteen others, a licence self-assignment among them. The
# personas that read the warehouse only, so a persona with no data grant cannot ask.
resource "google_project_iam_member" "persona_asks_native_agent" {
  for_each = local.warehouse_readers
  project  = var.project
  role     = "roles/geminidataanalytics.dataAgentStatelessUser"
  member   = "serviceAccount:${google_service_account.persona[each.key].email}"
}
