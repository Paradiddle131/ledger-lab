"""Building the dbt project on the local engine, over the committed samples or the real landing.

The build runs with the lab project variable removed and the local target named, so it never
reaches BigQuery whatever the session's environment holds. The `source_rows` var picks what every
model reading a fetched source reads: `sample`, the committed seeds (the vendor sample's names and
register records among them), or `landed`, the files under the landing path.
"""

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb

from semantics.versions import node_relation_unset, once_more

PROJECT = Path(__file__).resolve().parents[2] / "models"
DBT = Path(sys.executable).with_name("dbt")
SAMPLE = Path(__file__).with_name("vendor_sample.yaml")
SEEDS = PROJECT / "seeds" / "intermediate" / "vendors"
# A hashed vendor as the landing writes one, among the seeds: it must never match.
HASHED = "person:" + "0" * 64


@dataclass(frozen=True)
class Build:
    stdout: str
    database: Path
    results: dict[str, str]
    env: dict[str, str]
    cwd: Path


SECOND_RUN = (
    "[dbt was run a second time: its first run failed leaving a semantic model's node_relation"
    " unset; the output below is the second run's]\n"
)


def failed_on_unset_node_relation(result: subprocess.CompletedProcess[str]) -> bool:
    return result.returncode != 0 and node_relation_unset(result.stdout + result.stderr)


def run_dbt(command: list[str], env: dict[str, str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """A dbt command, run once more when it fails on a semantic model's unset `node_relation`
    (`semantics/versions.py`); a second run that fails has `SECOND_RUN` before its output."""

    def run() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            command, capture_output=True, text=True, check=False, env=env, cwd=cwd
        )

    result, second = once_more(run, failed_on_unset_node_relation)
    if second and result.returncode != 0:
        result.stdout = SECOND_RUN + result.stdout
    return result


def dbt(env: dict[str, str], cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    project = ["--project-dir", str(PROJECT), "--profiles-dir", str(PROJECT)]
    return run_dbt([str(DBT), *args, *project, "--target", "local"], env, cwd)


def built(tmp_path: Path, source_rows: str, landing: Path | None = None) -> Build:
    """Seed, build and test the whole project on the local engine, reading `source_rows`."""
    assert DBT.is_file(), "dbt is missing: run `uv sync`"
    database = tmp_path / "ledger_lab.duckdb"
    env = {k: v for k, v in os.environ.items() if k != "LEDGER_LAB_GCP_PROJECT"}
    env |= {
        "LEDGER_LAB_DUCKDB_PATH": str(database),
        "DBT_TARGET_PATH": str(tmp_path / "target"),
        "DBT_LOG_PATH": str(tmp_path / "logs"),
        "DBT_SEND_ANONYMOUS_USAGE_STATS": "false",
    }
    if landing is not None:
        env["LEDGER_LAB_LANDING"] = str(landing)
    result = dbt(env, tmp_path, "build", "--vars", json.dumps({"source_rows": source_rows}))
    assert result.returncode == 0, result.stdout + result.stderr
    run = json.loads((tmp_path / "target" / "run_results.json").read_text())
    # A node's name is its id's third part; a unit test's is its fourth, after its model's.
    results = {
        r["unique_id"].split(".")[3 if r["unique_id"].startswith("unit_test.") else 2]: r["status"]
        for r in run["results"]
    }
    return Build(result.stdout, database, results, env, tmp_path)


def query(build: Build, sql: str, *params: object) -> list[tuple[Any, ...]]:
    with duckdb.connect(str(build.database), read_only=True) as db:
        return db.execute(sql, list(params)).fetchall()
