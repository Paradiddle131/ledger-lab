"""Where the project's nodes land on BigQuery, read from dbt's own parse on a BigQuery target, made
offline (tests/models/bigquery_offline.py): a throwaway profile names an invented project and a
stand-in token, every proxy variable points at a closed port, and `dbt parse` connects to nothing.

The parse is the outcome the metric versions gate's rules are meant to give: each built node of the
project in its folder's route (`ROUTES` in semantics/versions.py) or in the profile's dataset, in
the profile's project, with a routed folder holding its route's tables; and no data test's schema
a product's dataset. The second test applies, to a copy of the project, three edits the gate
refuses (a twin of a metric's model enabled only on BigQuery, a model in a product's folder enabled
only on BigQuery, a schema that renders to none on the local target) and checks the outcome names
each of them, so the check is shown to see what it is for.
"""

import json
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

from semantics import versions
from tests.models import bigquery_offline

REFUSED = 97  # semantics/offline/refusal.py's exit status

PROJECT_ID = "probe-project"
DATASET = "intermediate"
PROFILE = {
    "ledger_lab": {
        "target": "bq",
        "outputs": {
            "bq": {
                "type": "bigquery",
                "method": "oauth-secrets",
                "token": "stand-in",
                "project": PROJECT_ID,
                "dataset": DATASET,
                "location": "US",
                "threads": 1,
            }
        },
    }
}
BUILT = ("model", "seed", "snapshot")


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
        shutil.copyfile(versions.REPO / name, into / name)
    return into / "models"


def bigquery_parse(project: Path, work: Path) -> dict:
    """The manifest dbt writes for `project` on the throwaway BigQuery target."""
    reason = bigquery_offline.unavailable()
    if reason:
        pytest.skip(reason)
    profiles = work / "profiles"
    profiles.mkdir()
    (profiles / "profiles.yml").write_text(yaml.safe_dump(PROFILE))
    paths = {"DBT_TARGET_PATH": str(work / "target"), "DBT_LOG_PATH": str(work / "logs")}
    parsed = bigquery_offline.run(
        "dbt",
        "parse",
        "--no-partial-parse",
        "--no-use-colors",
        "--project-dir",
        str(project),
        "--profiles-dir",
        str(profiles),
        extra=paths,
    )
    assert parsed.returncode == 0, parsed.stdout[-2000:] + parsed.stderr[-2000:]
    return json.loads((work / "target" / "manifest.json").read_text())


def misplaced(manifest: dict) -> list[str]:
    """Each node of the project that BigQuery's parse enables and that lands outside the routing,
    and each routed folder whose relations are not its route's tables."""
    found: list[str] = []
    held: dict[str, list[str]] = {folder: [] for folder in versions.ROUTES}
    products = {route.dataset for route in versions.ROUTES.values()}
    for node in manifest["nodes"].values():
        if node["package_name"] != "ledger_lab":
            continue
        where = f"{node['resource_type']} {node['name']} ({node['original_file_path']})"
        if node["resource_type"] == "test":
            if node["schema"] in products:
                found.append(f"{where} stores failures in {node['schema']}")
            continue
        if node["resource_type"] not in BUILT:
            continue
        expected = versions.route(node["original_file_path"]) or DATASET
        if node["schema"] != expected or node["database"] != PROJECT_ID:
            found.append(f"{where} lands in {node['database']}.{node['schema']}")
        folder = versions.folder_of(node["original_file_path"])
        if folder is not None:
            held[folder].append(node["alias"])
    for folder, relations in held.items():
        if sorted(relations) != sorted(versions.ROUTES[folder].tables):
            found.append(f"{folder} holds {sorted(relations)}")
    return found


def test_the_offline_environment_refuses_the_network() -> None:
    """A name lookup in the group's environment ends the process with the refusal's status."""
    reason = bigquery_offline.unavailable()
    if reason:
        pytest.skip(reason)
    probe = "import socket; socket.getaddrinfo('example.com', 443)"
    looked_up = bigquery_offline.run("python", "-c", probe)
    assert looked_up.returncode == REFUSED, looked_up.stderr[-500:]
    assert "offline: refused a network call" in looked_up.stderr


def test_the_project_lands_where_the_routing_says_on_bigquery(tmp_path: Path) -> None:
    manifest = bigquery_parse(copy_project(tmp_path / "tree"), tmp_path)
    assert misplaced(manifest) == []
    landed = {
        n["name"]: n["schema"]
        for n in manifest["nodes"].values()
        if n["resource_type"] == "model" and n["package_name"] == "ledger_lab"
    }
    assert landed["group_totals"] == landed["group_totals_left_out"] == "group_totals"
    assert landed["payments"] == landed["budget_against_outturn"] == DATASET


GROUP_TOTALS = "models/models/marts/group_totals/group_totals.sql"


def edit(path: str, change: Callable[[str], str]) -> Callable[[Path], None]:
    def apply(tree: Path) -> None:
        (tree / path).parent.mkdir(parents=True, exist_ok=True)
        before = (tree / path).read_text() if (tree / path).exists() else ""
        (tree / path).write_text(change(before))

    return apply


GATE_REFUSES = [
    edit(
        GROUP_TOTALS,
        lambda t: t.replace(
            "\nselect", "\n{{ config(enabled=(target.type != 'bigquery')) }}\nselect", 1
        ),
    ),
    edit(
        "models/models/scratch/group_totals.sql",
        lambda _: (
            "{{ config(enabled=(target.type == 'bigquery'), schema='payments') }}\n"
            + (versions.REPO / GROUP_TOTALS).read_text()
        ),
    ),
    edit(
        "models/models/marts/group_totals/payment_rows.sql",
        lambda _: (
            "{{ config(enabled=(target.type == 'bigquery')) }}\n"
            "select * from {{ ref('payments') }}\n"
        ),
    ),
    edit(
        "models/models/intermediate/chicago/_chicago__models.yml",
        lambda t: t.replace(
            "  - name: int_chicago__payments\n",
            "  - name: int_chicago__payments\n    config: {schema: \"{{ 'payments' if"
            " target.type == 'bigquery' else none }}\"}\n",
            1,
        ),
    ),
]


def test_the_outcome_check_names_what_the_gate_refuses(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    project = copy_project(tree)
    for change in GATE_REFUSES:
        change(tree)
    found = misplaced(bigquery_parse(project, tmp_path))
    assert any("group_totals (models/scratch/group_totals.sql) lands in" in f for f in found)
    assert any(f.endswith(f"{PROJECT_ID}.payments") and "scratch" in f for f in found)
    assert any(
        f.startswith("models/marts/group_totals holds") and "payment_rows" in f for f in found
    )
    assert any("int_chicago__payments" in f and f.endswith(f"{PROJECT_ID}.payments") for f in found)
