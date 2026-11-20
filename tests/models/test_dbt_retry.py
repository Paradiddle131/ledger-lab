"""The suite's dbt runs go once more only when dbt fails with a semantic model's `node_relation`
unset, which a backward step of the wall clock during the parse leaves.

A scripted stand-in prints each run's output and exit status; `clock_step.py` runs the real dbt
with the parse's start time put ahead of every node, so the signature is held to dbt's own output.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from semantics.versions import node_relation_unset
from tests.models.dbt_build import PROJECT, SECOND_RUN, run_dbt

CLOCK_STEP = Path(__file__).with_name("clock_step.py")

# dbt's own lines for the failure, as its text log writes them (on stdout) ...
UNSET = (
    "\x1b[0m22:11:26  Running with dbt=1.12.7\n"
    "\x1b[0m22:11:26  Registered adapter: duckdb=1.11.1\n"
    "\x1b[0m22:11:29  [\x1b[31mERROR\x1b[0m]: Encountered an error:\n"
    "1 validation error for PydanticSemanticModel\n"
    "node_relation\n"
    "  none is not an allowed value (type=type_error.none.not_allowed)\n"
)
# ... and as its JSON log writes them, the line breaks escaped.
UNSET_JSON = (
    json.dumps(
        {
            "data": {
                "exc": "1 validation error for PydanticSemanticModel\nnode_relation\n"
                "  none is not an allowed value (type=type_error.none.not_allowed)"
            },
            "info": {"level": "error", "name": "MainEncounteredError"},
        }
    )
    + "\n"
)
DONE = "\x1b[0m22:11:30  Done. PASS=1 WARN=0 ERROR=0 SKIP=0 NO-OP=0 TOTAL=1\n"
OTHER_ERRORS = {
    "another field of the semantic model": (
        "1 validation error for PydanticSemanticModel\nnode_relation\n  field required"
        " (type=value_error.missing)\n"
    ),
    "the same field of another node": (
        "1 validation error for PydanticMetric\nnode_relation\n  none is not an allowed value"
        " (type=type_error.none.not_allowed)\n"
    ),
    "a second error beside it": (
        "2 validation errors for PydanticSemanticModel\nnode_relation\n  none is not an allowed"
        " value (type=type_error.none.not_allowed)\nmeasures\n  field required"
        " (type=value_error.missing)\n"
    ),
    "a ref dbt could not infer": (
        "dbt was unable to infer all dependencies for the model"
        ' "chicago_contract_reconciliation".\n'
        "This typically happens when ref() is placed within a conditional block.\n"
    ),
    "a failed data test": "Failure in test not_null_smoke_amount\nDone. PASS=1 ERROR=1\n",
}

SCRIPTED = """\
import json, sys
from pathlib import Path
plan, count = Path(sys.argv[1]), Path(sys.argv[2])
with count.open("a") as file:
    file.write("run\\n")
