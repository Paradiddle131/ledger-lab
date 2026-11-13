# Column-level access: one policy-tag taxonomy, a policy tag per tag in access/columns.yaml with
# the built-in masking rule it names, and per tag the personas that read it in clear (fine-grained
# reader on the tag) or masked (masked reader on its masking rule). A persona with neither is
# refused the column. All in the datasets' region, as BigQuery requires. Policy tags, masking
# rules and their IAM are free; the models attach the tags to columns when they create tables.

locals {
  columns = { for t in yamldecode(file("${path.module}/../../access/columns.yaml")).tags : t.id => t }

  # The catalogue's masking rules as BigQuery's built-in masking expressions.
  masking_expressions = {
    nullify     = "ALWAYS_NULL"
    hash        = "SHA256"
    "last-four" = "LAST_FOUR_CHARACTERS"
  }

  clear_readers = {
    for r in flatten([for t in local.columns : [for p in t.clear : { tag = t.id, persona = p }]]) :
    "${r.tag} ${r.persona}" => r
  }
  masked_readers = {
    for r in flatten([for t in local.columns : [for p in t.masked : { tag = t.id, persona = p }]]) :
    "${r.tag} ${r.persona}" => r
  }
}

resource "google_data_catalog_taxonomy" "sensitive" {
  region                 = var.region
  display_name           = "ledger-lab sensitive columns"
  description            = "Policy tags for the columns that hold bank details or stand for a person."
  activated_policy_types = ["FINE_GRAINED_ACCESS_CONTROL"]
}

resource "google_data_catalog_policy_tag" "sensitive" {
  for_each     = local.columns
  taxonomy     = google_data_catalog_taxonomy.sensitive.id
  display_name = each.key
  description  = each.value.marks
}

resource "google_bigquery_datapolicy_data_policy" "masking" {
  for_each         = local.columns
  location         = var.region
  data_policy_id   = replace(each.key, "-", "_")
  policy_tag       = google_data_catalog_policy_tag.sensitive[each.key].name
  data_policy_type = "DATA_MASKING_POLICY"

  data_masking_policy {
    predefined_expression = local.masking_expressions[each.value.masking]
  }
}

resource "google_data_catalog_policy_tag_iam_member" "persona_reads_in_clear" {
  for_each   = local.clear_readers
  policy_tag = google_data_catalog_policy_tag.sensitive[each.value.tag].name
  role       = "roles/datacatalog.categoryFineGrainedReader"
  member     = "serviceAccount:${google_service_account.persona[each.value.persona].email}"
}

resource "google_bigquery_datapolicy_data_policy_iam_member" "persona_reads_masked" {
  for_each       = local.masked_readers
  location       = var.region
  data_policy_id = google_bigquery_datapolicy_data_policy.masking[each.value.tag].data_policy_id
  role           = "roles/bigquerydatapolicy.maskedReader"
  member         = "serviceAccount:${google_service_account.persona[each.value.persona].email}"
}

# The agent builds the models, which copy tagged columns from layer to layer: masked, it would
# store the mask, so it reads every tag in clear.
resource "google_data_catalog_policy_tag_iam_member" "agent_reads_in_clear" {
  for_each   = google_data_catalog_policy_tag.sensitive
  policy_tag = each.value.name
  role       = "roles/datacatalog.categoryFineGrainedReader"
  member     = "serviceAccount:${data.google_service_account.agent.email}"
}
