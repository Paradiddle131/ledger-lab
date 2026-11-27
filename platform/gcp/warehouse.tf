# The warehouse: one BigQuery dataset per data product in access/personas.yaml, one per model
# layer no persona reads, and one of views per product with aggregate tables and per purpose in
# access/purposes.yaml, all in the fences' free-tier region, where the landing bucket is, so load
# jobs read it in place. Empty datasets, views and their IAM are free; tables bill as storage
# beyond the free 10 GiB a month and queries as bytes beyond the free 1 TiB, under the daily query
# cap.

locals {
  access     = yamldecode(file("${path.module}/../../access/personas.yaml"))
  aggregates = yamldecode(file("${path.module}/../../access/aggregates.yaml"))
  purposes   = yamldecode(file("${path.module}/../../access/purposes.yaml"))

  # Each product with aggregate tables gets its own dataset of their views
  # (access/thresholds.py), read by that product's aggregate grantees alone; it reads its
  # product's rows through the source dataset's authorization of its views.
  aggregate_datasets = {
    for p in keys(local.aggregates.products) : "${replace(p, "-", "_")}_aggregates" => p
  }

  # Each purpose gets its own dataset of authorised views (access/purpose_views.py), the id of the
  # schema the local engine stores its tables in: one view per table it lists, of the purpose's
  # columns, read by the purpose's personas alone.
  purpose_datasets = { for u in local.purposes.purposes : replace(u.id, "-", "_") => u }

  # BigQuery dataset ids take underscores, not hyphens.
  datasets = merge(
    {
      landed       = "The landed files as loaded from the landing bucket, as the publishers' text."
      intermediate = "The model layers between the landed files and the data products."
    },
    { for p in local.access.data_products : replace(p.id, "-", "_") => p.holds },
    { for d, p in local.aggregate_datasets : d => "Aggregate views of ${p}: only groups of at least ${local.aggregates.minimum_group} payees." },
    { for d, u in local.purpose_datasets : d => "Views of the ${u.id} purpose's columns, under their sources' row and column rules." },
  )
  # The datasets the agent builds the models in: every one but the views' (aggregates and
  # purposes), which the owner writes (access/thresholds.py, access/purpose_views.py), since a view
  # there reads its source as itself.
  view_datasets = setunion(keys(local.aggregate_datasets), keys(local.purpose_datasets))
  built         = { for d, _ in local.datasets : d => d if !contains(local.view_datasets, d) }

  # A dataset's access list holds each role in its basic form when one exists, and the provider
  # compares the list as the API returns it, so a grant names the basic form.
  basic_roles = {
    "roles/bigquery.dataViewer" = "READER"
    "roles/bigquery.dataEditor" = "WRITER"
  }

  # The accesses that reach a product's rows (access/identities.py's READS) and the dataset role
  # each takes; aggregate, draft and post read nothing in the warehouse.
  dataset_roles = {
    read = "roles/bigquery.dataViewer"
    edit = "roles/bigquery.dataEditor"
  }
  # A product the models build tables of is read only through purposes, so a grant on it takes no
  # role on its dataset; a product with no tables yet keeps its dataset role.
  purpose_products = toset([
    for p in local.access.data_products : p.id if length(try(p.tables, [])) > 0
  ])
  dataset_grants = {
    for g in distinct([
      for g in local.access.grants : {
        persona = g.persona
        dataset = replace(g.product, "-", "_")
        role    = local.dataset_roles[g.access]
      } if contains(keys(local.dataset_roles), g.access) && !contains(local.purpose_products, g.product)
    ]) : "${g.persona} ${g.dataset} ${g.role}" => g
  }
  # Each persona of a purpose: a read of that purpose's views.
  purpose_grants = {
    for g in distinct(flatten([
      for d, u in local.purpose_datasets : [for p in u.personas : { persona = p, dataset = d }]
    ])) : "${g.persona} ${g.dataset}" => g
  }
  # Each aggregate grant on a product with aggregate tables: a read of that product's views.
  aggregate_grants = {
    for g in distinct([
      for g in local.access.grants : {
        persona = g.persona
        dataset = "${replace(g.product, "-", "_")}_aggregates"
      } if g.access == "aggregate" && contains(keys(local.aggregates.products), g.product)
    ]) : "${g.persona} ${g.dataset}" => g
  }
  # The personas that read the warehouse, so need to run a query at all.
  warehouse_readers = toset(concat(
    [for g in local.dataset_grants : g.persona],
    [for g in local.aggregate_grants : g.persona],
    [for g in local.purpose_grants : g.persona],
  ))
  # The dataset each aggregates dataset's views read: an entity-scoped product's mart in
  # `intermediate` (its product dataset holds the governed copy, under row access policies), a
  # group product's own dataset.
  aggregate_sources = {
    for d, p in local.aggregate_datasets : d => (
      contains(local.governed, replace(p, "-", "_")) ? "intermediate" : replace(p, "-", "_")
    )
  }
  # The dataset each product table sits in: its product's (a governed copy and its masked copy, or
  # a group product's own table).
  table_datasets = merge([
    for p in local.access.data_products : { for t in try(p.tables, []) : t => replace(p.id, "-", "_") }
  ]...)
  # The datasets each purpose dataset's views read: those of the tables the purpose lists, never
  # `intermediate`, so the row access policies on a governed copy act on the querying persona.
  purpose_sources = {
    for s in distinct(flatten([
      for d, u in local.purpose_datasets : [
        for t in keys(u.tables) : { views = d, source = local.table_datasets[t] }
      ]
    ])) : "${s.views} ${s.source}" => s
  }
  # The datasets of the entity-scoped products with tables: each holds a governed copy of its mart
  # (access/row_policies.py), which the publisher fills.
  governed = toset([
    for p in local.access.data_products : replace(p.id, "-", "_")
    if p.scope == "entity" && length(try(p.tables, [])) > 0
  ])
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

# Every dataset grant is one entry of the dataset's access list (`google_bigquery_dataset_access`),
# never a dataset IAM member: the provider's IAM member rewrites the whole list from its role
# entries, dropping a view authorization, and the two cannot be used on one dataset.

# The builder role on each dataset the models build, never on the project.
resource "google_bigquery_dataset_access" "agent_builds_warehouse" {
  for_each      = local.built
  dataset_id    = google_bigquery_dataset.warehouse[each.key].dataset_id
  role          = google_project_iam_custom_role.warehouse_builder.name
  user_by_email = data.google_service_account.agent.email
}

# Each persona reads (or, with an edit grant, changes) the datasets of the products it is granted
# that the models build no tables of yet; a product with tables is read through purposes (below).
resource "google_bigquery_dataset_access" "persona_reads_product" {
  for_each      = local.dataset_grants
  dataset_id    = google_bigquery_dataset.warehouse[each.value.dataset].dataset_id
  role          = local.basic_roles[each.value.role]
  user_by_email = google_service_account.persona[each.value.persona].email
}

# An aggregate grant reads its product's aggregate views and nothing else in the warehouse.
resource "google_bigquery_dataset_access" "persona_reads_aggregates" {
  for_each      = local.aggregate_grants
  dataset_id    = google_bigquery_dataset.warehouse[each.value.dataset].dataset_id
  role          = local.basic_roles["roles/bigquery.dataViewer"]
  user_by_email = google_service_account.persona[each.value.persona].email
}

# The views of each aggregates dataset read their source dataset as themselves (an authorized
# dataset): a reader of the views holds no grant on the source.
resource "google_bigquery_dataset_access" "aggregates_read_source" {
  for_each   = local.aggregate_sources
  dataset_id = google_bigquery_dataset.warehouse[each.value].dataset_id
  dataset {
    dataset {
      project_id = var.project
      dataset_id = google_bigquery_dataset.warehouse[each.key].dataset_id
    }
    target_types = ["VIEWS"]
  }
}

# A persona with a purpose reads that purpose's views, and through them its products' rows, in
# place of a role on the product's dataset.
resource "google_bigquery_dataset_access" "persona_reads_purposes" {
  for_each      = local.purpose_grants
  dataset_id    = google_bigquery_dataset.warehouse[each.value.dataset].dataset_id
  role          = local.basic_roles["roles/bigquery.dataViewer"]
  user_by_email = google_service_account.persona[each.value.persona].email
}

# The views of each purpose dataset read the datasets of the tables it lists (an authorized
# dataset), where the row access policies and policy tags on the source act on the persona querying
# the view: a reader of the views holds no grant on the source.
resource "google_bigquery_dataset_access" "purposes_read_sources" {
  for_each   = local.purpose_sources
  dataset_id = google_bigquery_dataset.warehouse[each.value.source].dataset_id
  dataset {
    dataset {
      project_id = var.project
      dataset_id = google_bigquery_dataset.warehouse[each.value.views].dataset_id
    }
    target_types = ["VIEWS"]
  }
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

# Publishing a governed copy replaces its rows from its mart: read the table and its data and write
# its data; no table create or delete, no access setting and no row access policy.
resource "google_project_iam_custom_role" "row_publisher" {
  role_id     = "labRowPublisher"
  title       = "Publishes the governed copies"
  description = "Granted per governed dataset: replaces a table's rows, nothing else."
  permissions = ["bigquery.tables.get", "bigquery.tables.getData", "bigquery.tables.updateData"]
}

resource "google_bigquery_dataset_access" "publisher_writes_governed_copies" {
  for_each      = local.governed
  dataset_id    = google_bigquery_dataset.warehouse[each.key].dataset_id
  role          = google_project_iam_custom_role.row_publisher.name
  user_by_email = google_service_account.publisher.email
}

# The marts it copies from: read only, on the layer no persona reads.
resource "google_bigquery_dataset_access" "publisher_reads_marts" {
  dataset_id    = google_bigquery_dataset.warehouse["intermediate"].dataset_id
  role          = local.basic_roles["roles/bigquery.dataViewer"]
  user_by_email = google_service_account.publisher.email
}

resource "google_project_iam_member" "publisher_runs_queries" {
  project = var.project
  role    = google_project_iam_custom_role.query_runner.name
  member  = "serviceAccount:${google_service_account.publisher.email}"
}
