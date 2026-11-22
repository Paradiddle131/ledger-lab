"""`make cost-check` calls no GCP tool without a lab project or in another lab's session; with a lab
project in its own environment it reads the month's cost
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
import signal
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest
from cost_check import (
    BILLING_PERMISSIONS,
    KILL_SWITCH_ACCOUNT_PERMISSIONS,
    MAX_BYTES_BILLED,
    PROJECT_PERMISSIONS,
    RATE_LIMIT_PAUSES,
    READS,
    bq_command,
    check,
    month_to_date_sql,
    read_permission,
)
from fences import declared

REPO = Path(__file__).resolve().parents[2]
TOOLS = ("gcloud", "bq", "gsutil", "curl")
PROJECT = "the-lab"
ACCOUNT = "billingAccounts/the-account"
# The lab's own cloud environment: gcloud is handed its token, and the proxy authenticates curl.
ENV = {
    "LEDGER_LAB_GCP_PROJECT": PROJECT,
    "LEDGER_LAB_BILLING_EXPORT": "billing.export_v1",
    "CLOUDSDK_AUTH_ACCESS_TOKEN": "proxy-injected",
}
# A machine off the proxy: gcloud's own login holds the credential.
OFF_PROXY = {k: v for k, v in ENV.items() if k != "CLOUDSDK_AUTH_ACCESS_TOKEN"}
LOGIN_CREDENTIAL = "the-logins-fake-credential"
D = declared()
P = f"projects/{PROJECT}"
KILL_SWITCH_ACCOUNT = f"{D.kill_switch_account}@{PROJECT}.iam.gserviceaccount.com"
SUBSCRIPTION = f"{P}/subscriptions/eventarc-{D.region}-{D.trigger}-sub-1"
DENIED = {"error": {"code": 403, "message": "denied", "status": "PERMISSION_DENIED"}}
SERVICE_DISABLED = {
    "error": {
        "code": 403,
        "message": "a message naming an identifier",
        "status": "PERMISSION_DENIED",
        "details": [
            {"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "SERVICE_DISABLED"}
        ],
    }
}
HEALTHY_ROW = {
    "cost": "0",
    "month_rows": "12",
    "all_rows": "40",
    "newest_export": "2026-11-13T10:00Z",
}
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
        "kill switch account": {
            "name": f"{P}/serviceAccounts/{KILL_SWITCH_ACCOUNT}",
            "email": KILL_SWITCH_ACCOUNT,
        },
        "iam policy": {
            "version": 1,
            "bindings": [
                {"role": "roles/owner", "members": ["user:someone@example.com"]},
                *(
                    {"role": role, "members": [f"serviceAccount:{KILL_SWITCH_ACCOUNT}"]}
                    for role in D.kill_switch_roles
                ),
            ],
        },
        "budgets": DENIED,
        "project": {"projectId": PROJECT, "projectNumber": "123"},
        "project permissions": {},
        "billing permissions": {},
        "account permissions": {},
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
                    "calendarPeriod": D.budget_period,
                    "subaccounts": [],
                },
                "amount": {"specifiedAmount": {"currencyCode": "USD", "units": D.budget_units}},
                "thresholdRules": [
                    {"thresholdPercent": t, "spendBasis": b} for t, b in D.budget_thresholds
                ],
                "notificationsRule": {"pubsubTopic": f"{P}/topics/{D.topic}"},
            },
        ]
    }


def route(command: Sequence[str]) -> str:
    url = command[-1]
    if url.endswith(":testIamPermissions"):
        if url.startswith("https://iam.googleapis.com/"):
            return "account permissions"
        return "billing permissions" if "billingAccounts/" in url else "project permissions"
    for fragment, key in (
        (":getIamPolicy", "iam policy"),
        ("https://iam.googleapis.com/v1/", "kill switch account"),
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
            return key
    raise AssertionError(f"unexpected read: {url}")


class FakeGCP:
    """Answers the check's reads like the live APIs; records every command."""

    def __init__(
        self,
        cost: str = "0",
        dry_run_bytes: int = 448448,
        changes: dict[str, Any] | None = None,
        rows: Any = None,
        token: str | Exception = LOGIN_CREDENTIAL,
    ) -> None:
        self.rows = [HEALTHY_ROW | {"cost": cost}] if rows is None else rows
        self.token = token
        self.headers: list[str] = []
        self.dry_run_bytes = dry_run_bytes
        self.answers = healthy() | (changes or {})
        self.calls: list[list[str]] = []

    def __call__(self, command: Sequence[str]) -> str:
        self.calls.append(list(command))
        if command[0] == "bq":
            if "--dry_run" in command:
                return json.dumps({"statistics": {"totalBytesProcessed": str(self.dry_run_bytes)}})
            return self.rows if isinstance(self.rows, str) else json.dumps(self.rows)
        if list(command) == ["gcloud", "auth", "print-access-token"]:
            if isinstance(self.token, Exception):
                raise self.token
            return self.token + "\n"
        assert command[0] == "curl", command
        for flag, value in zip(command, command[1:], strict=False):
            if flag == "-H" and value.startswith("@"):
                self.headers.append(Path(value[1:]).read_text())
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
    base = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("LEDGER_LAB_")
        and k not in ("GCP_PROJECT", "CLOUDSDK_AUTH_ACCESS_TOKEN")
    }
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
    code, out, calls = cost_check(tmp_path)
    assert code == 0, out
    assert "no GCP project" in out
    assert "$0.00" in out
    assert calls == ""


