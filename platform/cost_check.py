"""`make cost-check`: the lab's month-to-date GCP cost, which must be $0.00, and its spend fences.

With no lab project the lab spends nothing by construction: the check says so and calls no GCP
tool. In a session that carries another lab's `GCP_PROJECT` it calls no GCP tool either, since the
credential may be that lab's, and fails, since it has read nothing that shows the lab's cost is
$0.00. With `LEDGER_LAB_GCP_PROJECT` set and no `GCP_PROJECT` it
- reads this month's cost, credits included, and the newest export time from the Cloud Billing
  export through the view `LEDGER_LAB_BILLING_EXPORT` names (`dataset.table` in the lab project;
  the agent account reads the export only through it) through `bq`:
  a dry run first, then the query under a fixed byte cap; a cost above zero fails, and so does an
  answer that is not the query's one row, or an export that holds no rows for the project;
  the cost is shown in the currency the export's rows for the project carry (credits are in it
  too), and rows that carry more than one currency, none, or one that is not a three-letter code
  fail; the cost covers the lab project's rows only, not the whole billing account;
- reads each applied fence through its REST API (`curl`) and compares it with its declaration in
  `platform/gcp` (`fences.py`); a fence that is missing, differs or answers in an unexpected shape
  fails. Where the environment hands gcloud its token (`CLOUDSDK_AUTH_ACCESS_TOKEN`, as a cloud
  session does), curl sends no credential and the session's proxy supplies it; elsewhere curl sends
  the token of gcloud's own login, never a key file, and a read that login lacks the IAM
  permission on the resource for is not checked rather than failed, while any other refusal of a
  fence read still fails, among them one by a login that may not spend the lab project's quota
  (`USER_PROJECT_DENIED`); the budget and the kill switch's project roles are the exception below;
- asks `testIamPermissions` whether this account itself could move a fence, and fails if it can;
  off the proxy this account is gcloud's login, not the agent account, so it is not asked;
- reports a fence this account may not read (the budget, the kill switch's project roles) as not
  checked, which neither passes nor fails.
A REST read (`curl`) the API answers with a rate limit is sent again after each of
`RATE_LIMIT_PAUSES`, and fails as any unread fence does once the last attempt is refused too. The
billing reads name the lab project as the one whose quota they spend (`x-goog-user-project`): sent
with gcloud's own login and no quota project, Google may charge a read to gcloud's shared project,
which every gcloud user draws on (https://cloud.google.com/docs/quotas/quota-project).
Every call reads; none creates, changes or deletes anything. The output names each fence by its
role and never prints a project, billing account, service account or an API's error message.
"""

import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote

from fences import Declared, declared
from gcp_project import FOREIGN, foreign_session, lab_project_or_none

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
    "iam.serviceAccounts.getOpenIdToken",
    "iam.serviceAccounts.signJwt",
    "iam.serviceAccounts.signBlob",
    "iam.serviceAccounts.implicitDelegation",
    "iam.serviceAccountKeys.create",
    "iam.serviceAccounts.actAs",
    "iam.serviceAccounts.setIamPolicy",
    "iam.serviceAccounts.disable",
    "iam.serviceAccounts.delete",
)

Runner = Callable[[Sequence[str]], str]

# Set where the environment, not gcloud's own login, gives gcloud its token; behind the cloud
# session's proxy it holds a placeholder and the proxy puts the credential on every request.
INJECTED_VARIABLE = "CLOUDSDK_AUTH_ACCESS_TOKEN"
LOGIN_TOKEN_COMMAND = ("gcloud", "auth", "print-access-token")


def month_to_date_sql(project: str, export: str) -> str:
    """One row: the month's cost net of credits, its rows, all rows, the newest export, and the
    currencies of all the project's rows (distinct, how many lack one, and the least of them).

    The currency is judged on every row of the project, not this month's alone: a month with no
    rows yet still has a unit to show, and a subset of rows that all carry one currency carries
    that one too. COUNT(DISTINCT) is exact in BigQuery and skips NULL, which the second count
    counts."""
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
        " FORMAT_TIMESTAMP('%FT%H:%MZ', MAX(export_time)) AS newest_export,"
        " COUNT(DISTINCT currency) AS currencies,"
        " COUNTIF(currency IS NULL) AS rows_without_currency,"
        " MIN(currency) AS currency"
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

    def __init__(self, status: str, reason: str | None = None, attempts: int = 1) -> None:
        super().__init__(status)
        self.status = status
        self.reason = reason
        self.attempts = attempts

    def __str__(self) -> str:
        said = f"{self.status}, reason {self.reason}" if self.reason else self.status
        return said + (f", on each of {self.attempts} attempts" if self.attempts > 1 else "")


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


