"""The dbt project builds on the local engine; its profile picks the engine from the environment.

The calendar rules (one period per day, contiguous periods, the UK year's boundaries, a status
for every period) are dbt data tests in `models/`; these tests run them and check they ran.
The build (`dbt_build.py`) runs with the lab project variable removed and the local target named, so
these tests never reach BigQuery whatever the session's environment holds.
"""

import csv
import json
import os
import re
import shutil
import subprocess
from datetime import date
from pathlib import Path
from typing import Any

import cost_check
import duckdb
import jinja2
import pytest
import yaml

from ingest import load
from ingest.load import free_regions
from models.contracts import engine, warehouse
from tests.models import bigquery_parity
from tests.models.dbt_build import DBT, PROJECT, Build, dbt, query, run_dbt

GIT = shutil.which("git") or "git"

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
        ({"LEDGER_LAB_GCP_PROJECT": " "}, "local"),
        ({"LEDGER_LAB_GCP_PROJECT": "the-lab"}, "bigquery"),
        ({"LEDGER_LAB_GCP_PROJECT": "the-lab", "GCP_PROJECT": "another-lab"}, "local"),
        ({"LEDGER_LAB_GCP_PROJECT": "the-lab", "GCP_PROJECT": ""}, "bigquery"),
        ({"LEDGER_LAB_GCP_PROJECT": "the-lab", "GCP_PROJECT": " "}, "local"),
    ],
)
def test_the_profile_picks_bigquery_only_in_the_labs_own_environment(
    env: dict[str, str], expected: str
) -> None:
    assert target_for(env) == expected


# Not one of dbt-bigquery's methods (oauth, oauth-secrets, service-account, service-account-json,
# external-oauth-wif): `dbt parse --target bigquery` in another lab's session ends with
# "Credentials in profile ... invalid: 'refused-in-another-labs-session' is not one of [...]".
REFUSED = "refused-in-another-labs-session"
# The access token gcloud sends, which the profile sends too while the environment holds one.
FROM_GCLOUD = "oauth-secrets"


def render(field: str, env: dict[str, str]) -> str:
    """Render one of the bigquery output's fields the way dbt does, against the given
    environment."""
    output = yaml.safe_load((PROJECT / "profiles.yml").read_text())["ledger_lab"]["outputs"]
    template = jinja2.Environment(autoescape=False).from_string(output["bigquery"][field])  # noqa: S701
    return template.render(env_var=lambda name, default="": env.get(name, default))


# A free-tier region the lab's own environment may name.
IN_REGION = {"LEDGER_LAB_BQ_LOCATION": "us-central1"}


@pytest.mark.parametrize(
    ("env", "method"),
    [
        ({"LEDGER_LAB_GCP_PROJECT": "the-lab"} | IN_REGION, "oauth"),
        (
            {"LEDGER_LAB_GCP_PROJECT": "the-lab", "CLOUDSDK_AUTH_ACCESS_TOKEN": "stand-in"}
            | IN_REGION,
            FROM_GCLOUD,
        ),
        ({"LEDGER_LAB_GCP_PROJECT": "the-lab", "GCP_PROJECT": "another-lab"}, REFUSED),
        ({"LEDGER_LAB_GCP_PROJECT": "the-lab", "GCP_PROJECT": " "}, REFUSED),
        (
            {
                "LEDGER_LAB_GCP_PROJECT": "the-lab",
                "GCP_PROJECT": "another-lab",
                "CLOUDSDK_AUTH_ACCESS_TOKEN": "stand-in",
            },
            REFUSED,
        ),
    ],
)
def test_an_explicit_bigquery_target_is_refused_in_another_labs_session(
    env: dict[str, str], method: str
) -> None:
    """dbt checks the method against its own list when it reads the profile, so a method outside
    it stops `--target bigquery` before any connection."""
    assert render("method", env) == method


# Not one of dbt-bigquery's methods either: the method while the location is not a free-tier region.
OUT_OF_REGION = "refused-outside-the-free-tier-regions"


@pytest.mark.parametrize(
    "where",
    [
        None,
        "",
        " ",
        "US",
        "us",
        "EU",
        "US-CENTRAL1",
        "Us-Central1",
        "us-east4",
        "europe-west2",
        "us-central1,us-east1",
        "us central1",
    ],
)
@pytest.mark.parametrize("token", [{}, {"CLOUDSDK_AUTH_ACCESS_TOKEN": "stand-in"}])
def test_the_bigquery_method_is_a_refused_one_outside_the_free_tier_regions(
    where: str | None, token: dict[str, str]
) -> None:
    """Unset, empty, a multi-region, another case or a region outside the list: the method is one
    dbt refuses when it reads the profile, while the default target is still `bigquery`."""
    env = {"LEDGER_LAB_GCP_PROJECT": "the-lab"} | token
    if where is not None:
        env["LEDGER_LAB_BQ_LOCATION"] = where
    assert target_for(env) == "bigquery"
    assert render("method", env) == OUT_OF_REGION


