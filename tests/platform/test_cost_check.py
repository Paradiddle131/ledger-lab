"""`make cost-check` calls no GCP tool without a lab project; with one it reads the month's cost
and every applied fence, and fails on a cost above zero, a loose fence or a permission too many.

No real GCP tool runs here. Through `make`, stub `gcloud`, `bq`, `gsutil` and `curl` go first on
`PATH` and record their arguments; everything else drives `check` with a fake GCP that answers
each read the way the live APIs do, healthy unless a test changes one answer.
"""

import copy
import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest
from cost_check import (
    BILLING_PERMISSIONS,
    PROJECT_PERMISSIONS,
    bq_command,
    check,
    month_to_date_sql,
)
from fences import declared

REPO = Path(__file__).resolve().parents[2]
TOOLS = ("gcloud", "bq", "gsutil", "curl")
PROJECT = "the-lab"
ACCOUNT = "billingAccounts/the-account"
ENV = {"LEDGER_LAB_GCP_PROJECT": PROJECT, "LEDGER_LAB_BILLING_EXPORT": "billing.export_v1"}
D = declared()
P = f"projects/{PROJECT}"
KILL_SWITCH_ACCOUNT = f"{D.kill_switch_account}@{PROJECT}.iam.gserviceaccount.com"
SUBSCRIPTION = f"{P}/subscriptions/eventarc-{D.region}-{D.trigger}-sub-1"
DENIED = {"error": {"code": 403, "message": "denied", "status": "PERMISSION_DENIED"}}
NOT_FOUND = {"error": {"code": 404, "message": "gone", "status": "NOT_FOUND"}}


def healthy() -> dict[str, Any]:
    """Each read's answer when every fence stands as declared; the budget is not readable."""
    limit = D.query_cap_limit.replace("/", "%2F")
    return {
        "billing": {"billingAccountName": ACCOUNT, "billingEnabled": True},
        "quota": {
            "consumerQuotaLimits": [
                {
                    "name": f"projects/1/services/x/consumerQuotaMetrics/y/limits/{limit}",
                    "quotaBuckets": [
                        {
                            "effectiveLimit": str(D.query_cap_mib),
                            "defaultLimit": "209715200",
                            "consumerOverride": {"overrideValue": str(D.query_cap_mib)},
                        }
                    ],
                },
                {
                    "name": "projects/1/services/x/consumerQuotaMetrics/y/limits/%2Fd%2Fuser",
                    "quotaBuckets": [{"effectiveLimit": "9223372036854775807"}],
                },
            ]
        },
        "topic": {"name": f"{P}/topics/{D.topic}"},
        "workflow": {
            "name": f"{P}/locations/{D.region}/workflows/{D.workflow}",
            "state": "ACTIVE",
            "serviceAccount": f"{P}/serviceAccounts/{KILL_SWITCH_ACCOUNT}",
            "sourceContents": D.workflow_source,
            "userEnvVars": {"KILL_THRESHOLD": "1"},
        },
        "trigger": {
            "eventFilters": [{"attribute": "type", "value": D.trigger_event_type}],
            "serviceAccount": KILL_SWITCH_ACCOUNT,
            "destination": {"workflow": f"{P}/locations/{D.region}/workflows/{D.workflow}"},
            "transport": {
                "pubsub": {"topic": f"{P}/topics/{D.topic}", "subscription": SUBSCRIPTION}
            },
        },
        "subscription": {"name": SUBSCRIPTION, "topic": f"{P}/topics/{D.topic}"},
        "budgets": DENIED,
        "project": {"projectId": PROJECT, "projectNumber": "123"},
        "project permissions": {},
        "billing permissions": {},
    }


def readable_budget() -> dict[str, Any]:
    return {
        "budgets": [
            {"displayName": "someone else's budget"},
            {
                "displayName": D.budget_name,
                "budgetFilter": {
                    "projects": ["projects/123"],
                    "creditTypesTreatment": D.budget_credits,
                },
                "amount": {"specifiedAmount": {"currencyCode": "USD", "units": D.budget_units}},
                "thresholdRules": [{"thresholdPercent": t} for t in D.budget_thresholds],
                "notificationsRule": {"pubsubTopic": f"{P}/topics/{D.topic}"},
            },
        ]
    }


