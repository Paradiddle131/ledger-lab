# BigQuery's query bytes, capped per day inside the free 1 TiB a month: 30 GiB a day (the value is
# in MiB) is at most 930 GiB in a 31-day month. This only lowers the default, which is unlimited;
# `force` is what the API asks for when a quota drops by more than 10%.
resource "google_service_usage_consumer_quota_override" "query_bytes_per_day" {
  provider       = google-beta
  project        = var.project
  service        = "bigquery.googleapis.com"
  metric         = urlencode("bigquery.googleapis.com/quota/query/usage")
  limit          = urlencode("/d/project")
  override_value = "30720"
  force          = true
}
