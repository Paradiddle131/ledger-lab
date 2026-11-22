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
        return versions.bigquery_refusals(child, work, lambda: versions.start_bigquery(tree, work))
    finally:
        versions.end(child)


CLOCK_STEP = Path(__file__).resolve().parent / "clock_step.py"


def stepped(monkeypatch: pytest.MonkeyPatch, count: Path, runs: int) -> None:
    """The BigQuery parses run through `clock_step.py`: the first `runs` of them leave each
    semantic model's `node_relation` unset, as a backward step of the clock does."""
    stand_in = ("python", str(CLOCK_STEP), str(count), str(runs), "bigquery")
    monkeypatch.setattr(
        versions,
        "bigquery_command",
        lambda project, work: versions.offline_command(*stand_in, str(project), str(work)),
    )


def parses(count: Path) -> int:
    return len(count.read_text().splitlines()) if count.exists() else 0


def test_the_bigquery_parse_runs_once_more_after_an_unset_node_relation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    count = tmp_path / "runs"
    stepped(monkeypatch, count, 1)
    assert outcome(copy_project(tmp_path / "tree"), tmp_path / "bigquery") == []
    assert parses(count) == 2


def test_the_bigquery_parse_fails_another_parse_error_after_one_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    count = tmp_path / "runs"
    stepped(monkeypatch, count, 0)
    tree = copy_project(tmp_path / "tree")
    (tree / "models" / "models" / "probe.sql").write_text("select 1 as a {{\n")
    with pytest.raises(versions.Unreadable) as raised:
        outcome(tree, tmp_path / "bigquery")
    assert parses(count) == 1
    message = str(raised.value)
    assert message.startswith("dbt could not parse the working tree on a BigQuery target (")
    assert "second parse" not in message


def test_the_bigquery_parse_fails_when_its_second_run_leaves_node_relation_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    count = tmp_path / "runs"
    stepped(monkeypatch, count, 2)
    with pytest.raises(versions.Unreadable) as raised:
        outcome(copy_project(tmp_path / "tree"), tmp_path / "bigquery")
    assert parses(count) == 2
    assert str(raised.value) == (
        "dbt could not parse the working tree on a BigQuery target (ValidationError), on a second"
        " parse after the first left a semantic model's node_relation unset"
    )


def test_the_offline_environment_refuses_the_network() -> None:
    """A name lookup in the group's environment ends the process with the refusal's status."""
    probe = "import socket; socket.getaddrinfo('example.com', 443)"
    looked_up = versions.offline_run("python", "-c", probe)
    assert versions.group_failure(looked_up.stderr) is None, looked_up.stderr[-500:]
    assert looked_up.returncode == REFUSED, looked_up.stderr[-500:]
    assert "offline: refused a network call" in looked_up.stderr


def test_the_parse_runs_in_the_bigquery_group_alone() -> None:
    """The parse's Python is the group's own environment's, which holds dbt-bigquery and none of
    the project's `dev` group (dbt-duckdb, pytest), so the two dbt installs never mix."""
    probe = (
        "import importlib.util as u, sys; print(sys.prefix,"
        " *(u.find_spec(m) is not None for m in ('dbt.adapters.bigquery', 'dbt.adapters.duckdb',"
        " 'pytest')))"
    )
    found = versions.offline_run("python", "-c", probe)
    assert found.returncode == 0, found.stderr[-500:]
    assert found.stdout.split() == [str(versions.bigquery_environment()), "True", "False", "False"]


def test_the_project_lands_where_the_routing_says_on_bigquery(tmp_path: Path) -> None:
    work = tmp_path / "bigquery"
    assert outcome(copy_project(tmp_path / "tree"), work) == []
    landed = json.loads((work / "report.json").read_text())["landed"]
    project, _ = versions.PROBES[0]
    product, profile = [project, "group_totals"], [project, "intermediate"]
    assert landed["model group_totals"] == landed["model group_totals_left_out"] == product
    assert landed["model payments"] == landed["model budget_against_outturn"] == profile
    tests = [where for name, where in landed.items() if name.startswith("test ")]
    assert tests
    assert all(where == [project, versions.TEST_SCHEMA] for where in tests)


def test_each_probe_is_the_projects_bigquery_output_but_for_what_the_environment_gives(
    tmp_path: Path,
) -> None:
    """Each probe's profile keeps the real BigQuery output's name, as its target, and every
    literal setting; only the method, token, project and region are its own, and the two probes
    differ in project and region, the first region being the lab's (platform/gcp/variables.tf)."""
    tree = copy_project(tmp_path / "tree")
    real = yaml.safe_load((tree / "models" / "profiles.yml").read_text())["ledger_lab"]
    variables = (versions.REPO / "platform" / "gcp" / "variables.tf").read_text()
    lab_region = variables.split('variable "region"')[1].split('default     = "')[1].split('"')[0]
    (projects, regions) = zip(*versions.PROBES, strict=True)
    assert len(set(projects)) == len(set(regions)) == len(versions.PROBES) == 2
    assert regions[0] == lab_region
    for project, region in versions.PROBES:
        profile = versions.probe_profile(tree, project, region)["ledger_lab"]
        assert profile["target"] == "bigquery"
        probe = profile["outputs"]["bigquery"]
        own = {"method", "token", "project", "location"}
        assert {k: v for k, v in probe.items() if k not in own} == {
            k: v for k, v in real["outputs"]["bigquery"].items() if k not in own
        }
        assert (probe["project"], probe["location"]) == (project, region)


def test_the_gate_refuses_when_the_bigquery_group_is_not_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With uv's cache empty, the outcome check cannot run, and the gate says how to cache it."""
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "empty-cache"))
    with pytest.raises(versions.GateError) as refused:
        outcome(copy_project(tmp_path / "tree"), tmp_path / "bigquery")
    assert str(refused.value) == versions.NOT_CACHED_ADVICE
    assert "make sync" in str(refused.value)


def test_only_an_uncached_group_is_the_groups_failure() -> None:
    """uv's offline refusal is read as the group not being cached; any other failure, an adapter
    that does not import among them, is dbt's, which the gate reports by its error class."""
    assert versions.group_failure(f"error: {versions.NOT_CACHED}") == versions.NOT_CACHED_ADVICE
    assert versions.group_failure("ModuleNotFoundError: No module named 'dbt'") is None
