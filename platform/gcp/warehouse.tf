# The warehouse: one BigQuery dataset per data product in access/personas.yaml and one per model
# layer no persona reads, all in the fences' free-tier region, where the landing bucket is, so load
# jobs read it in place. Empty datasets and their IAM are free; tables bill as storage beyond the
# free 10 GiB a month and queries as bytes beyond the free 1 TiB, under the daily query cap.

locals {
  access = yamldecode(file("${path.module}/../../access/personas.yaml"))

  # BigQuery dataset ids take underscores, not hyphens.
  datasets = merge(
    {
      landed       = "The landed files as loaded from the landing bucket, as the publishers' text."
      intermediate = "The model layers between the landed files and the data products."
    },
    { for p in local.access.data_products : replace(p.id, "-", "_") => p.holds },
  )

  # The accesses that reach a product's rows (access/identities.py's READS) and the dataset role
  # each takes; aggregate, draft and post read nothing in the warehouse.
  dataset_roles = {
    read = "roles/bigquery.dataViewer"
    edit = "roles/bigquery.dataEditor"
  }
  dataset_grants = {
    for g in distinct([
      for g in local.access.grants : {
        persona = g.persona
        dataset = replace(g.product, "-", "_")
        role    = local.dataset_roles[g.access]
      } if contains(keys(local.dataset_roles), g.access)
    ]) : "${g.persona} ${g.dataset} ${g.role}" => g
  }
  # The personas that read the warehouse, so need to run a query at all.
  warehouse_readers = toset([for g in local.dataset_grants : g.persona])
}

resource "google_bigquery_dataset" "warehouse" {
  for_each    = local.datasets
  dataset_id  = each.key
  location    = var.region
  description = each.value
}

# What the agent needs to build the models in a dataset: create, load, read, replace and drop
# tables and views (data editor's table and data permissions), its routines for the models' own
# functions, row access policies with their grantees, and setting a policy tag on a column. Not
# the dataset's or a table's IAM (the predefined data owner role could grant anyone, allUsers
# included, any role there) and not deleting the dataset.
resource "google_project_iam_custom_role" "warehouse_builder" {
  role_id     = "labWarehouseBuilder"
  title       = "Builds the warehouse models"
  description = "Granted per dataset: builds tables, their row access policies and policy tags."
  permissions = [
    "bigquery.datasets.get",
    "bigquery.routines.create",
    "bigquery.routines.delete",
    "bigquery.routines.get",
    "bigquery.routines.list",
    "bigquery.routines.update",
    "bigquery.rowAccessPolicies.create",
    "bigquery.rowAccessPolicies.delete",
    "bigquery.rowAccessPolicies.get",
    "bigquery.rowAccessPolicies.getIamPolicy",
    "bigquery.rowAccessPolicies.list",
    "bigquery.rowAccessPolicies.setIamPolicy",
    "bigquery.rowAccessPolicies.update",
    "bigquery.tables.create",
    "bigquery.tables.createSnapshot",
    "bigquery.tables.delete",
    "bigquery.tables.export",
    "bigquery.tables.get",
    "bigquery.tables.getData",
    "bigquery.tables.getIamPolicy",
    "bigquery.tables.list",
    "bigquery.tables.restoreSnapshot",
    "bigquery.tables.setCategory",
    "bigquery.tables.update",
    "bigquery.tables.updateData",
  ]
}

# The builder role on each warehouse dataset, never on the project.
resource "google_bigquery_dataset_iam_member" "agent_builds_warehouse" {
  for_each   = google_bigquery_dataset.warehouse
  dataset_id = each.value.dataset_id
  role       = google_project_iam_custom_role.warehouse_builder.name
  member     = "serviceAccount:${data.google_service_account.agent.email}"
}

# Each persona reads (or, with an edit grant, changes) the datasets of the products it is granted;
# the row access policies on entity-scoped products narrow a read to the granted entities.
resource "google_bigquery_dataset_iam_member" "persona_reads_product" {
  for_each   = local.dataset_grants
  dataset_id = google_bigquery_dataset.warehouse[each.value.dataset].dataset_id
  role       = each.value.role
  member     = "serviceAccount:${google_service_account.persona[each.value.persona].email}"
}

# Running a query is a job in the project, so its permission is granted only on the project: this
# role holds that and reading the project's BigQuery settings, and nothing else (the predefined
# job user role also opens Gemini, Dataform and conversational analytics).
resource "google_project_iam_custom_role" "query_runner" {
  role_id     = "labQueryRunner"
  title       = "Runs BigQuery queries"
  description = "Creates BigQuery jobs in the lab project; what a job reads is granted per dataset."
  permissions = ["bigquery.config.get", "bigquery.jobs.create"]
}

resource "google_project_iam_member" "persona_runs_queries" {
  for_each = local.warehouse_readers
  project  = var.project
  role     = google_project_iam_custom_role.query_runner.name
  member   = "serviceAccount:${google_service_account.persona[each.key].email}"
}