n = len(count.read_text().splitlines())
run = json.loads(plan.read_text())[min(n, len(json.loads(plan.read_text()))) - 1]
sys.stdout.write(run["stdout"].replace("{n}", str(n)))
sys.stderr.write(run.get("stderr", ""))
sys.exit(run["status"])
"""


def scripted(tmp_path: Path, *runs: tuple[int, str]) -> tuple[list[str], Path]:
    """A stand-in for dbt whose n-th run prints the n-th output (`{n}` replaced by n) and exits
    with its status, the last one repeating; and the file counting its runs."""
    stub, plan, count = tmp_path / "stub.py", tmp_path / "plan.json", tmp_path / "runs"
    stub.write_text(SCRIPTED)
    plan.write_text(json.dumps([{"status": s, "stdout": out} for s, out in runs]))
    return [sys.executable, str(stub), str(plan), str(count)], count


def runs(count: Path) -> int:
    return len(count.read_text().splitlines()) if count.exists() else 0


@pytest.mark.parametrize("unset", [UNSET, UNSET_JSON], ids=["text log", "JSON log"])
def test_an_unset_node_relation_then_a_pass_passes_on_the_second_run(
    tmp_path: Path, unset: str
) -> None:
    command, count = scripted(tmp_path, (2, unset), (0, "run {n}\n" + DONE))
    result = run_dbt(command, env=dict(os.environ), cwd=tmp_path)
    assert runs(count) == 2
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "run 2\n" + DONE


def test_an_unset_node_relation_on_both_runs_fails_with_the_second_runs_output(
    tmp_path: Path,
) -> None:
    command, count = scripted(tmp_path, (2, "run {n}\n" + UNSET))
    result = run_dbt(command, env=dict(os.environ), cwd=tmp_path)
    assert runs(count) == 2
    assert result.returncode == 2
    assert result.stdout == SECOND_RUN + "run 2\n" + UNSET
    assert "second run" in SECOND_RUN


def test_an_unset_node_relation_on_stderr_is_run_once_more(tmp_path: Path) -> None:
    stub, plan, count = tmp_path / "stub.py", tmp_path / "plan.json", tmp_path / "runs"
    stub.write_text(SCRIPTED)
    plan.write_text(
        json.dumps([{"status": 2, "stdout": "", "stderr": UNSET}, {"status": 0, "stdout": DONE}])
    )
    result = run_dbt([sys.executable, str(stub), str(plan), str(count)], dict(os.environ), tmp_path)
    assert runs(count) == 2
    assert result.returncode == 0


@pytest.mark.parametrize("output", OTHER_ERRORS.values(), ids=OTHER_ERRORS.keys())
def test_another_failure_fails_after_one_run(tmp_path: Path, output: str) -> None:
    command, count = scripted(tmp_path, (1, output), (0, DONE))
    result = run_dbt(command, env=dict(os.environ), cwd=tmp_path)
    assert runs(count) == 1
    assert result.returncode == 1
    assert result.stdout == output


@pytest.mark.parametrize("output", [DONE, UNSET + DONE], ids=["plain", "naming the error"])
def test_a_passing_run_is_run_once(tmp_path: Path, output: str) -> None:
    """A run that exits 0 is not run again, even when its output holds the error's text."""
    command, count = scripted(tmp_path, (0, output), (2, UNSET))
    result = run_dbt(command, env=dict(os.environ), cwd=tmp_path)
    assert runs(count) == 1
    assert result.returncode == 0
    assert result.stdout == output


def local_env(tmp_path: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k != "LEDGER_LAB_GCP_PROJECT"}
    return env | {
        "LEDGER_LAB_DUCKDB_PATH": str(tmp_path / "unused.duckdb"),
        "DBT_TARGET_PATH": str(tmp_path / "target"),
        "DBT_LOG_PATH": str(tmp_path / "logs"),
        "DBT_SEND_ANONYMOUS_USAGE_STATS": "false",
    }


def stepped_parse(tmp_path: Path, stepped: int, *args: str) -> list[str]:
    """`dbt parse` of the project on the local target whose first `stepped` runs leave each
    semantic model's `node_relation` unset."""
    # Isolated (-I): dbt imports every `dbt_` module on the import path as a plugin, which the
    # stand-in's own folder holds.
    stand_in = [sys.executable, "-I", str(CLOCK_STEP), str(tmp_path / "runs"), str(stepped), "dbt"]
    project = ["--project-dir", str(PROJECT), "--profiles-dir", str(PROJECT)]
    return [*stand_in, "parse", *project, "--target", "local", *args]


LOGS = pytest.mark.parametrize("log", [[], ["--log-format", "json"]], ids=["text log", "JSON log"])


@LOGS
def test_dbts_own_unset_node_relation_error_matches_the_signature(
    tmp_path: Path, log: list[str]
) -> None:
    result = subprocess.run(
        stepped_parse(tmp_path, 1, *log),
        capture_output=True,
        text=True,
        check=False,
        env=local_env(tmp_path),
        cwd=tmp_path,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert node_relation_unset(result.stdout + result.stderr), result.stdout + result.stderr


@LOGS
def test_dbt_stepped_on_its_first_run_passes_on_the_second(tmp_path: Path, log: list[str]) -> None:
    result = run_dbt(stepped_parse(tmp_path, 1, *log), local_env(tmp_path), tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert runs(tmp_path / "runs") == 2


def test_dbt_stepped_on_both_runs_fails_after_one_retry(tmp_path: Path) -> None:
    result = run_dbt(stepped_parse(tmp_path, 2), local_env(tmp_path), tmp_path)
    assert runs(tmp_path / "runs") == 2
    assert result.returncode == 2
    assert result.stdout.startswith(SECOND_RUN)
    assert node_relation_unset(result.stdout)


def test_dbt_unstepped_parses_in_one_run(tmp_path: Path) -> None:
    result = run_dbt(stepped_parse(tmp_path, 0), local_env(tmp_path), tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert runs(tmp_path / "runs") == 1