@pytest.mark.parametrize(
    "lab", [{}, {"LEDGER_LAB_GCP_PROJECT": PROJECT}, ENV], ids=["no-lab", "lab", "lab-and-export"]
)
def test_in_another_labs_session_it_fails_unchecked_and_calls_no_gcp_tool(
    tmp_path: Path, lab: dict[str, str]
) -> None:
    """The session's credential may be the other lab's, so nothing is read, and nothing read
    shows the lab's cost is $0.00."""
    code, out, calls = cost_check(tmp_path, GCP_PROJECT="another-lab", **lab)
    assert code != 0, out
    assert out.startswith("cost-check: NOT CHECKED GCP_PROJECT is set"), out
    assert "$0.00" not in out
    assert "another-lab" not in out and PROJECT not in out
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
        "kill switch account",
        "kill switch roles",
        "agent permissions",
    ):
        assert line_for(out, fence).startswith("cost-check: ok "), out
    assert line_for(out, "budget:").startswith("cost-check: NOT CHECKED "), out
    assert out.splitlines()[-1].startswith("cost-check: passed"), out
    assert all(c[0] in ("bq", "curl") for c in gcp.calls)


def test_every_read_is_a_listed_row_and_every_row_is_read() -> None:
    """Each URL a healthy run reads matches one row of READS, and each row is read by a healthy
    run or by a run whose budget is readable, so READS lists what the check reads and no more."""
    used: set[str] = set()
    for gcp in (FakeGCP(), FakeGCP(changes={"budgets": readable_budget()})):
        code, out = check(ENV, gcp)
        assert code == 0, out
        for url in [c[-1] for c in gcp.calls if c[0] == "curl"]:
            read_permission(url)
            used |= {shape for shape, _, _ in READS if re.fullmatch(shape, url)}
    assert used == {shape for shape, _, _ in READS}


def test_read_permission_refuses_a_read_no_row_lists_or_two_rows_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError, match="0 rows match"):
        read_permission(f"https://monitoring.googleapis.com/v3/projects/{PROJECT}/alertPolicies")
    import cost_check

    doubled = (*cost_check.READS, cost_check.READS[0])
    monkeypatch.setattr(cost_check, "READS", doubled)
    with pytest.raises(ValueError, match="2 rows match"):
        read_permission(f"https://cloudbilling.googleapis.com/v1/projects/{PROJECT}/billingInfo")


def test_a_read_no_row_lists_is_refused_before_it_is_sent() -> None:
    import cost_check

    sent: list[list[str]] = []
    fences = cost_check.Fences(PROJECT, lambda c: (sent.append(list(c)), "{}")[1], declared())
    with pytest.raises(ValueError, match="does not list in READS"):
        fences.read(f"https://monitoring.googleapis.com/v3/projects/{PROJECT}/alertPolicies")
    assert sent == []


def test_a_project_id_no_row_matches_fails_every_fence_instead_of_crashing() -> None:
    """Every read is refused before it is sent: each fence fails, saying so, and the budget, whose
    account comes from the unread billing link, is not checked; nothing is sent."""
    gcp = FakeGCP()
    code, out = check(ENV | {"LEDGER_LAB_GCP_PROJECT": "example.com:the-lab"}, gcp)
    assert code == 1, out
    budget = line_for(out, "budget")
    assert budget.startswith("cost-check: NOT CHECKED budget: the billing link was not read"), out
    others = [
        line_for(out, fence)
        for fence in (
            "billing link",
            "daily query cap",
            "budget topic",
            "kill switch workflow",
            "kill switch trigger",
            "kill switch account",
            "kill switch roles",
            "agent permissions",
        )
    ]
    assert all(line.startswith("cost-check: FAIL ") for line in others), out
    assert all("a read that was refused" in line for line in others), out
    assert [c for c in gcp.calls if c[0] == "curl"] == []