@pytest.mark.parametrize("where", ["us-central1", " us-central1 ", "\tus-east1\n", "us-west1"])
def test_the_bigquery_method_is_kept_for_a_free_tier_region_trimmed(where: str) -> None:
    env = {"LEDGER_LAB_GCP_PROJECT": "the-lab", "LEDGER_LAB_BQ_LOCATION": where}
    assert render("method", env) == "oauth"
    assert render("location", env) == where.strip()


def test_the_bigquery_location_defaults_to_nothing() -> None:
    assert render("location", {"LEDGER_LAB_GCP_PROJECT": "the-lab"}) == ""


@pytest.mark.parametrize("where", [None, "US", "us-central1"])
def test_another_labs_refused_method_comes_first_at_each_location(where: str | None) -> None:
    env = {"LEDGER_LAB_GCP_PROJECT": "the-lab", "GCP_PROJECT": "another-lab"}
    if where is not None:
        env["LEDGER_LAB_BQ_LOCATION"] = where
    assert render("method", env) == REFUSED


def test_the_profiles_regions_are_the_ones_the_region_validation_allows() -> None:
    """The profile lists the regions itself; the list must equal `platform/gcp/variables.tf`'s,
    as `ingest.load.free_regions` reads it, and each listed region must be taken."""
    output = yaml.safe_load((PROJECT / "profiles.yml").read_text())["ledger_lab"]["outputs"]
    listed = re.findall(r"not in \[([^\]]*)\]", output["bigquery"]["method"])
    assert len(listed) == 1, output["bigquery"]["method"]
    regions = tuple(re.findall(r"'([^']*)'", listed[0]))
    assert regions == free_regions()
    for region in regions:
        env = {"LEDGER_LAB_GCP_PROJECT": "the-lab", "LEDGER_LAB_BQ_LOCATION": region}
        assert render("method", env) == "oauth"


@pytest.mark.parametrize("env", [{}, {"LEDGER_LAB_BQ_DATASET": "scratch"}])
def test_the_bigquery_target_builds_in_the_declared_intermediate_dataset(
    env: dict[str, str],
) -> None:
    """The one dataset, whatever the environment holds, and the one the parity check and the
    warehouse contract run read."""
    assert render("dataset", env) == "intermediate"
    assert bigquery_parity.DATASET == warehouse.DATASET == "intermediate"


def test_the_bigquery_target_sends_the_environments_access_token() -> None:
    assert render("token", {"CLOUDSDK_AUTH_ACCESS_TOKEN": "stand-in"}) == "stand-in"


def test_the_bigquery_target_caps_every_job() -> None:
    outputs = yaml.safe_load((PROJECT / "profiles.yml").read_text())["ledger_lab"]["outputs"]
    cap = outputs["bigquery"]["maximum_bytes_billed"]
    assert isinstance(cap, int) and not isinstance(cap, bool)
    assert 0 < cap <= 1_000_000_000
    assert "LEDGER_LAB_GCP_PROJECT" in outputs["bigquery"]["project"]


def profile_cap() -> int:
    outputs = yaml.safe_load((PROJECT / "profiles.yml").read_text())["ledger_lab"]["outputs"]
    return outputs["bigquery"]["maximum_bytes_billed"]


def test_the_scripts_cap_equals_the_profiles_cap() -> None:
    """The cap the contract test and the parity check give their BigQuery jobs (`CAP`) equals the
    profile's `maximum_bytes_billed`."""
    assert profile_cap() == warehouse.CAP
    assert profile_cap() == bigquery_parity.CAP


def test_the_cost_check_and_the_loads_caps_equal_the_profiles_cap() -> None:
    assert profile_cap() == cost_check.MAX_BYTES_BILLED
    assert profile_cap() == load.MAX_BYTES_BILLED


def compile_inline(tmp_path: Path, sql: str) -> subprocess.CompletedProcess[str]:
    """`dbt compile --inline` on the local target: `dbt` passes `--target local`, which is what
    keeps the compile off BigQuery; the environment is this process's with the four below added."""
    env = dict(os.environ) | {
        "LEDGER_LAB_DUCKDB_PATH": str(tmp_path / "ledger_lab.duckdb"),
        "DBT_TARGET_PATH": str(tmp_path / "target"),
        "DBT_LOG_PATH": str(tmp_path / "logs"),
        "DBT_SEND_ANONYMOUS_USAGE_STATS": "false",
    }
    return dbt(env, tmp_path, "compile", "--inline", sql)


