"""`make cost-check`: the lab's month-to-date GCP cost, which must be $0.00, and its spend fences.

With no lab project the lab spends nothing by construction: the check says so and calls no GCP
tool. With `LEDGER_LAB_GCP_PROJECT` set it
- sums this month's cost, credits included, from the Cloud Billing export named by
  `LEDGER_LAB_BILLING_EXPORT` (`dataset.table` in the lab project) through `bq`: a dry run first,
  then the query under a byte cap; any cost above zero fails;
- reads each applied fence through its REST API (`curl`; the session's proxy supplies the
  credential) and compares it with its declaration in `platform/gcp` (`fences.py`); a fence that
  is missing, differs or cannot be read fails;
- asks `testIamPermissions` whether this account itself could move a fence, and fails if it can;
- reports the budget as not checked while this account may not read it, which neither passes nor
  fails.
Every call reads; none creates, changes or deletes anything. The output names each fence by its
role and never prints a project, billing account or service account.
"""

import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from urllib.parse import quote

from fences import Declared, declared
from gcp_project import lab_project_or_none

EXPORT_VARIABLE = "LEDGER_LAB_BILLING_EXPORT"
MAX_BYTES_VARIABLE = "LEDGER_LAB_BQ_MAX_BYTES"
DEFAULT_MAX_BYTES = "1000000000"
EXPORT_NAME = re.compile(r"^[A-Za-z0-9_]+\.[A-Za-z0-9_]+$")
PROJECT_ID = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")

# Permissions that would let this account move a fence: change IAM, switch services on or off,
# change a quota override, edit or remove the kill switch's workflow, trigger, topic or
# subscription, link or unlink billing, or edit the budget. Holding any one fails the check.
PROJECT_PERMISSIONS = (
    "resourcemanager.projects.setIamPolicy",
    "iam.serviceAccounts.setIamPolicy",
    "iam.serviceAccounts.delete",
    "iam.roles.update",
    "serviceusage.services.enable",
    "serviceusage.services.disable",
    "serviceusage.quotas.update",
    "workflows.workflows.create",
    "workflows.workflows.update",
    "workflows.workflows.delete",
    "eventarc.triggers.update",
    "eventarc.triggers.delete",
    "pubsub.topics.update",
    "pubsub.topics.delete",
    "pubsub.subscriptions.delete",
    "resourcemanager.projects.createBillingAssignment",
    "resourcemanager.projects.deleteBillingAssignment",
)
BILLING_PERMISSIONS = (
    "billing.budgets.create",
    "billing.budgets.update",
    "billing.budgets.delete",
    "billing.resourceAssociations.create",
    "billing.resourceAssociations.delete",
)

Runner = Callable[[Sequence[str]], str]


def month_to_date_sql(project: str, export: str) -> str:
    """This calendar month's cost for the project, net of credits."""
    if not EXPORT_NAME.match(export):
        raise ValueError(f"{EXPORT_VARIABLE} must be dataset.table, got {export!r}")
    if not PROJECT_ID.match(project):
        raise ValueError(f"not a GCP project id: {project!r}")
    # A table name cannot be a query parameter; both parts of it are validated above.
    return (
        "SELECT IFNULL(SUM(cost), 0)"  # noqa: S608
        " + IFNULL(SUM((SELECT SUM(c.amount) FROM UNNEST(credits) AS c)), 0) AS cost"
        f" FROM `{project}.{export}`"
        " WHERE invoice.month = FORMAT_DATE('%Y%m', CURRENT_DATE())"
        " AND project.id = @project"
    )


def bq_command(project: str, export: str, max_bytes: str, *, dry_run: bool = False) -> list[str]:
    return [
        "bq",
        f"--project_id={project}",
        "query",
        *(["--dry_run"] if dry_run else []),
        "--use_legacy_sql=false",
        "--format=json",
        f"--maximum_bytes_billed={max_bytes}",
        f"--parameter=project:STRING:{project}",
        month_to_date_sql(project, export),
    ]


def run_tool(command: Sequence[str]) -> str:
    return subprocess.run(command, capture_output=True, text=True, check=True).stdout  # noqa: S603


class ReadError(Exception):
    """A read answered with an error; `status` is the API's status word, never its message."""

    def __init__(self, status: str) -> None:
        super().__init__(status)
        self.status = status


