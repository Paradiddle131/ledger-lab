"""Building the dbt project on the local engine, over a fixture landing or the real one.

The build runs with the lab project variable removed and the local target named, so it never
reaches BigQuery whatever the session's environment holds. The landing it reads holds the labelled
vendor sample (`vendor_sample.yaml`) as landed vendor names, with a hashed name beside them, and the
sample's register records as the landed register.
"""

import json
import os
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb
import yaml

from ingest.gleif.legal_entities import (
    CATEGORY,
    COLUMNS,
    COUNTRY,
    LEGAL_FORM,
    LEGAL_NAME,
)

PROJECT = Path(__file__).resolve().parents[2] / "models"
DBT = Path(sys.executable).with_name("dbt")
SAMPLE = Path(__file__).with_name("vendor_sample.yaml")
STATUS = "Entity.EntityStatus"
# A hashed vendor as the landing writes one: it must never match.
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


def sample() -> dict[str, Any]:
    return yaml.safe_load(SAMPLE.read_text())


def write_parquet(
    path: Path, columns: tuple[str, ...], rows: Sequence[Sequence[str | None]]
) -> None:
    con = duckdb.connect()
    names = ", ".join(f'"{c}" varchar' for c in columns)
    con.execute(f"create table t ({names})")
    if rows:
        con.executemany(f"insert into t values ({', '.join('?' for _ in columns)})", rows)  # noqa: S608
    con.execute(f"copy t to '{path}' (format parquet)")


def fixture_landing(path: Path) -> Path:
    """A landing holding the sample's vendor names and register records, in the landed columns
    the models read."""
    path.mkdir(parents=True, exist_ok=True)
    doc = sample()
    by_entity: dict[str, list[str]] = {}
    for vendor in doc["vendors"]:
        by_entity.setdefault(vendor["entity"], []).append(vendor["name"])
    chicago = [(n,) for n in [*by_entity.get("chicago", []), HASHED]]
    write_parquet(path / "chicago-contracts", ("vendor_name",), chicago)
    write_parquet(path / "chicago-payments", ("vendor_name",), chicago[:1])
    uk = [(n,) for n in [*by_entity.get("uk-transport", []), HASHED]]
    write_parquet(path / "uk-transport-spend-2025", ("supplier",), uk)
    at = {c: i for i, c in enumerate(COLUMNS)}
    records = []
    for r in doc["register"]:
        row: list[str | None] = [None] * len(COLUMNS)
        row[at["LEI"]] = r["lei"]
        row[at[LEGAL_NAME]] = r["legal_name"]
        row[at[LEGAL_FORM]] = r["form"]
        row[at[COUNTRY]] = r["country"]
        row[at[CATEGORY]] = r["category"]
        row[at[STATUS]] = r["status"]
        records.append(tuple(row))
    write_parquet(path / "gleif-legal-entities", COLUMNS, records)
    return path


def built(tmp_path: Path, landing: Path) -> Build:
    """Seed, build and test the whole project on the local engine over this landing."""
    assert DBT.is_file(), "dbt is missing: run `uv sync`"
    database = tmp_path / "ledger_lab.duckdb"
    env = {k: v for k, v in os.environ.items() if k != "LEDGER_LAB_GCP_PROJECT"}
    env |= {
        "LEDGER_LAB_DUCKDB_PATH": str(database),
        "LEDGER_LAB_LANDING": str(landing),
        "DBT_TARGET_PATH": str(tmp_path / "target"),
        "DBT_LOG_PATH": str(tmp_path / "logs"),
        "DBT_SEND_ANONYMOUS_USAGE_STATS": "false",
    }
    result = dbt(env, tmp_path, "build")
    assert result.returncode == 0, result.stdout + result.stderr
    run = json.loads((tmp_path / "target" / "run_results.json").read_text())
    results = {r["unique_id"].split(".")[2]: r["status"] for r in run["results"]}
    return Build(result.stdout, database, results, env, tmp_path)


def query(build: Build, sql: str, *params: object) -> list[tuple[Any, ...]]:
    with duckdb.connect(str(build.database), read_only=True) as db:
        return db.execute(sql, list(params)).fetchall()
