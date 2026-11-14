"""`make cost-check`: the lab's month-to-date GCP cost, which must be $0.00, and its spend fences.

With no lab project the lab spends nothing by construction: the check says so and calls no GCP
tool. With `LEDGER_LAB_GCP_PROJECT` set it
- reads this month's cost, credits included, and the newest export time from the Cloud Billing
  export named by `LEDGER_LAB_BILLING_EXPORT` (`dataset.table` in the lab project) through `bq`:
  a dry run first, then the query under a fixed byte cap; a cost above zero fails, and so does an
  answer that is not the query's one row, or an export that holds no rows for the project;
- reads each applied fence through its REST API (`curl`; the session's proxy supplies the
  credential) and compares it with its declaration in `platform/gcp` (`fences.py`); a fence that
  is missing, differs or answers in an unexpected shape fails;
- asks `testIamPermissions` whether this account itself could move a fence, and fails if it can;
- reports a fence this account may not read (the budget, the kill switch's project roles) as not
  checked, which neither passes nor fails.
Every call reads; none creates, changes or deletes anything. The output names each fence by its
role and never prints a project, billing account, service account or an API's error message.
"""

import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote

from fences import Declared, declared
from gcp_project import lab_project_or_none

EXPORT_VARIABLE = "LEDGER_LAB_BILLING_EXPORT"
# The cost query scans well under a megabyte; a fixed cap far above that and far inside the free
# tier, with no setting that could raise it.
MAX_BYTES_BILLED = 1_000_000_000
EXPORT_NAME = re.compile(r"^[A-Za-z0-9_]+\.[A-Za-z0-9_]+$")
PROJECT_ID = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")

# Permissions that would let this account move a fence, tested where each is granted. On the
# project: change IAM, disable or delete the kill switch's account, switch services on or off,
# change a quota, edit or remove the workflow, the trigger, the topic or its subscription, or
# link or unlink billing.
PROJECT_PERMISSIONS = (
    "resourcemanager.projects.setIamPolicy",
    "iam.serviceAccounts.setIamPolicy",
    "iam.serviceAccounts.disable",
    "iam.serviceAccounts.delete",
    "iam.roles.update",
    "serviceusage.services.enable",
    "serviceusage.services.disable",
    "serviceusage.quotas.update",
    "cloudquotas.quotas.update",
    "workflows.workflows.create",
    "workflows.workflows.update",
    "workflows.workflows.delete",
    "eventarc.triggers.update",
    "eventarc.triggers.delete",
    "pubsub.topics.update",
    "pubsub.topics.delete",
    "pubsub.topics.setIamPolicy",
    "pubsub.topics.detachSubscription",
    "pubsub.subscriptions.update",
    "pubsub.subscriptions.delete",
    "resourcemanager.projects.createBillingAssignment",
    "resourcemanager.projects.deleteBillingAssignment",
)
# On the billing account: edit the budget, link or unlink a project, or change who may.
BILLING_PERMISSIONS = (
    "billing.budgets.create",
    "billing.budgets.update",
    "billing.budgets.delete",
    "billing.resourceAssociations.create",
    "billing.resourceAssociations.delete",
    "billing.accounts.setIamPolicy",
)
# On the kill switch's own account, which holds the billing role: act as it, or disable, delete or
# re-grant it.
KILL_SWITCH_ACCOUNT_PERMISSIONS = (
    "iam.serviceAccounts.getAccessToken",
    "iam.serviceAccountKeys.create",
    "iam.serviceAccounts.actAs",
    "iam.serviceAccounts.setIamPolicy",
    "iam.serviceAccounts.disable",
    "iam.serviceAccounts.delete",
)

Runner = Callable[[Sequence[str]], str]


