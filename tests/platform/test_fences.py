"""The lab project's definitions, read offline: the spend fences sit inside the free tier, and the
persona identities, their token grants and the landing bucket give the agent nothing that moves one.

Nothing here plans, applies or calls GCP; `make fences-validate` checks the same files with
OpenTofu.
"""

import ast
import re
from pathlib import Path
from typing import Any

import hcl2
import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
GCP = REPO / "platform/gcp"
FREE_REGIONS = {"us-west1", "us-central1", "us-east1"}
FREE_MONTHLY_QUERY_MIB = 1024 * 1024  # 1 TiB
# Every resource type the definitions may declare; none of them bills inside the free tier.
ALLOWED = {
    "google_billing_budget",
    "google_pubsub_topic",
    "google_service_usage_consumer_quota_override",
    "google_service_account",
    "google_project_iam_member",
    "google_workflows_workflow",
    "google_eventarc_trigger",
    "google_service_account_iam_member",
    "google_storage_bucket",
    "google_storage_bucket_iam_member",
}
# A service account id: 6 to 30 characters, lowercase letters, digits and hyphens, from a letter.
ACCOUNT_ID = re.compile(r"[a-z][a-z0-9-]{4,28}[a-z0-9]")
AGENT_MEMBER = "serviceAccount:${data.google_service_account.agent.email}"


def unquote(value: Any) -> Any:
    if isinstance(value, str) and len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1]
    return value


def load() -> dict[str, list[Any]]:
    merged: dict[str, list[Any]] = {}
    for path in sorted(GCP.glob("*.tf")):
        with path.open() as handle:
            for key, value in hcl2.load(handle).items():
                if isinstance(value, list):
                    merged.setdefault(key, []).extend(value)
    return merged


CONFIG = load()


def blocks(kind: str) -> dict[tuple[str, str], dict[str, Any]]:
    """Resources (or data sources) by (type, name)."""
    found = {}
    for entry in CONFIG.get(kind, []):
        for rtype, named in entry.items():
            for name, body in named.items():
                found[(unquote(rtype), unquote(name))] = body
    return found


def one(rtype: str) -> dict[str, Any]:
    matches = [body for (t, _), body in blocks("resource").items() if t == rtype]
    assert len(matches) == 1, rtype
    return matches[0]


def local(name: str) -> Any:
    values = [entry[name] for entry in CONFIG["locals"] if name in entry]
    assert len(values) == 1, name
    return values[0]


def variable(name: str) -> dict[str, Any]:
    for entry in CONFIG["variable"]:
        if f'"{name}"' in entry:
            return entry[f'"{name}"']
    raise KeyError(name)


def test_only_allowlisted_resources_are_declared() -> None:
    declared = {rtype for rtype, _ in blocks("resource")}
    assert declared - ALLOWED == set()
    assert declared == ALLOWED, "a fence is missing"


def test_providers_are_pinned_exactly() -> None:
    providers = CONFIG["terraform"][0]["required_providers"][0]
    for name in ("google", "google-beta"):
        assert re.fullmatch(r"\d+\.\d+\.\d+", unquote(providers[name]["version"])), name


def test_the_budget_alerts_from_the_first_cent_and_spans_the_lab_project_with_credits() -> None:
    budget = one("google_billing_budget")
    amount = budget["amount"][0]["specified_amount"][0]
    assert "data.google_billing_account.lab.currency_code" in amount["currency_code"]
    units = float(unquote(amount["units"]))
    assert units <= 1
    cents = sorted(round(r["threshold_percent"] * units * 100) for r in budget["threshold_rules"])
    assert cents[0] == 1, "the first alert is the first hundredth"
    assert all(c <= 5 for c in cents[:-1]), "alerts below the unit are a few hundredths"
    budget_filter = budget["budget_filter"][0]
    assert unquote(budget_filter["credit_types_treatment"]) == "INCLUDE_ALL_CREDITS"
    assert "data.google_project.lab.number" in budget_filter["projects"][0]
    assert "google_pubsub_topic.budget" in budget["all_updates_rule"][0]["pubsub_topic"]


def test_the_daily_query_cap_stays_inside_the_free_monthly_bytes() -> None:
    quota = one("google_service_usage_consumer_quota_override")
    assert unquote(quota["service"]) == "bigquery.googleapis.com"
    assert "bigquery.googleapis.com/quota/query/usage" in quota["metric"]
    assert "/d/project" in quota["limit"]
    per_day_mib = int(unquote(quota["override_value"]))
    assert 0 < per_day_mib * 31 <= FREE_MONTHLY_QUERY_MIB


def test_regional_pieces_run_in_a_free_tier_region() -> None:
    region = variable("region")
    assert unquote(region["default"]) in FREE_REGIONS
    listed = re.search(r"\[.*?\]", region["validation"][0]["condition"])
    assert listed is not None
    assert set(ast.literal_eval(listed[0])) == FREE_REGIONS
    assert one("google_workflows_workflow")["region"] == "${var.region}"
    assert one("google_eventarc_trigger")["location"] == "${var.region}"


def test_the_kill_switch_fires_far_above_the_alerts_and_holds_one_billing_role() -> None:
    threshold = variable("kill_threshold")
    assert 1 <= threshold["default"] <= 10
    roles = sorted(
        unquote(body["role"])
        for (rtype, _), body in blocks("resource").items()
        if rtype == "google_project_iam_member"
    )
    assert roles == [
        "roles/billing.projectManager",
        "roles/eventarc.eventReceiver",
        "roles/workflows.invoker",
    ]
    trigger = one("google_eventarc_trigger")
    assert "google_pubsub_topic.budget" in trigger["transport"][0]["pubsub"][0]["topic"]
    assert "google_workflows_workflow.kill_switch" in trigger["destination"][0]["workflow"]