def test_another_labs_session_makes_no_call_even_with_the_lab_set() -> None:
    gcp = FakeGCP()
    code, out = check(ENV | {"GCP_PROJECT": "another-lab"}, gcp)
    assert code == 1, out
    assert gcp.calls == []


def test_every_call_is_a_read_on_the_lab_project_only() -> None:
    gcp = FakeGCP()
    code, out = check(ENV, gcp)
    assert code == 0, out
    assert gcp.calls, "the check made no call to judge"
    assert all(f"--project_id={PROJECT}" in c for c in gcp.calls if c[0] == "bq"), gcp.calls
    for command in (c for c in gcp.calls if c[0] == "curl"):
        url = command[-1]
        billed = [v for f, v in zip(command, command[1:], strict=False) if f == "-H"]
        billed = [v for v in billed if v.lower().startswith("x-goog-user-project:")]
        # A project's resource names the lab project as its whole path segment, and is billed to
        # the lab's project or to none; any other read is on the lab's billing account and is
        # billed to the lab's project alone.
        on_project = re.search(r"/projects/([^/:]+)(?:[/:]|$)", url)
        if on_project:
            assert on_project.group(1) == PROJECT, command
            assert billed in ([], [f"x-goog-user-project: {PROJECT}"]), command
        else:
            assert re.search(rf"/{re.escape(ACCOUNT)}(?:[/:]|$)", url), command
            assert billed == [f"x-goog-user-project: {PROJECT}"], command
        # The billing APIs, read with gcloud's login and no quota project, may spend the quota of
        # gcloud's shared project: each billing read names the lab's.
        if re.match(r"https://(cloudbilling|billingbudgets)\.googleapis\.com/", url):
            assert billed == [f"x-goog-user-project: {PROJECT}"], command
    for command in gcp.calls:
        if command[0] == "curl":
            method = command[command.index("-X") + 1] if "-X" in command else "GET"
            read_only_post = command[-1].endswith((":testIamPermissions", ":getIamPolicy"))
            assert method == "GET" or read_only_post, command


def test_the_cost_query_is_dry_run_first_and_capped() -> None:
    gcp = FakeGCP()
    check(ENV, gcp)
    queries = [c for c in gcp.calls if c[0] == "bq"]
    assert len(queries) == 2
    assert "--dry_run" in queries[0]
    assert "--dry_run" not in queries[1]
    for query in queries:
        assert f"--project_id={PROJECT}" in query
        assert f"--maximum_bytes_billed={MAX_BYTES_BILLED}" in query
    assert gcp.calls.index(queries[0]) == 0


def test_a_query_the_dry_run_puts_above_the_cap_is_not_run() -> None:
    gcp = FakeGCP(dry_run_bytes=MAX_BYTES_BILLED + 1)
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


def test_credits_are_netted_and_a_month_without_rows_yet_is_zero() -> None:
    code, out = check(ENV, FakeGCP(cost="-0.0"))
    assert code == 0, out
    code, out = check(ENV, FakeGCP(rows=[HEALTHY_ROW | {"month_rows": "0"}]))
    assert code == 0, out
    line = out.splitlines()[0]
    assert "month-to-date cost is $0.00" in line
    assert "no rows yet this month" in line
    assert "newest export 2026-11-13T10:00Z" in line


def test_the_cost_line_shows_how_fresh_the_export_is() -> None:
    _, out = check(ENV, FakeGCP())
    line = out.splitlines()[0]
    assert line.startswith("cost-check: ok month-to-date cost is $0.00")
    assert "12 rows this month" in line
    assert "newest export 2026-11-13T10:00Z" in line


def test_an_export_with_no_rows_for_the_project_cannot_show_zero() -> None:
    rows = [{"cost": "0", "month_rows": "0", "all_rows": "0", "newest_export": None}]
    code, out = check(ENV, FakeGCP(rows=rows))
    assert code != 0
    line = out.splitlines()[0]
    assert line.startswith("cost-check: FAIL cost:")
    assert "no rows" in line
    assert "$0.00" not in line