def read(
    run: Runner, url: str, *, body: dict[str, Any] | None = None, bill_to: str | None = None
) -> dict[str, Any]:
    """GET a Google API resource, or POST a read-only method such as `testIamPermissions`."""
    command = ["curl", "-sS", "--max-time", "60"]
    if bill_to is not None:
        command += ["-H", f"x-goog-user-project: {bill_to}"]
    if body is not None:
        command += ["-X", "POST", "-H", "content-type: application/json", "-d", json.dumps(body)]
    try:
        answer = json.loads(run([*command, url]))
    except subprocess.CalledProcessError as failed:
        raise ReadError(f"curl exit {failed.returncode}") from failed
    except json.JSONDecodeError as failed:
        raise ReadError("an answer that is not JSON") from failed
    if not isinstance(answer, dict):
        raise ReadError("an answer that is not a JSON object")
    if "error" in answer:
        error = answer["error"]
        raise ReadError(str(error.get("status") or error.get("code") or "an error"))
    return answer


@dataclass(frozen=True)
class Finding:
    state: str  # "ok", "FAIL" or "NOT CHECKED"
    fence: str
    detail: str

    def line(self) -> str:
        return f"cost-check: {self.state} {self.fence}: {self.detail}"


def judged(fence: str, problems: list[str], good: str) -> Finding:
    if problems:
        return Finding("FAIL", fence, "loose: " + "; ".join(problems))
    return Finding("ok", fence, good)


