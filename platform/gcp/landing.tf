# Where the fetched source files land before the warehouse loads them. Standard storage in a
# free-tier region has an always-free allowance (5 GB-months and small monthly operation counts),
# and an empty bucket costs nothing. Soft delete is off because soft-deleted bytes bill as storage;
# with no versioning only live objects count against the allowance.
resource "google_storage_bucket" "landing" {
  name                        = "${var.project}-landing"
  location                    = var.region
  storage_class               = "STANDARD"
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  soft_delete_policy {
    retention_duration_seconds = 0
  }
}

# The only grant this configuration adds on the bucket: object rights for the agent, which lands
# and replaces files and can change neither the bucket nor who may use it. Cloud Storage's default
# bindings still give the project's viewers, editors and owners their legacy bucket roles.
resource "google_storage_bucket_iam_member" "agent_lands_objects" {
  bucket = google_storage_bucket.landing.name
  role   = "roles/storage.objectUser"
  member = "serviceAccount:${data.google_service_account.agent.email}"
}
