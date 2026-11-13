"""`make cost-check`: the lab's month-to-date GCP cost, which must be $0.00.

With no lab project the lab spends nothing by construction: the check says so and calls no GCP
tool. With `LEDGER_LAB_GCP_PROJECT` set it sums this month's cost, credits included, from the
Cloud Billing export table named by `LEDGER_LAB_BILLING_EXPORT` (`dataset.table` in the lab
project), through `bq` with a byte cap, and fails on anything above zero.
"""

import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal

from gcp_project import lab_project_or_none

EXPORT_VARIABLE = "LEDGER_LAB_BILLING_EXPORT"
MAX_BYTES_VARIABLE = "LEDGER_LAB_BQ_MAX_BYTES"
EXPORT_NAME = re.compile(r"^[A-Za-z0-9_]+\.[A-Za-z0-9_]+$")
PROJECT_ID = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")

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


def bq_command(project: str, export: str, max_bytes: str) -> list[str]:
    return [
        "bq",
        f"--project_id={project}",
        "query",
        "--use_legacy_sql=false",
        "--format=json",
        f"--maximum_bytes_billed={max_bytes}",
        f"--parameter=project:STRING:{project}",
        month_to_date_sql(project, export),
    ]


def run_tool(command: Sequence[str]) -> str:
    return subprocess.run(command, capture_output=True, text=True, check=True).stdout  # noqa: S603


def check(env: Mapping[str, str], run: Runner = run_tool) -> tuple[int, str]:
    """The exit code and the line to print."""
    project = lab_project_or_none(env)
    if project is None:
        return 0, (
            "cost-check: the lab has no GCP project (LEDGER_LAB_GCP_PROJECT unset); spends $0.00"
        )
    export = env.get(EXPORT_VARIABLE, "").strip()
    if not export:
        return 1, f"cost-check: set {EXPORT_VARIABLE} to the billing export's dataset.table"
    command = bq_command(project, export, env.get(MAX_BYTES_VARIABLE, "1000000000"))
    rows = json.loads(run(command) or "[]")
    cost = Decimal(str(rows[0]["cost"])) if rows else Decimal(0)
    if cost > 0:
        return 1, f"cost-check: month-to-date cost is ${cost:.2f}, above $0.00: stop GCP work"
    return 0, f"cost-check: month-to-date cost is ${cost:.2f}"


def main() -> int:
    code, line = check(os.environ)
    print(line)
    return code


if __name__ == "__main__":
    sys.exit(main())
