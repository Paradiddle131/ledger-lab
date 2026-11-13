"""`make cost-check` calls no GCP tool without a lab project, and fails on any cost above zero.

Stub `gcloud`, `bq` and `gsutil` go first on `PATH`; each records its arguments, so a test can
assert which, if any, ran. No real GCP tool runs in these tests.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from cost_check import bq_command, check, month_to_date_sql

REPO = Path(__file__).resolve().parents[2]
TOOLS = ("gcloud", "bq", "gsutil")


def stubs(directory: Path, bq_output: str = "[]") -> Path:
    """Write the stub tools; return the call log they append to."""
    log = directory / "calls.log"
    directory.joinpath("bq-output.json").write_text(bq_output)
    for tool in TOOLS:
        stub = directory / tool
        stub.write_text(
            "#!/bin/sh\n"
            f'printf "%s %s\\n" {tool} "$*" >> "{log}"\n'
            + (f'cat "{directory}/bq-output.json"\n' if tool == "bq" else "")
        )
        stub.chmod(0o755)
    return log


def cost_check(tmp_path: Path, bq_output: str = "[]", **env: str) -> tuple[int, str, str]:
    log = stubs(tmp_path, bq_output)
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


def test_with_a_lab_project_it_reads_the_export_for_that_project_only(tmp_path: Path) -> None:
    code, out, calls = cost_check(
        tmp_path,
        '[{"cost": "0"}]',
        LEDGER_LAB_GCP_PROJECT="the-lab",
        LEDGER_LAB_BILLING_EXPORT="billing.export_v1",
        GCP_PROJECT="another-lab",
    )
    assert code == 0, out
    assert "$0.00" in out
    assert calls.startswith("bq --project_id=the-lab query")
    assert "another-lab" not in calls
    assert "--maximum_bytes_billed=" in calls
    assert [line.split()[0] for line in calls.splitlines()] == ["bq"]


def test_any_cost_above_zero_fails(tmp_path: Path) -> None:
    code, out, _ = cost_check(
        tmp_path,
        '[{"cost": "0.01"}]',
        LEDGER_LAB_GCP_PROJECT="the-lab",
        LEDGER_LAB_BILLING_EXPORT="billing.export_v1",
    )
    assert code != 0
    assert "$0.01" in out
    assert "above $0.00" in out


def test_a_lab_project_without_an_export_table_fails_before_any_call(tmp_path: Path) -> None:
    code, out, calls = cost_check(tmp_path, LEDGER_LAB_GCP_PROJECT="the-lab")
    assert code != 0
    assert "LEDGER_LAB_BILLING_EXPORT" in out
    assert calls == ""


def test_credits_are_netted_and_an_empty_month_is_zero() -> None:
    env = {"LEDGER_LAB_GCP_PROJECT": "the-lab", "LEDGER_LAB_BILLING_EXPORT": "b.t"}
    assert check(env, lambda _: "[]") == (0, "cost-check: month-to-date cost is $0.00")
    net = check(env, lambda _: '[{"cost": -0.0}]')
    assert net[0] == 0


@pytest.mark.parametrize("export", ["billing", "a.b.c", "b.t`; DROP", ""])
def test_an_export_name_that_is_not_dataset_table_is_refused(export: str) -> None:
    with pytest.raises(ValueError, match="dataset.table"):
        month_to_date_sql("the-lab", export)


def test_a_project_id_that_is_not_one_is_refused() -> None:
    with pytest.raises(ValueError, match="project id"):
        month_to_date_sql("the-lab`; x", "billing.export_v1")


def test_the_query_is_capped_and_scoped_to_the_project() -> None:
    command = bq_command("the-lab", "billing.export_v1", "1000")
    assert "--maximum_bytes_billed=1000" in command
    assert "--parameter=project:STRING:the-lab" in command
    assert "`the-lab.billing.export_v1`" in command[-1]