@pytest.mark.parametrize(
    "rows",
    [
        "",
        "[]",
        "null",
        "not json",
        [{"cost": None, "month_rows": "1", "all_rows": "1", "newest_export": "x"}],
        [{"month_rows": "1", "all_rows": "1", "newest_export": "x"}],
        [{"cost": "zero", "month_rows": "1", "all_rows": "1", "newest_export": "x"}],
        [{"cost": "0", "month_rows": None, "all_rows": "1", "newest_export": "x"}],
        [HEALTHY_ROW, HEALTHY_ROW],
        {"cost": "0"},
    ],
)
def test_a_cost_answer_that_is_not_one_row_with_a_cost_fails(rows: Any) -> None:
    code, out = check(ENV, FakeGCP(rows=rows))
    assert code != 0, out
    assert out.splitlines()[0].startswith("cost-check: FAIL cost:"), out
    assert "$0.00" not in out.splitlines()[0]


def test_a_cost_rounding_to_zero_cents_still_fails_and_shows_its_value() -> None:
    code, out = check(ENV, FakeGCP(cost="0.0001"))
    assert code != 0
    assert "0.0001" in out.splitlines()[0]


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
    (
        "daily query cap",
        "quota",
        changed(
            "quota",
            lambda a: a["consumerQuotaLimits"][0]["quotaBuckets"][0].pop("effectiveLimit"),
        ),
    ),
    ("budget topic", "topic", {}),
    ("budget topic", "topic", {"name": f"{P}/topics/other"}),
    ("kill switch trigger", "subscription", {}),
    ("kill switch account", "kill switch account", NOT_FOUND),
    (
        "kill switch account",
        "kill switch account",
        changed("kill switch account", lambda a: a.update(disabled=True)),
    ),
    ("kill switch account", "kill switch account", {"name": "x"}),
    ("kill switch roles", "iam policy", changed("iam policy", lambda a: a["bindings"].pop())),
    (
        "kill switch roles",
        "iam policy",
        changed(
            "iam policy",
            lambda a: a["bindings"][-1].update(condition={"expression": "false", "title": "off"}),
        ),
    ),
    ("kill switch roles", "iam policy", {"bindings": "none"}),
]


@pytest.mark.parametrize(("fence", "read", "answer"), LOOSE)
def test_each_loose_fence_fails_with_a_line_naming_it(fence: str, read: str, answer: Any) -> None:
    code, out = check(ENV, FakeGCP(changes={read: answer}))
    assert code != 0, out
    assert line_for(out, fence).startswith("cost-check: FAIL "), out
    assert out.splitlines()[-1].startswith("cost-check: failed"), out


PERMISSIONS = [
    *(("project permissions", p) for p in PROJECT_PERMISSIONS),
    *(("billing permissions", p) for p in BILLING_PERMISSIONS),
    *(("account permissions", p) for p in KILL_SWITCH_ACCOUNT_PERMISSIONS),
]


@pytest.mark.parametrize(("read", "permission"), PERMISSIONS)
def test_a_permission_the_agent_should_not_hold_fails(read: str, permission: str) -> None:
    code, out = check(ENV, FakeGCP(changes={read: {"permissions": [permission]}}))
    assert code != 0
    line = line_for(out, "agent permissions")
    assert line.startswith("cost-check: FAIL ")
    assert permission in line


def test_the_fence_moving_permissions_include_every_known_route() -> None:
    assert {
        "cloudquotas.quotas.update",
        "iam.serviceAccounts.disable",
        "pubsub.topics.setIamPolicy",
        "pubsub.topics.detachSubscription",
        "pubsub.subscriptions.update",
    } <= set(PROJECT_PERMISSIONS)
    assert "billing.accounts.setIamPolicy" in BILLING_PERMISSIONS
    assert {
        "iam.serviceAccounts.getAccessToken",
        "iam.serviceAccountKeys.create",
        "iam.serviceAccounts.actAs",
    } <= set(KILL_SWITCH_ACCOUNT_PERMISSIONS)