def month_to_date_sql(project: str, export: str) -> str:
    """One row: the month's cost net of credits, its rows, all rows and the newest export."""
    if not EXPORT_NAME.match(export):
        raise ValueError(f"{EXPORT_VARIABLE} must be dataset.table, got {export!r}")
    if not PROJECT_ID.match(project):
        raise ValueError(f"not a GCP project id: {project!r}")
    this_month = "invoice.month = FORMAT_DATE('%Y%m', CURRENT_DATE())"
    # A table name cannot be a query parameter; both parts of it are validated above.
    return (
        f"SELECT IFNULL(SUM(IF({this_month}, IFNULL(cost, 0)"  # noqa: S608
        " + IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) AS c), 0), 0)), 0) AS cost,"
        f" COUNTIF({this_month}) AS month_rows,"
        " COUNT(*) AS all_rows,"
        " FORMAT_TIMESTAMP('%FT%H:%MZ', MAX(export_time)) AS newest_export"
        f" FROM `{project}.{export}`"
        " WHERE project.id = @project"
    )


def bq_command(project: str, export: str, *, dry_run: bool = False) -> list[str]:
    return [
        "bq",
        f"--project_id={project}",
        "query",
        *(["--dry_run"] if dry_run else []),
        "--use_legacy_sql=false",
        "--format=json",
        f"--maximum_bytes_billed={MAX_BYTES_BILLED}",
        f"--parameter=project:STRING:{project}",
        month_to_date_sql(project, export),
    ]


def run_tool(command: Sequence[str]) -> str:
    return subprocess.run(command, capture_output=True, text=True, check=True).stdout  # noqa: S603


def tool_failure(failed: Exception) -> str:
    if isinstance(failed, subprocess.CalledProcessError):
        return f"exit {failed.returncode}"
    if isinstance(failed, FileNotFoundError):
        return "not installed"
    return type(failed).__name__


class ReadError(Exception):
    """A read failed; `status` and `reason` are the API's words, never its message."""

    def __init__(self, status: str, reason: str | None = None) -> None:
        super().__init__(status)
        self.status = status
        self.reason = reason

    def __str__(self) -> str:
        return f"{self.status}, reason {self.reason}" if self.reason else self.status


class Shape(ValueError):
    """An answer lacks a field the judgement needs, or holds it in the wrong type."""


def error_reason(error: dict[str, Any]) -> str | None:
    details = error.get("details")
    for detail in details if isinstance(details, list) else []:
        if (
            isinstance(detail, dict)
            and str(detail.get("@type", "")).endswith("google.rpc.ErrorInfo")
            and isinstance(detail.get("reason"), str)
        ):
            return detail["reason"]
    return None


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
    except (OSError, subprocess.SubprocessError) as failed:
        raise ReadError(f"curl {tool_failure(failed)}") from failed
    except json.JSONDecodeError as failed:
        raise ReadError("an answer that is not JSON") from failed
    if not isinstance(answer, dict):
        raise ReadError("an answer that is not a JSON object")
    if "error" in answer:
        error = answer["error"]
        if not isinstance(error, dict):
            raise ReadError("an error answer without a status")
        status = str(error.get("status") or error.get("code") or "an error")
        raise ReadError(status, error_reason(error))
    return answer


def field(answer: Any, *path: str, kind: type | tuple[type, ...] = str) -> Any:
    """`answer[path...]`, which must exist and be of `kind`."""
    value = answer
    for key in path:
        if not isinstance(value, dict) or key not in value:
            raise Shape(f"no {'.'.join(path)}")
        value = value[key]
    if not isinstance(value, kind):
        raise Shape(f"{'.'.join(path)} is not a {getattr(kind, '__name__', 'number')}")
    return value


def optional(answer: dict[str, Any], key: str, kind: type, default: Any) -> Any:
    """`answer[key]` if present, else `default`; present, it must be of `kind`."""
    value = answer.get(key, default)
    if not isinstance(value, kind):
        raise Shape(f"{key} is not a {kind.__name__}")
    return value


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


UNKNOWN = "its state is unknown, and this neither passes nor fails the check"