class Fences:
    """The live reads of one lab project, compared with the declaration."""

    def __init__(self, project: str, run: Runner, want: Declared) -> None:
        self.project = project
        self.run = run
        self.want = want
        self.p = f"projects/{project}"
        self.topic = f"{self.p}/topics/{want.topic}"
        self.workflow = f"{self.p}/locations/{want.region}/workflows/{want.workflow}"
        self.kill_switch_account = f"{want.kill_switch_account}@{project}.iam.gserviceaccount.com"

    def read(self, url: str, **kwargs: Any) -> dict[str, Any]:
        return read(self.run, url, **kwargs)

    def billing_link(self) -> tuple[Finding, str | None]:
        info = self.read(f"https://cloudbilling.googleapis.com/v1/{self.p}/billingInfo")
        account = info.get("billingAccountName") or None
        if not info.get("billingEnabled") or account is None:
            return Finding(
                "FAIL",
                "billing link",
                "billing is unlinked from the lab project: the kill switch may have fired; "
                "ask the owner",
            ), None
        return Finding("ok", "billing link", "the lab project is linked to a billing account"), (
            account
        )

    def query_cap(self) -> Finding:
        want = self.want
        metric = quote(want.query_cap_metric, safe="")
        limit = quote(want.query_cap_limit, safe="")
        answer = self.read(
            "https://serviceusage.googleapis.com/v1beta1/"
            f"{self.p}/services/bigquery.googleapis.com/consumerQuotaMetrics/{metric}"
        )
        limits = [
            item
            for item in answer.get("consumerQuotaLimits", [])
            if item.get("name", "").endswith(f"/limits/{limit}")
        ]
        buckets = [b for item in limits for b in item.get("quotaBuckets", [])]
        problems = []
        if len(buckets) != 1:
            problems.append(f"{len(buckets)} quota buckets for the per-project daily limit")
        else:
            (bucket,) = buckets
            override = bucket.get("consumerOverride", {}).get("overrideValue")
            effective = int(bucket.get("effectiveLimit", "0"))
            if override is None:
                problems.append("no override on the per-project daily limit")
            elif int(override) != want.query_cap_mib:
                problems.append(f"the override is {override} MiB a day")
            if effective > want.query_cap_mib:
                problems.append(f"the effective limit is {effective} MiB a day")
        return judged(
            "daily query cap",
            [f"{p}, declared {want.query_cap_mib}" for p in problems],
            f"{want.query_cap_mib} MiB a day, as declared",
        )

    def budget_topic(self) -> Finding:
        self.read(f"https://pubsub.googleapis.com/v1/{self.topic}")
        return Finding("ok", "budget topic", "exists, as declared")

    def kill_switch_workflow(self) -> Finding:
        want = self.want
        answer = self.read(f"https://workflows.googleapis.com/v1/{self.workflow}")
        problems = []
        if answer.get("state") != "ACTIVE":
            problems.append(f"state {answer.get('state')}, not ACTIVE")
        if answer.get("serviceAccount", "").rsplit("/", 1)[-1] != self.kill_switch_account:
            problems.append("it does not run as the kill switch account")
        if answer.get("sourceContents") != want.workflow_source:
            problems.append("its source differs from the declared workflow")
        low, high = want.kill_threshold_bounds
        raw = answer.get("userEnvVars", {}).get("KILL_THRESHOLD", "")
        try:
            threshold = float(raw)
        except ValueError:
            threshold = None
        if threshold is None or not low <= threshold <= high:
            problems.append(
                f"kill threshold {raw or 'unset'}, outside the declared {low:g}-{high:g}"
            )
        return judged(
            "kill switch workflow",
            problems,
            f"ACTIVE in {want.region}, runs as the kill switch account, source as declared, "
            f"kill threshold {threshold:g} (declared range {low:g}-{high:g})",
        )

    def kill_switch_trigger(self) -> Finding:
        want = self.want
        answer = self.read(
            f"https://eventarc.googleapis.com/v1/"
            f"{self.p}/locations/{want.region}/triggers/{want.trigger}"
        )
        problems = []
        filters = {(f.get("attribute"), f.get("value")) for f in answer.get("eventFilters", [])}
        if filters != {("type", want.trigger_event_type)}:
            problems.append("it listens for other events than a published message")
        pubsub = answer.get("transport", {}).get("pubsub", {})
        if pubsub.get("topic") != self.topic:
            problems.append("it does not listen on the budget topic")
        if answer.get("destination", {}).get("workflow") != self.workflow:
            problems.append("it does not start the kill switch workflow")
        if answer.get("serviceAccount") != self.kill_switch_account:
            problems.append("it does not run as the kill switch account")
        subscription = pubsub.get("subscription")
        if not subscription:
            problems.append("it has no subscription")
        else:
            try:
                delivers = self.read(f"https://pubsub.googleapis.com/v1/{subscription}")
            except ReadError as error:
                problems.append(f"its subscription could not be read ({error.status})")
            else:
                if delivers.get("topic") != self.topic:
                    problems.append("its subscription is not on the budget topic")
        return judged(
            "kill switch trigger",
            problems,
            "the budget topic's messages start the kill switch workflow, as declared",
        )

    def budget(self, account: str | None) -> Finding:
        want = self.want
        if account is None:
            return Finding(
                "NOT CHECKED", "budget", "no billing account is linked, so there is none to read"
            )
        try:
            answer = self.read(
                f"https://billingbudgets.googleapis.com/v1/{account}/budgets", bill_to=self.project
            )
        except ReadError as error:
            if error.status != "PERMISSION_DENIED":
                raise
            return Finding(
                "NOT CHECKED",
                "budget",
                "not readable by this account (PERMISSION_DENIED): its state is unknown, "
                "and this neither passes nor fails the check",
            )
        mine = [b for b in answer.get("budgets", []) if b.get("displayName") == want.budget_name]
        if len(mine) != 1:
            return Finding("FAIL", "budget", f"loose: {len(mine)} budgets carry the declared name")
        (budget,) = mine
        number = self.read(f"https://cloudresourcemanager.googleapis.com/v1/{self.p}")
        problems = []
        scope = budget.get("budgetFilter", {})
        if scope.get("projects") != [f"projects/{number.get('projectNumber')}"]:
            problems.append("it does not cover exactly the lab project")
        if scope.get("creditTypesTreatment") != want.budget_credits:
            problems.append("it does not count every credit")
        units = budget.get("amount", {}).get("specifiedAmount", {}).get("units")
        if units != want.budget_units:
            problems.append(f"amount {units} units, declared {want.budget_units}")
        thresholds = tuple(
            sorted(r.get("thresholdPercent") for r in budget.get("thresholdRules", []))
        )
        if thresholds != want.budget_thresholds:
            problems.append("its alert thresholds differ from the declared ones")
        if budget.get("notificationsRule", {}).get("pubsubTopic") != self.topic:
            problems.append("it does not publish to the budget topic")
        return judged(
            "budget",
            problems,
            f"{want.budget_units} unit on the lab project, credits included, alerts as declared",
        )

    def agent_permissions(self, account: str | None) -> Finding:
        held = self.read(
            f"https://cloudresourcemanager.googleapis.com/v1/{self.p}:testIamPermissions",
            body={"permissions": list(PROJECT_PERMISSIONS)},
        ).get("permissions", [])
        if account is not None:
            held += self.read(
                f"https://cloudbilling.googleapis.com/v1/{account}:testIamPermissions",
                body={"permissions": list(BILLING_PERMISSIONS)},
                bill_to=self.project,
            ).get("permissions", [])
        tested = len(PROJECT_PERMISSIONS) + (len(BILLING_PERMISSIONS) if account else 0)
        if held:
            return Finding(
                "FAIL",
                "agent permissions",
                f"loose: this account holds {', '.join(sorted(held))}, which could move a fence",
            )
        return Finding(
            "ok", "agent permissions", f"this account holds none of {tested} fence-moving ones"
        )

    def findings(self) -> list[Finding]:
        found: list[Finding] = []
        try:
            link, account = self.billing_link()
        except ReadError as error:
            link, account = (
                Finding("FAIL", "billing link", f"could not be read ({error.status})"),
                None,
            )
        found.append(link)
        for fence, judge in (
            ("daily query cap", self.query_cap),
            ("budget topic", self.budget_topic),
            ("kill switch workflow", self.kill_switch_workflow),
            ("kill switch trigger", self.kill_switch_trigger),
            ("budget", lambda: self.budget(account)),
            ("agent permissions", lambda: self.agent_permissions(account)),
        ):
            try:
                found.append(judge())
            except ReadError as error:
                found.append(Finding("FAIL", fence, f"loose: could not be read ({error.status})"))
        return found