def test_each_permission_is_only_tested_on_its_own_resource() -> None:
    gcp = FakeGCP()
    check(ENV, gcp)
    asked = {
        c[-1]: set(json.loads(c[c.index("-d") + 1])["permissions"])
        for c in gcp.calls
        if c[-1].endswith(":testIamPermissions")
    }
    assert asked == {
        f"https://cloudresourcemanager.googleapis.com/v1/{P}:testIamPermissions": set(
            PROJECT_PERMISSIONS
        ),
        f"https://cloudbilling.googleapis.com/v1/{ACCOUNT}:testIamPermissions": set(
            BILLING_PERMISSIONS
        ),
        f"https://iam.googleapis.com/v1/{P}/serviceAccounts/{KILL_SWITCH_ACCOUNT}"
        ":testIamPermissions": set(KILL_SWITCH_ACCOUNT_PERMISSIONS),
    }


def test_an_unreadable_budget_is_reported_and_neither_passes_nor_fails() -> None:
    code, out = check(ENV, FakeGCP())
    line = line_for(out, "budget:")
    assert line.startswith("cost-check: NOT CHECKED ")
    assert "not readable by this account" in line
    assert " ok" not in line
    assert code == 0
    assert "1 not checked (budget)" in out.splitlines()[-1]
    failing, failed = check(ENV, FakeGCP(cost="0.01"))
    assert failing != 0
    assert line_for(failed, "budget:").startswith("cost-check: NOT CHECKED ")


def test_a_budget_refused_for_another_reason_names_it() -> None:
    code, out = check(ENV, FakeGCP(changes={"budgets": SERVICE_DISABLED}))
    line = line_for(out, "budget")
    assert line.startswith("cost-check: NOT CHECKED ")
    assert "PERMISSION_DENIED" in line
    assert "SERVICE_DISABLED" in line
    assert "not readable by this account" not in line
    assert "identifier" not in out
    assert code == 0


def test_an_unreadable_iam_policy_leaves_the_roles_not_checked() -> None:
    code, out = check(ENV, FakeGCP(changes={"iam policy": DENIED}))
    line = line_for(out, "kill switch roles")
    assert line.startswith("cost-check: NOT CHECKED ")
    assert "not readable by this account" in line
    assert code == 0
    assert "2 not checked (kill switch roles, budget)" in out.splitlines()[-1]


def narrowed(edit: Callable[[dict[str, Any]], Any]) -> dict[str, Any]:
    answer = readable_budget()
    edit(answer["budgets"][1])
    return answer


@pytest.mark.parametrize(
    "budget",
    [
        narrowed(lambda b: b["budgetFilter"].update(services=["services/bigquery"])),
        narrowed(lambda b: b["budgetFilter"].update(labels={"team": {"values": ["x"]}})),
        narrowed(lambda b: b["budgetFilter"].update(calendarPeriod="QUARTER")),
        narrowed(
            lambda b: b["budgetFilter"].update(
                customPeriod={"startDate": {"year": 2026, "month": 1, "day": 1}}
            )
        ),
        narrowed(lambda b: b["thresholdRules"][0].update(spendBasis="FORECASTED_SPEND")),
        narrowed(lambda b: b["budgetFilter"].update(projects=["projects/123", "projects/9"])),
        narrowed(lambda b: b.update(amount={"lastPeriodAmount": {}})),
        narrowed(lambda b: b.update(thresholdRules=None)),
    ],
)
def test_a_readable_budget_narrower_than_its_definition_fails(budget: dict[str, Any]) -> None:
    code, out = check(ENV, FakeGCP(changes={"budgets": budget}))
    assert code != 0, out
    assert line_for(out, "budget").startswith("cost-check: FAIL "), out


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


FENCE_OF = {
    "billing": "billing link",
    "quota": "daily query cap",
    "topic": "budget topic",
    "workflow": "kill switch workflow",
    "trigger": "kill switch trigger",
    "subscription": "kill switch trigger",
    "kill switch account": "kill switch account",
    "iam policy": "kill switch roles",
    "project permissions": "agent permissions",
    "billing permissions": "agent permissions",
    "account permissions": "agent permissions",
}


@pytest.mark.parametrize("read", sorted(FENCE_OF))
@pytest.mark.parametrize("answer", ["", "null", "[]", '"text"', '{"error": "flat"}'])
def test_an_unreadable_answer_fails_its_own_fence(read: str, answer: str) -> None:
    code, out = check(ENV, FakeGCP(changes={read: answer}))
    assert code != 0, out
    assert line_for(out, FENCE_OF[read]).startswith("cost-check: FAIL "), out
    assert out.splitlines()[-1].startswith("cost-check: failed"), out


