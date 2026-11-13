# The persona identities: one service account per persona in access/personas.yaml (account id =
# persona id); the access rules (warehouse.tf, columns.tf) grant their data rights and, to those
# that read the warehouse, the one project role that runs a query. Tests act as a
# persona by minting its token as the agent account, the session's injected credential, which was
# created by hand and is only referenced here. Service accounts and their IAM are free.

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
