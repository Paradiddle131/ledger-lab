"""The spend fences as `platform/gcp` declares them, read offline from the OpenTofu files.

The offline fence tests and the live cost check both read the declaration from here, so an expected
value is written once, in the `.tf` file that applies it.
"""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import hcl2

GCP = Path(__file__).resolve().parent / "gcp"
WORKFLOW_SOURCE = GCP / "kill_switch.workflow.yaml"


def unquote(value: Any) -> Any:
    if isinstance(value, str) and len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1]
    return value


def load() -> dict[str, list[Any]]:
    """Every top-level block of every `.tf` file, merged by kind."""
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
    """The only resource of a type; raises when there is none or several."""
    matches = [body for (t, _), body in blocks("resource").items() if t == rtype]
    if len(matches) != 1:
        raise LookupError(f"{rtype}: {len(matches)} declared, expected one")
    return matches[0]


def variable(name: str) -> dict[str, Any]:
    for entry in CONFIG["variable"]:
        if f'"{name}"' in entry:
            return entry[f'"{name}"']
    raise KeyError(name)


def _urlencoded_argument(expression: str) -> str:
    """`${urlencode("x")}` -> `x`."""
    found = re.fullmatch(r'\$\{urlencode\("([^"]+)"\)\}', expression)
    if found is None:
        raise ValueError(f'expected urlencode("..."), got {expression!r}')
    return found[1]


def _bounds(condition: str, name: str) -> tuple[float, float]:
    """The `>= low && <= high` bounds a variable's validation sets."""
    low = re.search(rf"var\.{name} >= ([0-9.]+)", condition)
    high = re.search(rf"var\.{name} <= ([0-9.]+)", condition)
    if low is None or high is None:
        raise ValueError(f"no bounds for {name} in {condition!r}")
    return float(low[1]), float(high[1])


@dataclass(frozen=True)
class Declared:
    """What the live check compares each applied fence with."""

    region: str
    topic: str
    query_cap_metric: str
    query_cap_limit: str
    query_cap_mib: int
    workflow: str
    workflow_source: str
    kill_switch_account: str
    kill_threshold_bounds: tuple[float, float]
    trigger: str
    trigger_event_type: str
    budget_name: str
    budget_units: str
    budget_thresholds: tuple[tuple[float, str], ...]
    budget_credits: str
    budget_period: str
    kill_switch_roles: tuple[str, ...]


# What the Budgets API applies to a field the definition leaves unset.
BUDGET_DEFAULT_PERIOD = "MONTH"
BUDGET_DEFAULT_SPEND_BASIS = "CURRENT_SPEND"
KILL_SWITCH_MEMBER = "serviceAccount:${google_service_account.kill_switch.email}"


def kill_switch_roles() -> tuple[str, ...]:
    """The project roles the definition grants the kill switch's account."""
    return tuple(
        sorted(
            unquote(body["role"])
            for (rtype, _), body in blocks("resource").items()
            if rtype == "google_project_iam_member"
            and unquote(body["member"]) == KILL_SWITCH_MEMBER
        )
    )


def declared() -> Declared:
    quota = one("google_service_usage_consumer_quota_override")
    workflow = one("google_workflows_workflow")
    trigger = one("google_eventarc_trigger")
    budget = one("google_billing_budget")
    (budget_filter,) = budget["budget_filter"]
    (criterion,) = trigger["matching_criteria"]
    (threshold_check,) = variable("kill_threshold")["validation"]
    return Declared(
        region=unquote(variable("region")["default"]),
        topic=unquote(one("google_pubsub_topic")["name"]),
        query_cap_metric=_urlencoded_argument(quota["metric"]),
        query_cap_limit=_urlencoded_argument(quota["limit"]),
        query_cap_mib=int(unquote(quota["override_value"])),
        workflow=unquote(workflow["name"]),
        workflow_source=WORKFLOW_SOURCE.read_text(),
        kill_switch_account=unquote(one("google_service_account")["account_id"]),
        kill_threshold_bounds=_bounds(threshold_check["condition"], "kill_threshold"),
        trigger=unquote(trigger["name"]),
        trigger_event_type=unquote(criterion["value"]),
        budget_name=unquote(budget["display_name"]),
        budget_units=unquote(budget["amount"][0]["specified_amount"][0]["units"]),
        budget_thresholds=tuple(
            sorted(
                (
                    r["threshold_percent"],
                    unquote(r.get("spend_basis", BUDGET_DEFAULT_SPEND_BASIS)),
                )
                for r in budget["threshold_rules"]
            )
        ),
        budget_credits=unquote(budget_filter["credit_types_treatment"]),
        budget_period=unquote(budget_filter.get("calendar_period", BUDGET_DEFAULT_PERIOD)),
        kill_switch_roles=kill_switch_roles(),
    )