@pytest.mark.parametrize(
    ("read", "answer"),
    [
        ("quota", {"consumerQuotaLimits": "none"}),
        ("quota", {"consumerQuotaLimits": [{"name": None}]}),
        ("workflow", {"state": "ACTIVE", "userEnvVars": None}),
        ("trigger", {"transport": None}),
        ("project permissions", {"permissions": "everything"}),
        ("budgets", {"budgets": [None]}),
    ],
)
def test_an_answer_of_the_wrong_shape_fails_its_own_fence(read: str, answer: Any) -> None:
    fence = FENCE_OF.get(read, "budget")
    code, out = check(ENV, FakeGCP(changes={read: answer}))
    assert code != 0, out
    assert line_for(out, fence).startswith("cost-check: FAIL "), out


@pytest.mark.parametrize(
    "failure",
    [FileNotFoundError(2, "No such file"), subprocess.CalledProcessError(7, ["tool"], "", "")],
)
def test_a_missing_or_failing_tool_fails_every_line_without_a_traceback(
    failure: Exception,
) -> None:
    def broken(command: Sequence[str]) -> str:
        raise failure

    code, out = check(ENV, broken)
    assert code != 0
    assert out.splitlines()[0].startswith("cost-check: FAIL cost:")
    for fence in ("billing link", "daily query cap", "kill switch workflow", "agent permissions"):
        assert line_for(out, fence).startswith("cost-check: FAIL "), out
    assert "Traceback" not in out


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
    command = bq_command(PROJECT, "billing.export_v1")
    assert f"--maximum_bytes_billed={MAX_BYTES_BILLED}" in command
    assert f"--parameter=project:STRING:{PROJECT}" in command
    assert f"`{PROJECT}.billing.export_v1`" in command[-1]


# Off the proxy: curl sends the token of gcloud's own login.


def test_behind_the_proxy_curl_sends_no_credential_and_gcloud_is_not_asked() -> None:
    gcp = FakeGCP()
    code, out = check(ENV, gcp)
    assert code == 0, out
    assert [c for c in gcp.calls if c[0] == "gcloud"] == []
    assert not any("-H" in c and any(a.startswith("@") for a in c) for c in gcp.calls)


def test_off_the_proxy_every_read_sends_the_logins_token_from_a_file() -> None:
    gcp = FakeGCP()
    code, out = check(OFF_PROXY, gcp)
    assert code == 0, out
    curls = [c for c in gcp.calls if c[0] == "curl"]
    assert len(gcp.headers) == len(curls) > 0
    assert set(gcp.headers) == {f"Authorization: Bearer {LOGIN_CREDENTIAL}\n"}
    assert [c for c in gcp.calls if c[0] == "gcloud"] == [["gcloud", "auth", "print-access-token"]]
    assert not any(LOGIN_CREDENTIAL in " ".join(c) for c in gcp.calls)
    assert LOGIN_CREDENTIAL not in out


def test_the_header_file_is_readable_by_this_user_only_and_removed_after() -> None:
    seen: list[tuple[Path, int, int]] = []
    gcp = FakeGCP()

    def run(command: Sequence[str]) -> str:
        for value in command:
            if value.startswith("@"):
                header = Path(value[1:])
                modes = header.stat().st_mode & 0o777, header.parent.stat().st_mode & 0o777
                seen.append((header, *modes))
        return gcp(command)

    check(OFF_PROXY, run)
    assert seen
    assert {(file, folder) for _, file, folder in seen} == {(0o600, 0o700)}
    assert len({path for path, _, _ in seen}) == 1
    assert not any(path.exists() or path.parent.exists() for path, _, _ in seen)


@pytest.mark.parametrize(
    "token",
    [
        subprocess.CalledProcessError(1, ["gcloud"], "", "a message naming an account"),
        FileNotFoundError(2, "No such file"),
        "",
        "two words",
    ],
    ids=["gcloud-fails", "no-gcloud", "empty", "not-one-token"],
)
def test_off_the_proxy_without_a_login_token_every_read_fence_fails_and_nothing_is_sent(
    token: str | Exception,
) -> None:
    gcp = FakeGCP(token=token)
    code, out = check(OFF_PROXY, gcp)
    assert code != 0, out
    assert line_for(out, "billing link").startswith(
        "cost-check: FAIL billing link: could not be read (no login token"
    )
    for fence in ("daily query cap", "kill switch workflow", "kill switch account"):
        assert "no login token" in line_for(out, fence), out
    assert [c for c in gcp.calls if c[0] == "curl"] == []
    assert len([c for c in gcp.calls if c[0] == "gcloud"]) == 1
    assert "account" not in out.split("no login token")[1].split(")")[0]


