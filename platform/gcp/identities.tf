# The persona identities: one service account per persona in access/personas.yaml (account id =
# persona id); the access rules (warehouse.tf, columns.tf) grant their data rights and, to those
# that read the warehouse, the one project role that runs a query. Tests act as a
# persona by minting its token through the agent account, which was created by hand and is only
# referenced here: on the owner's machine the owner's login mints through it, by a grant on the
# agent account made by hand (access/bigquery.py). Service accounts and their IAM are free.

locals {
  personas = { for p in yamldecode(file("${path.module}/../../access/personas.yaml")).personas : p.id => p.name }
}

data "google_service_account" "agent" {
  account_id = var.agent_account_id
}

resource "google_service_account" "persona" {
  for_each     = local.personas
  account_id   = each.key
  display_name = each.value
}

# Token creation on each persona account only, never on the project: the agent can act as a
# persona and as nobody else.
resource "google_service_account_iam_member" "agent_mints_persona_tokens" {
  for_each           = google_service_account.persona
  service_account_id = each.value.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:${data.google_service_account.agent.email}"
}

# The publisher: the one account a governed copy's publish runs as (access/row_policies.py). Its
# row access policy is the one that grants every row of the copy, which BigQuery asks of a DELETE
# or INSERT on a table with policies. The agent mints its token, as it mints a persona's, so the
# owner's login reaches it by delegation and never acts as the agent itself.
resource "google_service_account" "publisher" {
  account_id   = "publisher"
  display_name = "Publishes the governed copies"
}

resource "google_service_account_iam_member" "agent_mints_publisher_token" {
  service_account_id = google_service_account.publisher.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:${data.google_service_account.agent.email}"
}