def not_readable(fence: str, error: ReadError) -> Finding | None:
    """A refused read, worded so it cannot be mistaken for ok; None when it is not a refusal."""
    if error.status != "PERMISSION_DENIED":
        return None
    if error.reason in (None, "IAM_PERMISSION_DENIED"):
        return Finding("NOT CHECKED", fence, f"not readable by this account ({error}): {UNKNOWN}")
    return Finding("NOT CHECKED", fence, f"the read was refused ({error}): {UNKNOWN}")


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
        self.kill_switch_resource = f"{self.p}/serviceAccounts/{self.kill_switch_account}"

    def read(self, url: str, **kwargs: Any) -> dict[str, Any]:
        return read(self.run, url, **kwargs)

    def billing_link(self) -> tuple[Finding, str | None]:
        info = self.read(f"https://cloudbilling.googleapis.com/v1/{self.p}/billingInfo")
        enabled = optional(info, "billingEnabled", bool, False)
        account = optional(info, "billingAccountName", str, "")
        if not enabled or not account.startswith("billingAccounts/"):
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
            for item in field(answer, "consumerQuotaLimits", kind=list)
            if field(item, "name").endswith(f"/limits/{limit}")
        ]
        buckets = [b for item in limits for b in field(item, "quotaBuckets", kind=list)]
        problems = []
        if len(buckets) != 1:
            problems.append(f"{len(buckets)} quota buckets for the per-project daily limit")
        else:
            (bucket,) = buckets
            override = bucket.get("consumerOverride")
            if override is None:
                problems.append("no override on the per-project daily limit")
            elif int(field(override, "overrideValue")) != want.query_cap_mib:
                problems.append(f"the override is {override['overrideValue']} MiB a day")
            if "effectiveLimit" not in bucket:
                problems.append("no effective limit")
            elif int(field(bucket, "effectiveLimit")) > want.query_cap_mib:
                problems.append(f"the effective limit is {bucket['effectiveLimit']} MiB a day")
        return judged(
            "daily query cap",
            [f"{p}, declared {want.query_cap_mib}" for p in problems],
            f"{want.query_cap_mib} MiB a day, as declared",
        )

    def budget_topic(self) -> Finding:
        answer = self.read(f"https://pubsub.googleapis.com/v1/{self.topic}")
        problems = [] if answer.get("name") == self.topic else ["the answer is not the topic"]
        return judged("budget topic", problems, "exists, as declared")

    def kill_switch_workflow(self) -> Finding:
        want = self.want
        answer = self.read(f"https://workflows.googleapis.com/v1/{self.workflow}")
        problems = []
        if answer.get("name") != self.workflow:
            problems.append("the answer is not the workflow")
        if answer.get("state") != "ACTIVE":
            problems.append(f"state {answer.get('state')}, not ACTIVE")
        runs_as = optional(answer, "serviceAccount", str, "").rsplit("/", 1)[-1]
        if runs_as != self.kill_switch_account:
            problems.append("it does not run as the kill switch account")
        if answer.get("sourceContents") != want.workflow_source:
            problems.append("its source differs from the declared workflow")
        low, high = want.kill_threshold_bounds
        raw = optional(answer, "userEnvVars", dict, {}).get("KILL_THRESHOLD", "")
        try:
            threshold = float(raw)
        except (TypeError, ValueError):
            threshold = None
        if threshold is None or not low <= threshold <= high:
            problems.append(
                f"kill threshold {raw or 'unset'}, outside the declared {low:g}-{high:g}"
            )
            threshold = 0.0
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
        filters = {
            (field(f, "attribute"), field(f, "value"))
            for f in optional(answer, "eventFilters", list, [])
        }
        if filters != {("type", want.trigger_event_type)}:
            problems.append("it listens for other events than a published message")
        pubsub = field(answer, "transport", "pubsub", kind=dict)
        if pubsub.get("topic") != self.topic:
            problems.append("it does not listen on the budget topic")
        if optional(answer, "destination", dict, {}).get("workflow") != self.workflow:
            problems.append("it does not start the kill switch workflow")
        if answer.get("serviceAccount") != self.kill_switch_account:
            problems.append("it does not run as the kill switch account")
        subscription = pubsub.get("subscription")
        if not isinstance(subscription, str) or not subscription.startswith(
            f"{self.p}/subscriptions/"
        ):
            problems.append("it has no subscription")
        else:
            try:
                delivers = self.read(f"https://pubsub.googleapis.com/v1/{subscription}")
            except ReadError as error:
                problems.append(f"its subscription could not be read ({error})")
            else:
                if delivers.get("name") != subscription or delivers.get("topic") != self.topic:
                    problems.append("its subscription is not on the budget topic")
        return judged(
            "kill switch trigger",
            problems,
            "the budget topic's messages start the kill switch workflow, as declared",
        )

    def kill_switch_account_state(self) -> Finding:
        answer = self.read(f"https://iam.googleapis.com/v1/{self.kill_switch_resource}")
        problems = []
        if answer.get("email") != self.kill_switch_account:
            problems.append("the answer is not the kill switch account")
        if optional(answer, "disabled", bool, False):
            problems.append("the account is disabled")
        return judged("kill switch account", problems, "exists and is enabled, as declared")

    def kill_switch_roles(self) -> Finding:
        fence = "kill switch roles"
        try:
            policy = self.read(
                f"https://cloudresourcemanager.googleapis.com/v1/{self.p}:getIamPolicy",
                body={"options": {"requestedPolicyVersion": 3}},
            )
        except ReadError as error:
            refused = not_readable(fence, error)
            if refused is None:
                raise
            return refused
        member = f"serviceAccount:{self.kill_switch_account}"
        held = {
            field(b, "role")
            for b in field(policy, "bindings", kind=list)
            if "condition" not in b and member in field(b, "members", kind=list)
        }
        missing = [role for role in self.want.kill_switch_roles if role not in held]
        return judged(
            fence,
            [f"the kill switch account no longer holds {', '.join(missing)}"] if missing else [],
            f"the kill switch account holds {', '.join(self.want.kill_switch_roles)}, as declared",
        )

    def budget(self, account: str | None) -> Finding:
        want = self.want
        if account is None:
            return Finding(
                "NOT CHECKED", "budget", f"no billing account is linked to read it from: {UNKNOWN}"
            )
        try:
            answer = self.read(
                f"https://billingbudgets.googleapis.com/v1/{account}/budgets", bill_to=self.project
            )
        except ReadError as error:
            refused = not_readable("budget", error)
            if refused is None:
                raise
            return refused
        mine = [
            b
            for b in optional(answer, "budgets", list, [])
            if field(b, "displayName") == want.budget_name
        ]
        if len(mine) != 1:
            return Finding("FAIL", "budget", f"loose: {len(mine)} budgets carry the declared name")
        (budget,) = mine
        number = field(
            self.read(f"https://cloudresourcemanager.googleapis.com/v1/{self.p}"), "projectNumber"
        )
        problems = []
        scope = field(budget, "budgetFilter", kind=dict)
        expected_scope = {
            "projects": [f"projects/{number}"],
            "creditTypesTreatment": want.budget_credits,
            "calendarPeriod": want.budget_period,
        }
        if {k: v for k, v in scope.items() if v not in ([], {}, "", None)} != expected_scope:
            problems.append(
                "its filter is not exactly the lab project, every credit and the declared period"
            )
        units = field(budget, "amount", "specifiedAmount", "units")
        if units != want.budget_units:
            problems.append(f"amount {units} units, declared {want.budget_units}")
        thresholds = tuple(
            sorted(
                (float(field(r, "thresholdPercent", kind=(int, float))), field(r, "spendBasis"))
                for r in field(budget, "thresholdRules", kind=list)
            )
        )
        if thresholds != want.budget_thresholds:
            problems.append("its alert thresholds or their basis differ from the declared ones")
        if optional(budget, "notificationsRule", dict, {}).get("pubsubTopic") != self.topic:
            problems.append("it does not publish to the budget topic")
        return judged(
            "budget",
            problems,
            f"{want.budget_units} unit a {want.budget_period.lower()} on the lab project, "
            "credits included, alerts as declared",
        )

    def held(self, url: str, permissions: Sequence[str], **kwargs: Any) -> list[str]:
        answer = self.read(url, body={"permissions": list(permissions)}, **kwargs)
        held = optional(answer, "permissions", list, [])
        if not all(isinstance(p, str) for p in held):
            raise Shape("permissions are not names")
        return held

    def agent_permissions(self, account: str | None) -> Finding:
        held = self.held(
            f"https://cloudresourcemanager.googleapis.com/v1/{self.p}:testIamPermissions",
            PROJECT_PERMISSIONS,
        )
        held += self.held(
            f"https://iam.googleapis.com/v1/{self.kill_switch_resource}:testIamPermissions",
            KILL_SWITCH_ACCOUNT_PERMISSIONS,
        )
        tested = len(PROJECT_PERMISSIONS) + len(KILL_SWITCH_ACCOUNT_PERMISSIONS)
        if account is not None:
            held += self.held(
                f"https://cloudbilling.googleapis.com/v1/{account}:testIamPermissions",
                BILLING_PERMISSIONS,
                bill_to=self.project,
            )
            tested += len(BILLING_PERMISSIONS)
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
        account: str | None = None
        try:
            link, account = self.billing_link()
        except (ReadError, Shape) as error:
            link = Finding("FAIL", "billing link", f"could not be read ({error})")
        found = [link]
        for fence, judge in (
            ("daily query cap", self.query_cap),
            ("budget topic", self.budget_topic),
            ("kill switch workflow", self.kill_switch_workflow),
            ("kill switch trigger", self.kill_switch_trigger),
            ("kill switch account", self.kill_switch_account_state),
            ("kill switch roles", self.kill_switch_roles),
            ("budget", lambda: self.budget(account)),
            ("agent permissions", lambda: self.agent_permissions(account)),
        ):
            try:
                found.append(judge())
            except ReadError as error:
                found.append(Finding("FAIL", fence, f"loose: could not be read ({error})"))
            except (KeyError, TypeError, ValueError, AttributeError) as error:
                shape = error if isinstance(error, Shape) else type(error).__name__
                found.append(
                    Finding("FAIL", fence, f"loose: an answer of unexpected shape ({shape})")
                )
        return found


