"""The dbt project builds on the local engine; its profile picks the engine from the environment.

The calendar rules (one period per day, contiguous periods, the UK year's boundaries, a status
for every period) are dbt data tests in `models/`; these tests run them and check they ran.
The build (`dbt_build.py`) runs with the lab project variable removed and the local target named, so
these tests never reach BigQuery whatever the session's environment holds.
"""

import csv
import json
from datetime import date
from typing import Any

import jinja2
import pytest
import yaml

from tests.models.dbt_build import PROJECT, Build, dbt, query

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


def test_every_model_seed_and_data_test_passes_on_the_local_engine(build: Build) -> None:
    assert "WARN=0 ERROR=0 SKIP=0" in build.stdout
    assert build.results
    assert {status for status in build.results.values()} <= {"success", "pass"}


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
        "accepted_values_fiscal_periods_status__open__closed",
        "dates_in_fiscal_calendar_fiscal_periods_period_start",
        "dates_in_fiscal_calendar_fiscal_periods_period_end",
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


def test_a_date_outside_the_calendar_fails_the_date_test(build: Build) -> None:
    """The reusable test reports each date its entity's calendar lacks, and only those."""
    probe = (
        "(select 'chicago' as entity_id, cast('2019-06-30' as date) as posted_on"
        " union all select 'chicago', cast('2024-06-30' as date)"
        " union all select 'uk-transport', cast('2023-03-31' as date)"
        " union all select 'uk-transport', cast('2027-03-31' as date))"
    )
    sql = f"{{{{ test_dates_in_fiscal_calendar(model={probe!r}, column_name='posted_on') }}}}"
    result = dbt(
        build.env,
        build.cwd,
        "show",
        "--inline",
        sql,
        "--output",
        "json",
        "--log-format",
        "json",
        "--quiet",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    shown = [json.loads(line)["data"] for line in result.stdout.splitlines() if line.strip()]
    rows = json.loads(next(d["preview"] for d in shown if "preview" in d))
    assert sorted((r["entity_id"], r["checked_date"]) for r in rows) == [
        ("chicago", "2019-06-30"),
        ("uk-transport", "2023-03-31"),
    ]
