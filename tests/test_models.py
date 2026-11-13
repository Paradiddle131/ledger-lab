"""The dbt project builds on the local engine; its profile picks the engine from the environment.

The build runs with the lab project variable removed and the local target named, so this test
never reaches BigQuery whatever the session's environment holds.
"""

import os
import subprocess
import sys
from datetime import date
from pathlib import Path

import duckdb
import jinja2
import pytest
import yaml

PROJECT = Path(__file__).resolve().parents[1] / "models"
DBT = Path(sys.executable).with_name("dbt")


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


def test_the_smoke_model_builds_on_the_local_engine(tmp_path: Path) -> None:
    assert DBT.is_file(), "dbt is missing: run `uv sync`"
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
    assert "PASS=3" in result.stdout

    with duckdb.connect(str(database), read_only=True) as db:
        rows = db.execute(
            "select entity_id, amount, posted_month from smoke order by entity_id"
        ).fetchall()
    assert rows == [(1, 250, date(2025, 5, 1)), (2, 75, date(2025, 4, 1))]