PERMISSION_READS = {"project permissions", "billing permissions", "account permissions"}


def test_off_the_proxy_the_agents_permissions_are_not_checked_and_not_asked() -> None:
    """testIamPermissions answers for the caller, which off the proxy is the machine's login."""
    gcp = FakeGCP(changes={"project permissions": {"permissions": list(PROJECT_PERMISSIONS)}})
    code, out = check(OFF_PROXY, gcp)
    assert code == 0, out
    line = line_for(out, "agent permissions")
    assert line.startswith("cost-check: NOT CHECKED agent permissions: off the proxy"), out
    assert not [c for c in gcp.calls if c[-1].endswith(":testIamPermissions")]


@pytest.mark.parametrize("read", sorted(set(FENCE_OF) - PERMISSION_READS))
def test_off_the_proxy_a_read_the_login_is_refused_is_not_checked(read: str) -> None:
    code, out = check(OFF_PROXY, FakeGCP(changes={read: DENIED}))
    line = line_for(out, FENCE_OF[read])
    assert line.startswith(
        f"cost-check: NOT CHECKED {FENCE_OF[read]}: not readable by this account"
    ), out
    assert code == 0, out


@pytest.mark.parametrize("read", sorted(set(FENCE_OF) - {"iam policy"}))
def test_behind_the_proxy_a_refused_read_still_fails(read: str) -> None:
    """The agent account is granted every read but the roles' and the budget's (their tests
    above), so a refusal behind the proxy means a grant has gone."""
    code, out = check(ENV, FakeGCP(changes={read: DENIED}))
    assert code != 0, out
    assert line_for(out, FENCE_OF[read]).startswith("cost-check: FAIL "), out


@pytest.mark.parametrize("read", sorted(set(FENCE_OF) - PERMISSION_READS))
def test_off_the_proxy_an_unauthenticated_read_still_fails(read: str) -> None:
    unauthenticated = {"error": {"code": 401, "message": "m", "status": "UNAUTHENTICATED"}}
    code, out = check(OFF_PROXY, FakeGCP(changes={read: unauthenticated}))
    assert code != 0, out
    assert line_for(out, FENCE_OF[read]).startswith("cost-check: FAIL "), out


def test_off_the_proxy_curl_reads_no_curlrc_and_behind_it_the_command_is_unchanged() -> None:
    """`-q` must come first; a curlrc's trace or header-dump options would write the token out."""
    off, behind = FakeGCP(), FakeGCP()
    check(OFF_PROXY, off)
    check(ENV, behind)
    assert {c[1] for c in off.calls if c[0] == "curl"} == {"-q"}
    assert {tuple(c[1:4]) for c in behind.calls if c[0] == "curl"} == {("-sS", "--max-time", "60")}


@pytest.mark.parametrize("reason", ["SERVICE_DISABLED", "USER_PROJECT_DENIED", "ACCESS_DENIED"])
@pytest.mark.parametrize("read", sorted(set(FENCE_OF) - PERMISSION_READS - {"iam policy"}))
def test_off_the_proxy_a_refusal_that_is_not_a_missing_permission_still_fails(
    read: str, reason: str
) -> None:
    refused = copy.deepcopy(SERVICE_DISABLED)
    refused["error"]["details"][0]["reason"] = reason
    code, out = check(OFF_PROXY, FakeGCP(changes={read: refused}))
    assert code != 0, out
    assert line_for(out, FENCE_OF[read]).startswith("cost-check: FAIL "), out


def test_off_the_proxy_a_loose_trigger_fails_though_its_subscription_is_refused() -> None:
    loose = changed(
        "trigger",
        lambda t: t.update(serviceAccount=f"another-account@{PROJECT}.iam.gserviceaccount.com"),
    )
    code, out = check(OFF_PROXY, FakeGCP(changes={"trigger": loose, "subscription": DENIED}))
    assert code != 0, out
    assert line_for(out, "kill switch trigger").startswith("cost-check: FAIL "), out