def month_to_date(project: str, export: str, max_bytes: str, run: Runner) -> tuple[bool, str]:
    """Whether the cost is $0.00, and the line saying so."""
    try:
        dry = json.loads(run(bq_command(project, export, max_bytes, dry_run=True)))
        scanned = int(dry["statistics"]["totalBytesProcessed"])
        if scanned > int(max_bytes):
            return False, (
                f"cost-check: FAIL cost: the query would scan {scanned} bytes, above the cap of "
                f"{max_bytes}; not run"
            )
        rows = json.loads(run(bq_command(project, export, max_bytes)) or "[]")
    except subprocess.CalledProcessError as failed:
        return False, f"cost-check: FAIL cost: the cost query failed (bq exit {failed.returncode})"
    except (KeyError, IndexError, TypeError, ValueError):
        return False, "cost-check: FAIL cost: the cost query failed (an answer bq does not give)"
    cost = Decimal(str(rows[0]["cost"])) if rows else Decimal(0)
    if cost > 0:
        return False, (
            f"cost-check: FAIL month-to-date cost is ${cost:.2f}, above $0.00: stop GCP work"
        )
    return True, (
        f"cost-check: ok month-to-date cost is ${cost:.2f} "
        f"(dry run first: {scanned} bytes, under the {max_bytes}-byte cap)"
    )


def check(env: Mapping[str, str], run: Runner = run_tool) -> tuple[int, str]:
    """The exit code and the lines to print."""
    project = lab_project_or_none(env)
    if project is None:
        return 0, (
            "cost-check: the lab has no GCP project (LEDGER_LAB_GCP_PROJECT unset); spends $0.00"
        )
    export = env.get(EXPORT_VARIABLE, "").strip()
    if not export:
        return 1, f"cost-check: set {EXPORT_VARIABLE} to the billing export's dataset.table"
    max_bytes = env.get(MAX_BYTES_VARIABLE, DEFAULT_MAX_BYTES)
    zero, cost_line = month_to_date(project, export, max_bytes, run)
    findings = Fences(project, run, declared()).findings()
    lines = [cost_line, *(f.line() for f in findings)]
    failed = [f.fence for f in findings if f.state == "FAIL"]
    unchecked = [f.fence for f in findings if f.state == "NOT CHECKED"]
    ok = sum(f.state == "ok" for f in findings)
    tally = f"{ok} fences ok, {len(unchecked)} not checked" + (
        f" ({', '.join(unchecked)})" if unchecked else ""
    )
    if zero and not failed:
        lines.append(f"cost-check: passed: $0.00, {tally}")
        return 0, "\n".join(lines)
    reasons = (["the cost"] if not zero else []) + failed
    lines.append(f"cost-check: failed on {', '.join(reasons)}; {tally}")
    return 1, "\n".join(lines)


def main() -> int:
    code, lines = check(os.environ)
    print(lines)
    return code


if __name__ == "__main__":
    sys.exit(main())