def cost_row(answer: str) -> tuple[Decimal, int, int, str | None]:
    """The query's one row: cost, this month's rows, all rows, newest export time."""
    try:
        rows = json.loads(answer)
    except json.JSONDecodeError as failed:
        raise Shape("an answer that is not JSON") from failed
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise Shape("not the query's one row")
    (row,) = rows
    if row.get("cost") is None:
        raise Shape("no cost in the row")
    try:
        cost = Decimal(str(row["cost"]))
        month_rows, all_rows = int(row["month_rows"]), int(row["all_rows"])
    except (KeyError, TypeError, ValueError, InvalidOperation) as failed:
        raise Shape("a row field is not a number") from failed
    if not cost.is_finite():
        raise Shape("the cost is not a number")
    newest = row.get("newest_export")
    return cost, month_rows, all_rows, newest if isinstance(newest, str) else None


def month_to_date(project: str, export: str, run: Runner) -> tuple[bool, str]:
    """Whether the cost is $0.00, and the line saying so."""
    try:
        dry = json.loads(run(bq_command(project, export, dry_run=True)))
        scanned = int(field(dry, "statistics", "totalBytesProcessed"))
        if scanned > MAX_BYTES_BILLED:
            return False, (
                f"cost-check: FAIL cost: the query would scan {scanned} bytes, above the cap of "
                f"{MAX_BYTES_BILLED}; not run"
            )
        cost, month_rows, all_rows, newest = cost_row(run(bq_command(project, export)))
    except (OSError, subprocess.SubprocessError) as failed:
        return False, f"cost-check: FAIL cost: the cost query failed (bq {tool_failure(failed)})"
    except (KeyError, TypeError, ValueError) as failed:
        shape = failed if isinstance(failed, Shape) else type(failed).__name__
        return False, f"cost-check: FAIL cost: the cost query answered unexpectedly ({shape})"
    if all_rows == 0:
        return False, (
            "cost-check: FAIL cost: the billing export holds no rows for the lab project, so "
            "its cost cannot be read from it; check that the export is live"
        )
    cents = cost.quantize(Decimal("0.01"))
    shown = f"${cents}" if cost == cents else f"${cents} (exactly {cost})"
    fresh = (
        f"{month_rows} rows this month" if month_rows else "no rows yet this month"
    ) + f", newest export {newest or 'unknown'}"
    if cost > 0:
        return False, (
            f"cost-check: FAIL month-to-date cost is {shown}, above $0.00: stop GCP work ({fresh})"
        )
    return True, (
        f"cost-check: ok month-to-date cost is {shown} ({fresh}; dry run first: {scanned} bytes, "
        f"under the {MAX_BYTES_BILLED}-byte cap)"
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
    zero, cost_line = month_to_date(project, export, run)
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