@pytest.mark.parametrize("value", ["", "  "])
def test_an_empty_injected_token_is_not_the_proxy(value: str) -> None:
    gcp = FakeGCP()
    check(OFF_PROXY | {"CLOUDSDK_AUTH_ACCESS_TOKEN": value}, gcp)
    assert len(gcp.headers) == len([c for c in gcp.calls if c[0] == "curl"]) > 0


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGHUP])
def test_a_terminated_run_removes_the_header_file_and_restores_the_handlers(signum: int) -> None:
    before = signal.getsignal(signum)
    seen: list[Path] = []
    gcp = FakeGCP()

    def run(command: Sequence[str]) -> str:
        seen.extend(Path(v[1:]) for v in command if v.startswith("@"))
        if seen:
            os.kill(os.getpid(), signum)
        return gcp(command)

    with pytest.raises(SystemExit) as stopped:
        check(OFF_PROXY, run)
    assert stopped.value.code == 128 + signum
    assert seen
    assert not any(path.exists() or path.parent.exists() for path in seen)
    assert signal.getsignal(signum) == before


# A read the API refuses for a rate limit is sent again after a pause, a bounded number of times.

RATE_LIMITED = {
    "error": {
        "code": 429,
        "message": "quota exceeded",
        "status": "RESOURCE_EXHAUSTED",
        "details": [
            {
                "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                "reason": "RATE_LIMIT_EXCEEDED",
                "domain": "googleapis.com",
            }
        ],
    }
}


class Limited(FakeGCP):
    """The live answers, but each read of `key` refused for a rate limit `times` times first."""

    def __init__(self, key: str, times: int, refusal: dict[str, Any] = RATE_LIMITED) -> None:
        super().__init__()
        self.key, self.left, self.refusal = key, times, refusal
        self.tries = 0

    def __call__(self, command: Sequence[str]) -> str:
        if command[0] == "curl" and route(command) == self.key:
            self.tries += 1
            if self.left:
                self.left -= 1
                self.calls.append(list(command))
                return json.dumps(self.refusal)
        return super().__call__(command)


def test_a_rate_limited_billing_read_passes_on_the_next_attempt() -> None:
    gcp, paused = Limited("billing", 1), []
    code, out = check(ENV, gcp, paused.append)
    assert code == 0, out
    assert gcp.tries == 2
    assert paused == [RATE_LIMIT_PAUSES[0]]
    assert line_for(out, "billing link").startswith("cost-check: ok billing link:"), out


def test_a_billing_read_rate_limited_on_every_attempt_fails_the_check() -> None:
    """An unread billing link leaves the budget unchecked, so the retries end in a failure."""
    gcp, paused = Limited("billing", len(RATE_LIMIT_PAUSES) + 1), []
    code, out = check(ENV, gcp, paused.append)
    assert code == 1, out
    assert gcp.tries == len(RATE_LIMIT_PAUSES) + 1
    assert paused == list(RATE_LIMIT_PAUSES)
    assert line_for(out, "billing link") == (
        "cost-check: FAIL billing link: could not be read (RESOURCE_EXHAUSTED, reason "
        f"RATE_LIMIT_EXCEEDED, on each of {len(RATE_LIMIT_PAUSES) + 1} attempts)"
    )
    assert out.splitlines()[-1].startswith("cost-check: failed on billing link;"), out


def info(reason: str) -> list[dict[str, str]]:
    return [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": reason}]


@pytest.mark.parametrize(
    "refusal",
    [
        {"error": {"code": 429, "message": "m"}},
        {"error": {"code": 429, "message": "m", "status": "RESOURCE_EXHAUSTED"}},
        {
            "error": {
                "code": 403,
                "message": "m",
                "status": "X",
                "details": info("RATE_LIMIT_EXCEEDED"),
            }
        },
    ],
    ids=["only-the-code", "only-the-status", "only-the-reason"],
)
def test_each_sign_of_a_rate_limit_alone_is_retried(refusal: dict[str, Any]) -> None:
    gcp, paused = Limited("topic", 1, refusal), []
    code, out = check(ENV, gcp, paused.append)
    assert code == 0, out
    assert gcp.tries == 2 and paused == [RATE_LIMIT_PAUSES[0]]


def test_the_retries_are_the_two_pauses_the_architecture_names() -> None:
    assert RATE_LIMIT_PAUSES == (5.0, 15.0)
    assert (
        "sent twice more after pauses of 5 and 15 seconds" in (REPO / "ARCHITECTURE.md").read_text()
    )


def test_a_refusal_that_is_no_rate_limit_is_not_retried() -> None:
    gcp, paused = Limited("billing", 1, DENIED), []
    _, out = check(ENV, gcp, paused.append)
    assert gcp.tries == 1 and paused == []
    assert "attempts" not in line_for(out, "billing link"), out
