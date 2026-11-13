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


def dbt(env: dict[str, str], cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(DBT), *args, "--project-dir", str(PROJECT), "--profiles-dir", str(PROJECT)]
        + ["--target", "local"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        cwd=cwd,
    )


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
    results = {r["unique_id"].split(".")[2]: r["status"] for r in run["results"]}
    return Build(result.stdout, database, results, env, tmp_path)


def query(build: Build, sql: str, *params: object) -> list[tuple[Any, ...]]:
    with duckdb.connect(str(build.database), read_only=True) as db:
        return db.execute(sql, list(params)).fetchall()
