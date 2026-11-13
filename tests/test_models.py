"""The dbt project builds on the local engine; its profile picks the engine from the environment.

The calendar rules (one period per day, contiguous periods, the UK year's boundaries, a status
for every period) are dbt data tests in `models/`; these tests run them and check they ran.
The build runs with the lab project variable removed and the local target named, so this test
never reaches BigQuery whatever the session's environment holds.
"""

import csv
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import duckdb
import jinja2
import pytest
import yaml

PROJECT = Path(__file__).resolve().parents[1] / "models"
DBT = Path(sys.executable).with_name("dbt")
with (PROJECT / "seeds" / "calendars" / "fiscal_year_starts.csv").open() as starts:
    ENTITIES = {row["entity_id"] for row in csv.DictReader(starts)}


def target_for(env: dict[str, str]) -> str:
    """Render the profile's target line the way dbt does, against the given environment."""
    profile = yaml.safe_load((PROJECT / "profiles.yml").read_text())["ledger_lab"]
    template = jinja2.Environment(autoescape=False).from_string(profile["target"])  # noqa: S701
    return template.render(env_var=lambda name, default="": env.get(name, default))


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({}, "local"),
        ({"GCP_PROJECT": "another-lab"}, "local"),
        ({"LEDGER_LAB_GCP_PROJECT": ""}, "local"),
        ({"LEDGER_LAB_GCP_PROJECT": "the-lab"}, "bigquery"),
    ],
)
def test_the_profile_picks_the_engine_from_the_lab_project_variable(
    env: dict[str, str], expected: str
) -> None:
    assert target_for(env) == expected


def test_the_bigquery_target_caps_every_job() -> None:
    outputs = yaml.safe_load((PROJECT / "profiles.yml").read_text())["ledger_lab"]["outputs"]
    assert "maximum_bytes_billed" in outputs["bigquery"]
    assert "LEDGER_LAB_GCP_PROJECT" in outputs["bigquery"]["project"]


@dataclass(frozen=True)
class Build:
    stdout: str
    database: Path
    results: dict[str, str]


@pytest.fixture(scope="module")
def build(tmp_path_factory: pytest.TempPathFactory) -> Build:
    """Seed, build and test the whole project once on the local engine."""
    assert DBT.is_file(), "dbt is missing: run `uv sync`"
    tmp_path = tmp_path_factory.mktemp("dbt")
    database = tmp_path / "ledger_lab.duckdb"
    env = {k: v for k, v in os.environ.items() if k != "LEDGER_LAB_GCP_PROJECT"}
    env |= {
        "LEDGER_LAB_DUCKDB_PATH": str(database),
        "DBT_TARGET_PATH": str(tmp_path / "target"),
        "DBT_LOG_PATH": str(tmp_path / "logs"),
        "DBT_SEND_ANONYMOUS_USAGE_STATS": "false",
    }
    result = subprocess.run(
        [str(DBT), "build", "--project-dir", str(PROJECT), "--profiles-dir", str(PROJECT)]
        + ["--target", "local"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    run = json.loads((tmp_path / "target" / "run_results.json").read_text())
    results = {r["unique_id"].split(".")[2]: r["status"] for r in run["results"]}
    return Build(result.stdout, database, results)


def query(build: Build, sql: str, *params: object) -> list[tuple[Any, ...]]:
    with duckdb.connect(str(build.database), read_only=True) as db:
        return db.execute(sql, list(params)).fetchall()


def test_every_model_seed_and_data_test_passes_on_the_local_engine(build: Build) -> None:
    assert "PASS=31 WARN=0 ERROR=0 SKIP=0" in build.stdout


def test_the_smoke_model_runs_the_models_dialect(build: Build) -> None:
    rows = query(build, "select entity_id, amount, posted_month from smoke order by entity_id")
    assert rows == [(1, 250, date(2025, 5, 1)), (2, 75, date(2025, 4, 1))]


@pytest.mark.parametrize(
    "rule",
    [
        "no_date_maps_to_two_periods",
        "periods_are_contiguous",
        "uk_year_runs_april_to_march",
        "calendar_year_entities_match_the_calendar",
        "period_status_names_each_period_once",
        "not_null_fiscal_periods_status",
    ],
)
def test_each_calendar_rule_ran_and_passed(build: Build, rule: str) -> None:
    assert build.results.get(rule) == "pass"


@pytest.mark.parametrize(
    ("day", "year", "period"),
    [
        (date(2024, 3, 31), 2023, 12),
        (date(2024, 4, 1), 2024, 1),
        (date(2025, 3, 31), 2024, 12),
    ],
)
def test_a_uk_year_is_labelled_by_the_calendar_year_it_opens_in(
    build: Build, day: date, year: int, period: int
) -> None:
    rows = query(
        build,
        "select fiscal_year, fiscal_period from fiscal_calendar "
        "where entity_id = 'uk-transport' and calendar_date = ?",
        day,
    )
    assert rows == [(year, period)]


def test_the_calendars_cover_the_fiscal_years_the_project_names(build: Build) -> None:
    years = yaml.safe_load((PROJECT / "dbt_project.yml").read_text())["vars"]["fiscal_years"]
    rows = query(
        build,
        "select entity_id, min(fiscal_year), max(fiscal_year) from fiscal_periods "
        "group by entity_id order by entity_id",
    )
    assert rows == [(e, years["first"], years["last"]) for e in sorted(ENTITIES)]


def test_the_calendars_use_the_entity_ids_of_the_access_mapping(access: dict[str, Any]) -> None:
    assert {e["id"] for e in access["entities"]} == ENTITIES
