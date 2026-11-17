"""Where the project's nodes land on BigQuery, read by the metric versions gate's own outcome check
(semantics/versions.py `start_bigquery`): dbt-bigquery's parse of the project on a throwaway
profile that names an invented project and a stand-in token, run from uv's cache with the network
refused. The gate's `ROUTING_REFUSED` cases (tests/semantics/test_metric_versions.py) show it
refusing each placement it is for; these show the committed project landing where the routing says,
the environment refusing the network, and the gate refusing when the group is not cached.
"""

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from semantics import versions

REFUSED = 97  # semantics/offline/refusal.py's exit status


def copy_project(into: Path) -> Path:
    """The tracked files of the dbt project and the metric definitions it reads, copied."""
    listed = subprocess.run(
        ["git", "ls-files", "-z", "models", "semantics/metricflow"],  # noqa: S607
        cwd=versions.REPO,
        capture_output=True,
        check=True,
    ).stdout.decode()
    for name in filter(None, listed.split("\0")):
        (into / name).parent.mkdir(parents=True, exist_ok=True)
        (into / name).write_bytes((versions.REPO / name).read_bytes())
    return into


def outcome(tree: Path, work: Path) -> list[str]:
    child = versions.start_bigquery(tree, work)
    try:
        return versions.bigquery_refusals(child, work)
    finally:
        versions.end(child)


def test_the_offline_environment_refuses_the_network() -> None:
    """A name lookup in the group's environment ends the process with the refusal's status."""
    probe = "import socket; socket.getaddrinfo('example.com', 443)"
    looked_up = versions.offline_run("python", "-c", probe)
    assert versions.group_failure(looked_up.stderr) is None, looked_up.stderr[-500:]
    assert looked_up.returncode == REFUSED, looked_up.stderr[-500:]
    assert "offline: refused a network call" in looked_up.stderr


def test_the_project_lands_where_the_routing_says_on_bigquery(tmp_path: Path) -> None:
    work = tmp_path / "bigquery"
    assert outcome(copy_project(tmp_path / "tree"), work) == []
    landed = json.loads((work / "report.json").read_text())["landed"]
    product = [versions.PROBE_PROJECT, "group_totals"]
    profile = [versions.PROBE_PROJECT, versions.PROFILE_DATASET]
    assert landed["model group_totals"] == landed["model group_totals_left_out"] == product
    assert landed["model payments"] == landed["model budget_against_outturn"] == profile
    tests = [where for name, where in landed.items() if name.startswith("test ")]
    assert tests
    assert all(where == [versions.PROBE_PROJECT, versions.TEST_SCHEMA] for where in tests)


def test_the_outcome_checks_profile_names_the_projects_dataset() -> None:
    """The throwaway profile's dataset is the one models/profiles.yml's bigquery output names."""
    profiles = yaml.safe_load((versions.REPO / "models" / "profiles.yml").read_text())
    dataset = profiles["ledger_lab"]["outputs"]["bigquery"]["dataset"]
    assert dataset == versions.PROFILE_DATASET
    assert versions.BIGQUERY_PROFILE["ledger_lab"]["outputs"]["bq"]["dataset"] == dataset


def test_the_gate_refuses_when_the_bigquery_group_is_not_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With uv's cache empty, the outcome check cannot run, and the gate says how to cache it."""
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "empty-cache"))
    with pytest.raises(versions.GateError) as refused:
        outcome(copy_project(tmp_path / "tree"), tmp_path / "bigquery")
    assert str(refused.value) == versions.NOT_CACHED_ADVICE
    assert "make sync" in str(refused.value)