def test_the_workflow_unlinks_billing_only_above_the_threshold() -> None:
    steps = yaml.safe_load((GCP / "kill_switch.workflow.yaml").read_text())["main"]["steps"]
    by_name = {name: body for step in steps for name, body in step.items()}
    condition = by_name["decide"]["switch"][0]
    assert condition == {"condition": "${cost > threshold}", "next": "unlink"}
    unlink = by_name["unlink"]
    assert unlink["call"] == "http.put"
    assert unlink["args"]["url"].startswith('${"https://cloudbilling.googleapis.com/v1/projects/"')
    assert unlink["args"]["url"].endswith('+ "/billingInfo"}')
    assert unlink["args"]["auth"] == {"type": "OAuth2"}
    assert unlink["args"]["body"] == {"billingAccountName": ""}


def providers() -> dict[str, dict[str, Any]]:
    """Provider bodies by name, an aliased one as `<name>.<alias>`."""
    found = {}
    for entry in CONFIG["provider"]:
        ((name, body),) = entry.items()
        alias = unquote(body.get("alias"))
        found[f"{unquote(name)}.{alias}" if alias else unquote(name)] = body
    return found


def test_billing_account_calls_are_billed_to_the_lab_project() -> None:
    billing = providers()["google.billing"]
    assert billing["billing_project"] == "${var.project}"
    assert billing["user_project_override"] is True
    assert unquote(one("google_billing_budget")["provider"]) == "${google.billing}"


def test_project_resources_carry_no_quota_override() -> None:
    # Eventarc forwards the override to Pub/Sub as its service agent, which is refused.
    for name in ("google", "google-beta"):
        assert "user_project_override" not in providers()[name]


def test_the_budget_refuses_an_account_whose_unit_is_far_from_a_dollar() -> None:
    (lifecycle,) = one("google_billing_budget")["lifecycle"]
    (check,) = lifecycle["precondition"]
    assert "local.dollar_like_currencies" in check["condition"]
    currencies = {unquote(c) for c in local("dollar_like_currencies")}
    assert currencies == {"USD", "EUR", "GBP", "CHF"}


@pytest.mark.parametrize("name", ["project", "billing_account", "agent_account_id"])
def test_identifiers_come_from_variables_with_no_default(name: str) -> None:
    assert "default" not in variable(name)


def persona_ids() -> list[str]:
    personas = yaml.safe_load((REPO / "access/personas.yaml").read_text())["personas"]
    return [p["id"] for p in personas]


def test_every_persona_id_is_a_valid_service_account_id() -> None:
    assert [i for i in persona_ids() if not ACCOUNT_ID.fullmatch(i)] == []


# The persona accounts' ids come from the persona file itself, keyed by id, so the list is never
# written twice.
PERSONAS_FROM_FILE = re.compile(
    r"\$\{\{for (\w+) in "
    r'yamldecode\(file\("\$\{path\.module\}/\.\./\.\./access/personas\.yaml"\)\)\.personas'
    r" : \1\.id => \1\.name\}\}"
)


def test_one_service_account_per_persona_and_no_more() -> None:
    accounts = {
        name: body
        for (rtype, name), body in blocks("resource").items()
        if rtype == "google_service_account"
    }
    assert set(accounts) == {"kill_switch", "persona"}
    persona = accounts["persona"]
    assert persona["for_each"] == "${local.personas}"
    assert persona["account_id"] == "${each.key}"
    assert PERSONAS_FROM_FILE.fullmatch(local("personas"))
    assert unquote(accounts["kill_switch"]["account_id"]) not in persona_ids()


def test_the_agent_account_is_referenced_by_a_variable_never_managed_here() -> None:
    agent = blocks("data")[("google_service_account", "agent")]
    assert agent["account_id"] == "${var.agent_account_id}"


def test_only_the_agent_mints_tokens_and_only_for_persona_accounts() -> None:
    grant = one("google_service_account_iam_member")
    assert grant["for_each"] == "${google_service_account.persona}"
    assert grant["service_account_id"] == "${each.value.name}"
    assert unquote(grant["role"]) == "roles/iam.serviceAccountTokenCreator"
    assert unquote(grant["member"]) == AGENT_MEMBER


def test_no_project_level_role_reaches_anyone_but_the_kill_switch() -> None:
    members = [
        unquote(body["member"])
        for (rtype, _), body in blocks("resource").items()
        if rtype == "google_project_iam_member"
    ]
    assert members, "the kill switch's roles are project-level"
    assert set(members) == {"serviceAccount:${google_service_account.kill_switch.email}"}


def test_the_landing_bucket_is_free_tier_standard_private_and_named_from_the_project() -> None:
    bucket = one("google_storage_bucket")
    assert bucket["location"] == "${var.region}"
    assert unquote(bucket["storage_class"]) == "STANDARD"
    assert bucket["uniform_bucket_level_access"] is True
    assert unquote(bucket["public_access_prevention"]) == "enforced"
    assert unquote(bucket["name"]).startswith("${var.project}")
    # Soft-deleted bytes bill as storage: keeping none leaves only live objects on the allowance.
    assert bucket["soft_delete_policy"][0]["retention_duration_seconds"] == 0
    assert "versioning" not in bucket


def test_only_the_agent_holds_object_rights_and_only_on_the_landing_bucket() -> None:
    access = one("google_storage_bucket_iam_member")
    assert access["bucket"] == "${google_storage_bucket.landing.name}"
    assert unquote(access["role"]) == "roles/storage.objectUser"
    assert unquote(access["member"]) == AGENT_MEMBER