def route(command: Sequence[str]) -> str:
    url = command[-1]
    for fragment, key in (
        (":testIamPermissions", "permissions"),
        ("/billingInfo", "billing"),
        ("/consumerQuotaMetrics/", "quota"),
        ("/subscriptions/", "subscription"),
        ("/topics/", "topic"),
        ("/workflows/", "workflow"),
        ("/triggers/", "trigger"),
        ("/budgets", "budgets"),
        (f"cloudresourcemanager.googleapis.com/v1/{P}", "project"),
    ):
        if fragment in url:
            if key == "permissions":
                return "billing permissions" if "billingAccounts/" in url else "project permissions"
            return key
    raise AssertionError(f"unexpected read: {url}")


class FakeGCP:
    """Answers the check's reads like the live APIs; records every command."""

    def __init__(
        self,
        cost: str | None = "0",
        dry_run_bytes: int = 448448,
        changes: dict[str, Any] | None = None,
    ) -> None:
        self.cost = cost
        self.dry_run_bytes = dry_run_bytes
        self.answers = healthy() | (changes or {})
        self.calls: list[list[str]] = []

    def __call__(self, command: Sequence[str]) -> str:
        self.calls.append(list(command))
        if command[0] == "bq":
            if "--dry_run" in command:
                return json.dumps({"statistics": {"totalBytesProcessed": str(self.dry_run_bytes)}})
            return json.dumps([] if self.cost is None else [{"cost": self.cost}])
        assert command[0] == "curl", command
        answer = self.answers[route(command)]
        return answer if isinstance(answer, str) else json.dumps(answer)


