"""The lab project's spend fences, read offline from their definitions, sit inside the free tier.

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

GCP = Path(__file__).resolve().parents[2] / "platform/gcp"
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
    assert unquote(amount["currency_code"]) == "USD"
    dollars = float(unquote(amount["units"]))
    assert dollars <= 1
    cents = sorted(round(r["threshold_percent"] * dollars * 100) for r in budget["threshold_rules"])
    assert cents[0] == 1, "the first alert is the first cent"
    assert all(c <= 5 for c in cents[:-1]), "alerts below the dollar are a few cents"
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
    threshold = variable("kill_threshold_usd")
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


def test_the_budget_refuses_an_account_that_does_not_bill_in_dollars() -> None:
    (lifecycle,) = one("google_billing_budget")["lifecycle"]
    (check,) = lifecycle["precondition"]
    assert '== "USD"' in check["condition"]


@pytest.mark.parametrize("name", ["project", "billing_account"])
def test_identifiers_come_from_variables_with_no_default(name: str) -> None:
    assert "default" not in variable(name)