class Credential:
    """What curl sends to authenticate a read.

    With the token injected by the environment, nothing: the proxy authenticates the request.
    Otherwise the token of gcloud's own login, asked for once and written to a header file only this
    user can read, which curl reads with `-H @file`, so the token is on no command line.
    """

    def __init__(self, env: Mapping[str, str], run: Runner) -> None:
        self.injected = bool(env.get(INJECTED_VARIABLE, "").strip())
        self.run = run
        self.folder: str | None = None
        self.header: str | None = None
        self.failure: ReadError | None = None

    def curl_arguments(self) -> list[str]:
        if self.injected:
            return []
        if self.header is None and self.failure is None:
            self.header, self.failure = self.write_header()
        if self.header is None:
            raise self.failure or ReadError("no login token")
        return ["-H", f"@{self.header}"]

    def write_header(self) -> tuple[str | None, ReadError | None]:
        try:
            token = self.run(LOGIN_TOKEN_COMMAND).strip()
        except (OSError, subprocess.SubprocessError) as failed:
            return None, ReadError(f"no login token (gcloud {tool_failure(failed)})")
        if not token or any(c.isspace() or not c.isprintable() for c in token):
            return None, ReadError("no login token (gcloud printed none)")
        self.folder = tempfile.mkdtemp(prefix="cost-check-")
        header = os.path.join(self.folder, "authorization")
        descriptor = os.open(header, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as file:
            file.write(f"Authorization: Bearer {token}\n")
        return header, None

    def close(self) -> None:
        if self.folder is not None:
            shutil.rmtree(self.folder, ignore_errors=True)
            self.folder = self.header = None


# The pauses before each further attempt at a read the API answers with a rate limit: three
# attempts with twenty seconds of pauses between them, so a minute's quota has had time to refill
# in part; each attempt itself is bounded by curl's `--max-time`.
RATE_LIMIT_PAUSES = (5.0, 15.0)


def rate_limited(error: ReadError) -> bool:
    """Whether the API refused the read for a quota, which a later attempt may pass."""
    return error.status in ("RESOURCE_EXHAUSTED", "429") or error.reason == "RATE_LIMIT_EXCEEDED"


def read(
    run: Runner,
    url: str,
    *,
    body: dict[str, Any] | None = None,
    bill_to: str | None = None,
    credential: Credential | None = None,
    pause: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """GET a Google API resource, or POST a read-only method such as `testIamPermissions`; a
    rate-limited read is sent again after each of RATE_LIMIT_PAUSES, and its last refusal raised,
    saying how many attempts were made."""
    for waited in (*RATE_LIMIT_PAUSES, None):
        try:
            return read_once(run, url, body=body, bill_to=bill_to, credential=credential)
        except ReadError as error:
            if not rate_limited(error):
                raise
            if waited is None:
                raise ReadError(
                    error.status, error.reason, attempts=len(RATE_LIMIT_PAUSES) + 1
                ) from error
            pause(waited)
    raise AssertionError("unreachable")


def read_once(
    run: Runner,
    url: str,
    *,
    body: dict[str, Any] | None = None,
    bill_to: str | None = None,
    credential: Credential | None = None,
) -> dict[str, Any]:
    """One attempt at a read."""
    command = ["curl", "-sS", "--max-time", "60"]
    if credential is not None and not credential.injected:
        # `-q`, first: no curlrc, whose trace or header-dump options would write the token out.
        command = ["curl", "-q", *command[1:], *credential.curl_arguments()]
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


# Every Google API read the fence checks make, by the URL's shape, with the permission it needs
# and where that permission is held: `project` reads are what the agent account holds on the lab
# project, `budget topic` and `kill switch account` reads are held on that one resource alone
# (declared in platform/gcp), `billing account` reads are on the linked billing account, and
# `none` methods (`testIamPermissions`) need no permission. A read whose URL matches no row is
# refused before it is sent, so a new read cannot be made without a row here, where the tests of
# the agent account's hand-granted roles and of the access rules read it. A read sent with
# `bill_to` also needs `serviceusage.services.use` on the project it bills.
API = r"https://[a-z]+\.googleapis\.com/v1(beta1)?/"
P = r"projects/[^/:]+"
READS: tuple[tuple[str, str | None, str], ...] = (
    (rf"{API}{P}/billingInfo", "resourcemanager.projects.get", "project"),
    (
        rf"{API}{P}/services/bigquery\.googleapis\.com/consumerQuotaMetrics/[^/]+",
        "serviceusage.quotas.get",
        "project",
    ),
    (rf"{API}{P}/topics/[^/]+", "pubsub.topics.get", "budget topic"),
    (rf"{API}{P}/subscriptions/[^/]+", "pubsub.subscriptions.get", "project"),
    (rf"{API}{P}/locations/[^/]+/workflows/[^/]+", "workflows.workflows.get", "project"),
    (rf"{API}{P}/locations/[^/]+/triggers/[^/]+", "eventarc.triggers.get", "project"),
    (rf"{API}{P}/serviceAccounts/[^/:]+", "iam.serviceAccounts.get", "kill switch account"),
    (rf"{API}{P}:getIamPolicy", "resourcemanager.projects.getIamPolicy", "project"),
    (rf"{API}{P}", "resourcemanager.projects.get", "project"),
    (rf"{API}billingAccounts/[^/]+/budgets", "billing.budgets.list", "billing account"),
    (rf"{API}{P}:testIamPermissions", None, "none"),
    (rf"{API}{P}/serviceAccounts/[^/:]+:testIamPermissions", None, "none"),
    (rf"{API}billingAccounts/[^/:]+:testIamPermissions", None, "none"),
)
BILLED_READ_PERMISSION = "serviceusage.services.use"


class RefusedRead(ValueError):
    """A read the cost check does not list in READS, refused before anything is sent."""


def read_permission(url: str) -> tuple[str | None, str]:
    """The permission a read of `url` needs and where it is held; refuses an unlisted read."""
    matches = [(p, scope) for shape, p, scope in READS if re.fullmatch(shape, url)]
    if len(matches) != 1:
        raise RefusedRead(
            f"a read the cost check does not list in READS: {len(matches)} rows match"
        )
    return matches[0]


class Fences:
    """The live reads of one lab project, compared with the declaration."""

    def __init__(
        self,
        project: str,
        run: Runner,
        want: Declared,
        credential: Credential | None = None,
        pause: Callable[[float], None] = time.sleep,
    ) -> None:
        self.project = project
        self.run = run
        self.pause = pause
        self.want = want
        self.credential = credential
        self.p = f"projects/{project}"
        self.link_read = True
        self.topic = f"{self.p}/topics/{want.topic}"
        self.workflow = f"{self.p}/locations/{want.region}/workflows/{want.workflow}"
        self.kill_switch_account = f"{want.kill_switch_account}@{project}.iam.gserviceaccount.com"
        self.kill_switch_resource = f"{self.p}/serviceAccounts/{self.kill_switch_account}"

    def read(self, url: str, **kwargs: Any) -> dict[str, Any]:
        read_permission(url)
        return read(self.run, url, credential=self.credential, pause=self.pause, **kwargs)

    def refused_to_login(self, fence: str, error: ReadError) -> Finding | None:
        """Off the proxy, a read gcloud's login lacks the permission for: not checked; else None.
        Any other refusal, such as an API that is off (`SERVICE_DISABLED`), still fails."""
        login = self.credential is not None and not self.credential.injected
        lacks_permission = error.status == "PERMISSION_DENIED" and error.reason in (
            None,
            "IAM_PERMISSION_DENIED",
        )
        return not_readable(fence, error) if login and lacks_permission else None

    def unread(self, fence: str, error: ReadError, failed: str = "loose: ") -> Finding:
        """A read that failed: off the proxy, one the login is refused is not checked."""
        refused = self.refused_to_login(fence, error)
        return refused or Finding("FAIL", fence, f"{failed}could not be read ({error})")

    def billing_link(self) -> tuple[Finding, str | None]:
        info = self.read(
            f"https://cloudbilling.googleapis.com/v1/{self.p}/billingInfo", bill_to=self.project
        )
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
                refused = self.refused_to_login("kill switch trigger", error)
                if refused is not None and not problems:
                    return refused
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
                "NOT CHECKED",
                "budget",
                f"no billing account is linked to read it from: {UNKNOWN}"
                if self.link_read
                else f"the billing link was not read, so its account is not known: {UNKNOWN}",
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
        if self.credential is not None and not self.credential.injected:
            # testIamPermissions answers for the caller, which off the proxy is gcloud's login.
            return Finding(
                "NOT CHECKED",
                "agent permissions",
                "off the proxy this account is the machine's gcloud login, not the agent account, "
                f"so what it may do says nothing of the agent's: {UNKNOWN}",
            )
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
        except ReadError as error:
            link = self.unread("billing link", error, failed="")
            self.link_read = False
        except Shape as error:
            link = Finding("FAIL", "billing link", f"could not be read ({error})")
            self.link_read = False
        except RefusedRead as error:
            link = Finding("FAIL", "billing link", f"loose: a read that was refused ({error})")
            self.link_read = False
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
                found.append(self.unread(fence, error))
            except RefusedRead as error:
                found.append(Finding("FAIL", fence, f"loose: a read that was refused ({error})"))
            except (KeyError, TypeError, ValueError, AttributeError) as error:
                shape = error if isinstance(error, Shape) else type(error).__name__
                found.append(
                    Finding("FAIL", fence, f"loose: an answer of unexpected shape ({shape})")
                )
        return found


@dataclass(frozen=True)
class CostRow:
    """The cost query's one row."""

    cost: Decimal
    month_rows: int
    all_rows: int
    newest: str | None
    currencies: int
    rows_without_currency: int
    currency: str | None


CURRENCY_CODE = re.compile(r"[A-Z]{3}")


def cost_row(answer: str) -> CostRow:
    """The query's one row: cost, this month's rows, all rows, newest export time, currencies."""
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
        counts = [
            int(row[name])
            for name in ("month_rows", "all_rows", "currencies", "rows_without_currency")
        ]
    except (KeyError, TypeError, ValueError, InvalidOperation) as failed:
        raise Shape("a row field is not a number") from failed
    if not cost.is_finite():
        raise Shape("the cost is not a number")
    if any(count < 0 for count in counts):
        raise Shape("a row count is negative")
    month_rows, all_rows, currencies, rows_without_currency = counts
    newest, currency = row.get("newest_export"), row.get("currency")
    return CostRow(
        cost,
        month_rows,
        all_rows,
        newest if isinstance(newest, str) else None,
        currencies,
        rows_without_currency,
        currency if isinstance(currency, str) else None,
    )


def currency_problem(row: CostRow) -> str | None:
    """Why the rows give the cost no one unit, or None when they all carry one currency code."""
    rows = "the lab project's rows in the billing export"
    if row.currencies > 1:
        return f"{rows} carry {row.currencies} currencies"
    if row.currencies == 0 or row.currency is None:
        return f"{rows} carry no currency it can read"
    if row.rows_without_currency:
        return f"{row.rows_without_currency} of {rows} carry no currency"
    if not CURRENCY_CODE.fullmatch(row.currency):
        return f"{rows} carry a currency that is not a three-letter code"
    return None


def unjudged(reason: str) -> tuple[bool, str, None]:
    """A cost line that fails before any amount is judged."""
    return False, f"cost-check: FAIL cost: {reason}", None


def month_to_date(project: str, export: str, run: Runner) -> tuple[bool, str, str | None]:
    """Whether the cost is zero, the line saying so, and the currency it is in (None unless the
    rows carry one currency code)."""
    try:
        dry = json.loads(run(bq_command(project, export, dry_run=True)))
        scanned = int(field(dry, "statistics", "totalBytesProcessed"))
        if scanned > MAX_BYTES_BILLED:
            return unjudged(
                f"the query would scan {scanned} bytes, above the cap of {MAX_BYTES_BILLED}; "
                "not run"
            )
        row = cost_row(run(bq_command(project, export)))
    except (OSError, subprocess.SubprocessError) as failed:
        return unjudged(f"the cost query failed (bq {tool_failure(failed)})")
    except (KeyError, TypeError, ValueError) as failed:
        shape = failed if isinstance(failed, Shape) else type(failed).__name__
        return unjudged(f"the cost query answered unexpectedly ({shape})")
    if row.all_rows == 0:
        return unjudged(
            "the billing export holds no rows for the lab project, so its cost cannot be read "
            "from it; check that the export is live"
        )
    problem = currency_problem(row)
    if problem is not None or row.currency is None:
        stop = "; their summed cost is above zero: stop GCP work" if row.cost > 0 else ""
        return unjudged(f"{problem or 'no currency'}, so the cost has no one unit{stop}")
    unit = row.currency
    cents = row.cost.quantize(Decimal("0.01"))
    shown = f"{cents} {unit}" if row.cost == cents else f"{cents} {unit} (exactly {row.cost})"
    fresh = (
        f"{row.month_rows} rows this month" if row.month_rows else "no rows yet this month"
    ) + f", newest export {row.newest or 'unknown'}"
    if row.cost > 0:
        line = f"FAIL month-to-date cost is {shown}, above 0.00 {unit}: stop GCP work ({fresh})"
        return False, f"cost-check: {line}", unit
    line = (
        f"ok month-to-date cost is {shown} ({fresh}; dry run first: {scanned} bytes, under the "
        f"{MAX_BYTES_BILLED}-byte cap)"
    )
    return True, f"cost-check: {line}", unit


@contextlib.contextmanager
def removed_on_termination(credential: Credential) -> Iterator[None]:
    """A SIGTERM or SIGHUP (a `timeout`, a closed terminal) ends the run through Python's exit, so
    the header file is removed; a SIGKILL cannot be caught, and leaves it, at mode 0600."""
    if credential.injected or threading.current_thread() is not threading.main_thread():
        yield
        return

    def leave(signum: int, frame: object) -> None:
        raise SystemExit(128 + signum)

    previous = {s: signal.signal(s, leave) for s in (signal.SIGTERM, signal.SIGHUP)}
    try:
        yield
    finally:
        for s, handler in previous.items():
            signal.signal(s, handler)


def check(
    env: Mapping[str, str], run: Runner = run_tool, pause: Callable[[float], None] = time.sleep
) -> tuple[int, str]:
    """The exit code and the lines to print."""
    if foreign_session(env):
        return 1, (
            f"cost-check: NOT CHECKED {FOREIGN} is set, so this session's GCP credential may be "
            "another lab's: no GCP call is made and the lab's cost is unread; run it in the lab's "
            "own environment"
        )
    project = lab_project_or_none(env)
    if project is None:
        return 0, (
            "cost-check: the lab has no GCP project (LEDGER_LAB_GCP_PROJECT unset); spends $0.00"
        )
    export = env.get(EXPORT_VARIABLE, "").strip()
    if not export:
        return 1, (
            f"cost-check: set {EXPORT_VARIABLE} to the view over the billing export that the agent "
            "account reads, as dataset.table in the lab project"
        )
    zero, cost_line, unit = month_to_date(project, export, run)
    credential = Credential(env, run)
    try:
        with removed_on_termination(credential):
            findings = Fences(project, run, declared(), credential, pause).findings()
    finally:
        credential.close()
    lines = [cost_line, *(f.line() for f in findings)]
    failed = [f.fence for f in findings if f.state == "FAIL"]
    unchecked = [f.fence for f in findings if f.state == "NOT CHECKED"]
    ok = sum(f.state == "ok" for f in findings)
    tally = f"{ok} fences ok, {len(unchecked)} not checked" + (
        f" ({', '.join(unchecked)})" if unchecked else ""
    )
    if zero and unit is not None and not failed:
        lines.append(
            f"cost-check: passed: 0.00 {unit} month-to-date for the lab project only (its rows in "
            f"the billing export), not the whole billing account; {tally}"
        )
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