def changed(key: str, edit: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    answer = copy.deepcopy(healthy()[key])
    edit(answer)
    return answer


def line_for(out: str, fence: str) -> str:
    """The one line judging a fence."""
    fence = fence.rstrip(":")
    pattern = re.compile(rf"^cost-check: (ok|FAIL|NOT CHECKED) {re.escape(fence)}: ")
    (line,) = [line for line in out.splitlines() if pattern.match(line)]
    return line


# Through make: no lab project means no GCP tool at all.


def stubs(directory: Path) -> Path:
    """Write the stub tools; return the call log they append to."""
    log = directory / "calls.log"
    for tool in TOOLS:
        stub = directory / tool
        stub.write_text(f'#!/bin/sh\nprintf "%s %s\\n" {tool} "$*" >> "{log}"\necho "[]"\n')
        stub.chmod(0o755)
    return log


def cost_check(tmp_path: Path, **env: str) -> tuple[int, str, str]:
    log = stubs(tmp_path)
    base = {k: v for k, v in os.environ.items() if not k.startswith("LEDGER_LAB_")}
    base["PATH"] = f"{tmp_path}{os.pathsep}{base['PATH']}"
    make = shutil.which("make")
    assert make is not None
    result = subprocess.run(
        [make, "-s", "cost-check"],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO,
        env=base | env,
    )
    calls = log.read_text() if log.exists() else ""
    return result.returncode, result.stdout + result.stderr, calls


def test_without_a_lab_project_it_reports_zero_and_calls_no_gcp_tool(tmp_path: Path) -> None:
    code, out, calls = cost_check(tmp_path, GCP_PROJECT="another-lab")
    assert code == 0, out
    assert "no GCP project" in out
    assert "$0.00" in out
    assert calls == ""


def test_a_lab_project_without_an_export_table_fails_before_any_call(tmp_path: Path) -> None:
    code, out, calls = cost_check(tmp_path, LEDGER_LAB_GCP_PROJECT=PROJECT)
    assert code != 0
    assert "LEDGER_LAB_BILLING_EXPORT" in out
    assert calls == ""


# The proving test of the lab-spend-fenced control.


def test_the_applied_fences_match_their_definitions() -> None:
    gcp = FakeGCP()
    code, out = check(ENV, gcp)
    assert code == 0, out
    assert "month-to-date cost is $0.00" in out
    for fence in (
        "billing link",
        "daily query cap",
        "budget topic",
        "kill switch workflow",
        "kill switch trigger",
        "agent permissions",
    ):
        assert line_for(out, fence).startswith("cost-check: ok "), out
    assert line_for(out, "budget:").startswith("cost-check: NOT CHECKED "), out
    assert out.splitlines()[-1].startswith("cost-check: passed"), out
    assert all(c[0] in ("bq", "curl") for c in gcp.calls)


def test_every_call_is_a_read_on_the_lab_project_only() -> None:
    gcp = FakeGCP()
    check(ENV | {"GCP_PROJECT": "another-lab"}, gcp)
    assert not any("another-lab" in " ".join(c) for c in gcp.calls)
    for command in gcp.calls:
        if command[0] == "curl":
            method = command[command.index("-X") + 1] if "-X" in command else "GET"
            assert method == "GET" or command[-1].endswith(":testIamPermissions"), command


def test_the_cost_query_is_dry_run_first_and_capped() -> None:
    gcp = FakeGCP()
    check(ENV | {"LEDGER_LAB_BQ_MAX_BYTES": "5000000"}, gcp)
    queries = [c for c in gcp.calls if c[0] == "bq"]
    assert len(queries) == 2
    assert "--dry_run" in queries[0]
    assert "--dry_run" not in queries[1]
    for query in queries:
        assert f"--project_id={PROJECT}" in query
        assert "--maximum_bytes_billed=5000000" in query
    assert gcp.calls.index(queries[0]) == 0


def test_a_query_the_dry_run_puts_above_the_cap_is_not_run() -> None:
    gcp = FakeGCP(dry_run_bytes=2_000_000_000)
    code, out = check(ENV, gcp)
    assert code != 0
    assert "above the cap" in out
    assert [c for c in gcp.calls if c[0] == "bq" and "--dry_run" not in c] == []


def test_any_cost_above_zero_fails() -> None:
    code, out = check(ENV, FakeGCP(cost="0.01"))
    assert code != 0
    assert "$0.01" in out
    assert "above $0.00" in out
    assert out.splitlines()[-1].startswith("cost-check: failed")


def test_credits_are_netted_and_an_empty_month_is_zero() -> None:
    assert check(ENV, FakeGCP(cost="-0.0"))[0] == 0
    code, out = check(ENV, FakeGCP(cost=None))
    assert code == 0, out
    assert "month-to-date cost is $0.00" in out


LOOSE: list[tuple[str, str, Any]] = [
    ("billing link", "billing", {"billingAccountName": "", "billingEnabled": False}),
    (
        "daily query cap",
        "quota",
        changed(
            "quota",
            lambda a: a["consumerQuotaLimits"][0]["quotaBuckets"][0].pop("consumerOverride"),
        ),
    ),
    (
        "daily query cap",
        "quota",
        changed(
            "quota",
            lambda a: a["consumerQuotaLimits"][0]["quotaBuckets"][0].update(
                effectiveLimit="209715200", consumerOverride={"overrideValue": "209715200"}
            ),
        ),
    ),
    ("budget topic", "topic", NOT_FOUND),
    ("kill switch workflow", "workflow", NOT_FOUND),
    ("kill switch workflow", "workflow", changed("workflow", lambda a: a.update(state="FAILED"))),
    (
        "kill switch workflow",
        "workflow",
        changed("workflow", lambda a: a.update(sourceContents="main:\n  steps: []\n")),
    ),
    (
        "kill switch workflow",
        "workflow",
        changed("workflow", lambda a: a.update(userEnvVars={"KILL_THRESHOLD": "500"})),
    ),
    (
        "kill switch workflow",
        "workflow",
        changed("workflow", lambda a: a.update(serviceAccount=f"{P}/serviceAccounts/other@x")),
    ),
    ("kill switch trigger", "trigger", NOT_FOUND),
    (
        "kill switch trigger",
        "trigger",
        changed("trigger", lambda a: a["transport"]["pubsub"].update(topic=f"{P}/topics/other")),
    ),
    (
        "kill switch trigger",
        "trigger",
        changed("trigger", lambda a: a["destination"].update(workflow="elsewhere")),
    ),
    ("kill switch trigger", "subscription", NOT_FOUND),
    ("daily query cap", "quota", "<html>blocked</html>"),
]


@pytest.mark.parametrize(("fence", "read", "answer"), LOOSE)
def test_each_loose_fence_fails_with_a_line_naming_it(fence: str, read: str, answer: Any) -> None:
    code, out = check(ENV, FakeGCP(changes={read: answer}))
    assert code != 0, out
    assert line_for(out, fence).startswith("cost-check: FAIL "), out
    assert out.splitlines()[-1].startswith("cost-check: failed"), out


@pytest.mark.parametrize(
    ("read", "permission"),
    [
        ("project permissions", "resourcemanager.projects.setIamPolicy"),
        ("project permissions", "serviceusage.services.enable"),
        ("project permissions", "serviceusage.quotas.update"),
        ("project permissions", "workflows.workflows.update"),
        ("project permissions", "eventarc.triggers.delete"),
        ("project permissions", "resourcemanager.projects.deleteBillingAssignment"),
        ("billing permissions", "billing.resourceAssociations.create"),
        ("billing permissions", "billing.budgets.update"),
    ],
)
def test_a_permission_the_agent_should_not_hold_fails(read: str, permission: str) -> None:
    assert permission in (
        PROJECT_PERMISSIONS if read.startswith("project") else BILLING_PERMISSIONS
    )
    code, out = check(ENV, FakeGCP(changes={read: {"permissions": [permission]}}))
    assert code != 0
    line = line_for(out, "agent permissions")
    assert line.startswith("cost-check: FAIL ")
    assert permission in line


def test_the_permissions_asked_for_are_only_tested() -> None:
    gcp = FakeGCP()
    check(ENV, gcp)
    asked = [c for c in gcp.calls if c[-1].endswith(":testIamPermissions")]
    assert len(asked) == 2
    sent = {p for c in asked for p in json.loads(c[c.index("-d") + 1])["permissions"]}
    assert sent == set(PROJECT_PERMISSIONS) | set(BILLING_PERMISSIONS)


def test_an_unreadable_budget_is_reported_and_neither_passes_nor_fails() -> None:
    code, out = check(ENV, FakeGCP())
    line = line_for(out, "budget:")
    assert line.startswith("cost-check: NOT CHECKED ")
    assert "not readable by this account" in line
    assert " ok" not in line
    assert code == 0
    assert "1 not checked" in out.splitlines()[-1]
    failing, failed = check(ENV, FakeGCP(cost="0.01"))
    assert failing != 0
    assert line_for(failed, "budget:").startswith("cost-check: NOT CHECKED ")


def test_a_readable_budget_is_compared_with_its_definition() -> None:
    code, out = check(ENV, FakeGCP(changes={"budgets": readable_budget()}))
    assert code == 0, out
    assert line_for(out, "budget:").startswith("cost-check: ok ")
    loose = readable_budget()
    loose["budgets"][1]["amount"]["specifiedAmount"]["units"] = "100"
    loose["budgets"][1]["thresholdRules"] = [{"thresholdPercent": 0.5}]
    code, out = check(ENV, FakeGCP(changes={"budgets": loose}))
    assert code != 0
    assert line_for(out, "budget:").startswith("cost-check: FAIL ")
    code, out = check(ENV, FakeGCP(changes={"budgets": {"budgets": []}}))
    assert code != 0
    assert line_for(out, "budget:").startswith("cost-check: FAIL ")


def test_the_output_names_fences_by_role_never_by_identifier() -> None:
    for gcp in (FakeGCP(), FakeGCP(cost="1", changes={"workflow": NOT_FOUND})):
        _, out = check(ENV, gcp)
        assert PROJECT not in out
        assert "the-account" not in out
        assert "iam.gserviceaccount.com" not in out


def test_a_failing_tool_fails_the_check_without_leaking_its_identifiers() -> None:
    def broken(command: Sequence[str]) -> str:
        if command[0] == "bq":
            raise subprocess.CalledProcessError(2, command, "", f"Not found: {PROJECT}:billing")
        return FakeGCP()(command)

    code, out = check(ENV, broken)
    assert code != 0
    assert "cost query failed" in out
    assert PROJECT not in out


@pytest.mark.parametrize("export", ["billing", "a.b.c", "b.t`; DROP", ""])
def test_an_export_name_that_is_not_dataset_table_is_refused(export: str) -> None:
    with pytest.raises(ValueError, match="dataset.table"):
        month_to_date_sql(PROJECT, export)


def test_a_project_id_that_is_not_one_is_refused() -> None:
    with pytest.raises(ValueError, match="project id"):
        month_to_date_sql("the-lab`; x", "billing.export_v1")


def test_the_query_is_capped_and_scoped_to_the_project() -> None:
    command = bq_command(PROJECT, "billing.export_v1", "1000")
    assert "--maximum_bytes_billed=1000" in command
    assert f"--parameter=project:STRING:{PROJECT}" in command
    assert f"`{PROJECT}.billing.export_v1`" in command[-1]