@pytest.mark.parametrize(
    ("text", "compiled"), [("0.05", "select 0.05 as d"), ("-12", "select -12 as d"), ("1e5", None)]
)
def test_exact_decimal_takes_only_a_decimals_digits(
    tmp_path: Path, text: str, compiled: str | None
) -> None:
    """On the local engine the macro writes a decimal's digits as given, and stops the compile
    with its own message for text that is not one, such as an exponent."""
    result = compile_inline(tmp_path, f"select {{{{ exact_decimal('{text}') }}}} as d")
    out = result.stdout + result.stderr
    if compiled is None:
        assert result.returncode != 0, out
        assert f"exact_decimal takes a decimal's digits, not {text}" in out, out
    else:
        assert result.returncode == 0, out
        assert compiled in out, out


@pytest.mark.parametrize(
    ("landing", "refused"), [("gs://the-lab-project-landing", True), ("landing", False)]
)
def test_the_models_refuse_to_read_a_bucket_landing(
    tmp_path: Path, landing: str, refused: bool
) -> None:
    """A model over a landed file compiles against a local landing, and stops at compile time
    when `LEDGER_LAB_LANDING` names the bucket, which the models never read."""
    env = {k: v for k, v in os.environ.items() if k != "LEDGER_LAB_GCP_PROJECT"} | {
        "LEDGER_LAB_LANDING": landing,
        "LEDGER_LAB_DUCKDB_PATH": str(tmp_path / "ledger_lab.duckdb"),
        "DBT_TARGET_PATH": str(tmp_path / "target"),
        "DBT_LOG_PATH": str(tmp_path / "logs"),
        "DBT_SEND_ANONYMOUS_USAGE_STATS": "false",
    }
    vars_ = json.dumps({"source_rows": "landed"})
    result = dbt(env, tmp_path, "compile", "--select", "fx_published_rates", "--vars", vars_)
    out = result.stdout + result.stderr
    assert (result.returncode != 0) == refused, out
    assert ("LEDGER_LAB_LANDING names the landing bucket" in out) == refused, out


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


REPO = PROJECT.parent


def test_a_build_with_the_default_database_path_needs_no_folder_made_first(tmp_path: Path) -> None:
    """A build of one seed on the local target, run from the root of a copy of the tracked files
    under `models/` and `semantics/` with the profile's default database path, needs no folder made
    first: dbt makes the project's target folder, where that path points, before it opens the
    database."""
    clone = tmp_path / "clone"
    listed = subprocess.run(
        [GIT, "-C", str(REPO), "ls-files", "-z", "--cached"] + ["--", "models", "semantics"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    for name in filter(None, listed.split("\0")):
        if (REPO / name).is_file():
            (clone / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO / name, clone / name)
    assert not (clone / "target").exists() and not (clone / "models" / "target").exists()
    unset = {"LEDGER_LAB_GCP_PROJECT", "LEDGER_LAB_DUCKDB_PATH", "DBT_TARGET_PATH", "DBT_LOG_PATH"}
    env = {k: v for k, v in os.environ.items() if k not in unset}
    env["DBT_SEND_ANONYMOUS_USAGE_STATS"] = "false"
    result = run_dbt(
        [str(DBT), "build", "--project-dir", "models", "--profiles-dir", "models"]
        + ["--target", "local", "--select", "fiscal_year_starts"]
        + ["--vars", json.dumps({"source_rows": "sample"})],
        env,
        clone,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (clone / LOCAL_DATABASE).is_file()


def default_database_path() -> str:
    """The local output's database path when the environment names none, rendered as dbt does."""
    output = yaml.safe_load((PROJECT / "profiles.yml").read_text())["ledger_lab"]["outputs"]
    template = jinja2.Environment(autoescape=False).from_string(output["local"]["path"])  # noqa: S701
    return template.render(env_var=lambda name, default="": default)


LOCAL_DATABASE = "models/target/ledger_lab.duckdb"


def test_the_local_database_defaults_to_the_projects_target_folder() -> None:
    assert default_database_path() == LOCAL_DATABASE


def test_the_contract_test_reads_the_database_the_default_build_writes() -> None:
    assert REPO / default_database_path() == engine.BUILD


def test_the_contract_tests_copy_goes_to_a_folder_not_made_yet(tmp_path: Path) -> None:
    build = tmp_path / "build.duckdb"
    with duckdb.connect(str(build)) as db:
        db.execute("create table t as select 1 as x")
    copy = engine.for_the_cli(build, tmp_path / "not-made-yet" / "copy.duckdb")
    assert copy.is_file()
