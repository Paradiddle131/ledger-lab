"""The lab project's spend fences, read offline from their definitions, sit inside the free tier.

Nothing here plans, applies or calls GCP; `make fences-validate` checks the same files with
OpenTofu.
"""

import ast
import re
from typing import Any

import pytest
import yaml
from fences import CONFIG, GCP, blocks, declared, one, unquote, variable

QUOTA = "google_service_usage_consumer_quota_override"
FREE_REGIONS = {"us-west1", "us-central1", "us-east1"}
FREE_MONTHLY_QUERY_MIB = 1024 * 1024  # 1 TiB
# Every resource type the fences may declare; none of them bills inside the free tier.
ALLOWED = {
    "google_billing_budget",
    "google_pubsub_topic",
    "google_service_usage_consumer_quota_override",
    "google_service_account",
    "google_project_iam_member",
    "google_workflows_workflow",
    "google_eventarc_trigger",
}


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
    (local,) = CONFIG["locals"]
    assert {unquote(c) for c in local["dollar_like_currencies"]} == {"USD", "EUR", "GBP", "CHF"}


@pytest.mark.parametrize("name", ["project", "billing_account"])
def test_identifiers_come_from_variables_with_no_default(name: str) -> None:
    assert "default" not in variable(name)


def test_the_live_check_reads_its_expected_values_from_these_definitions() -> None:
    expected = declared()
    assert expected.query_cap_mib == int(unquote(one(QUOTA)["override_value"]))
    assert expected.query_cap_metric == "bigquery.googleapis.com/quota/query/usage"
    assert expected.query_cap_limit == "/d/project"
    assert expected.region in FREE_REGIONS
    assert expected.kill_threshold_bounds == (1, 10)
    assert expected.budget_thresholds == tuple(
        (t, "CURRENT_SPEND") for t in (0.01, 0.03, 0.05, 1.0)
    )
    assert expected.budget_period == "MONTH"
    assert expected.kill_switch_roles == (
        "roles/billing.projectManager",
        "roles/eventarc.eventReceiver",
        "roles/workflows.invoker",
    )
    assert expected.workflow_source == (GCP / "kill_switch.workflow.yaml").read_text()
    unresolved = [k for k, v in vars(expected).items() if k != "workflow_source" and "${" in str(v)]
    assert unresolved == []
