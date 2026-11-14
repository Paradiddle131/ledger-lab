"""The metric versions: every metric carries one, and the gate that fails `make check` when a
definition changes against `origin/main` under the same version, or a metric loses its version or
its owner (`semantics/versions.py`).

Each case runs the gate itself over a temporary git repository whose `origin/main` holds the
committed dbt project file and metric definitions; the working tree's change is made inside the
test, so every edited definition here is invented for the test.
"""

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from semantics import versions
from tests.models.dbt_build import PROJECT, Build
from tests.semantics.metric_cli import manifest

FOLDER = "semantics/metricflow"
PROJECT_FILE = "models/dbt_project.yml"
REAL = {
    PROJECT_FILE: (PROJECT / "dbt_project.yml").read_text(),
    **{f"{FOLDER}/{p.name}": p.read_text() for p in sorted((versions.REPO / FOLDER).glob("*.yml"))},
}
CONTRACTS = f"{FOLDER}/contracts.yml"
PAYMENTS = f"{FOLDER}/payments.yml"
Block = dict[str, Any]
Edit = Callable[[dict[str, str]], None]


def git_repo(tmp_path: Path, main: dict[str, str] | None) -> Path:
    """A repository whose `origin/main` holds the files `main`, or (None) no `origin/main`; its
    working tree starts as `main`."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git = ["git", "-c", "user.name=test", "-c", "user.email=test@example.com"]
    git += ["-c", "commit.gpgsign=false"]

    def run(*args: str) -> None:
        subprocess.run([*git, *args], cwd=repo, check=True, capture_output=True)

    run("init", "--quiet")
    (repo / "README.md").write_text("a clone\n")
    write(repo, main or {})
    run("add", ".")
    run("commit", "--quiet", "-m", "main")
    if main is not None:
        run("update-ref", "refs/remotes/origin/main", "HEAD")
    return repo


def write(repo: Path, files: dict[str, str]) -> None:
    for path, text in files.items():
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(text)


def working(tmp_path: Path, edit: Edit, main: dict[str, str] = REAL) -> Path:
    """A clone of `main` whose working tree is `main` changed by `edit` (in place, on a copy)."""
    repo = git_repo(tmp_path, main)
    files = dict(main)
    edit(files)
    for path in main.keys() - files.keys():
        (repo / path).unlink()
    write(repo, files)
    return repo


def on(path: str, kind: str, name: str, change: Callable[[Block], None]) -> Edit:
    """An edit of the metric or semantic model `name` in the file at `path`, written back as YAML
    of its own (another key order and folding, which the gate does not count as a change)."""

    def edit(files: dict[str, str]) -> None:
        content = yaml.safe_load(files[path])
        (block,) = [b for b in content[kind] if b["name"] == name]
        change(block)
        files[path] = yaml.safe_dump(content, sort_keys=True)

    return edit


def metric(name: str, change: Callable[[Block], None], path: str = CONTRACTS) -> Edit:
    return on(path, "metrics", name, change)


def meta(**values: object) -> Callable[[Block], None]:
    def change(block: Block) -> None:
        for key, value in values.items():
            if value is None:
                block["config"]["meta"].pop(key)
            else:
                block["config"]["meta"][key] = value

    return change


def measure(model: str, name: str, key: str, value: str, path: str = CONTRACTS) -> Edit:
    def change(block: Block) -> None:
        (found,) = [m for m in block["measures"] if m["name"] == name]
        found[key] = value

    return on(path, "semantic_models", model, change)


def both(*edits: Edit) -> Edit:
    def edit(files: dict[str, str]) -> None:
        for each in edits:
            each(files)

    return edit


def setting(key: str, value: object) -> Callable[[Block], None]:
    return lambda block: block.update({key: value})


def bump(name: str, path: str = CONTRACTS, to: int = 2) -> Edit:
    return metric(name, meta(version=to), path)


A_FILTER = "{{ Dimension('contract__is_break') }} = false"
NEW_EXPR = "case when award_held_to > 1 then award_held_to end"
NEW_DESCRIPTION = "The share of fixed awards paid, said otherwise."
REPEATS = "{{ Dimension('payment__entity_id') }} = 'uk-transport'"


def new_metric(files: dict[str, str]) -> None:
    content = yaml.safe_load(files[CONTRACTS])
    content["metrics"].append(
        {
            "name": "unpaid_award_total",
            "label": "Unpaid award",
            "description": "Not for direct use: invented for a test.",
            "type": "simple",
            "type_params": {"measure": "unpaid_award"},
            "config": {
                "meta": {"owner": "payables-clerk", "term": "award", "role": "input", "version": 1}
            },
        }
    )
    files[CONTRACTS] = yaml.safe_dump(content)


def removed(name: str) -> Edit:
    def edit(files: dict[str, str]) -> None:
        content = yaml.safe_load(files[CONTRACTS])
        content["metrics"] = [m for m in content["metrics"] if m["name"] != name]
        files[CONTRACTS] = yaml.safe_dump(content)

    return edit


def moved(old: str, new: str) -> Edit:
    def edit(files: dict[str, str]) -> None:
        files[new] = files.pop(old)

    return edit


def project_moved(files: dict[str, str]) -> None:
    """The whole dbt project and the metric files moved, with `model-paths` following them."""
    project = files.pop(PROJECT_FILE).replace('"../semantics/metricflow"', '"../metrics"')
    files["dbt/dbt_project.yml"] = project
    for path in [p for p in files if p.startswith(FOLDER)]:
        files[path.replace(FOLDER, "metrics")] = files.pop(path)


def model_paths_cut(files: dict[str, str]) -> None:
    """The metric folder taken out of the project's `model-paths`, as if dbt no longer read it."""
    files[PROJECT_FILE] = files[PROJECT_FILE].replace(', "../semantics/metricflow"', "")


def commented(files: dict[str, str]) -> None:
    files[CONTRACTS] = "# A comment.\n" + files[CONTRACTS].replace(
        "\nmetrics:", "\n# Another.\nmetrics:"
    )


FAILS: dict[str, tuple[Edit, list[str]]] = {
    "a measure's expression changed, no version": (
        measure("chicago_contracts", "fixed_award", "expr", NEW_EXPR),
        ["fixed_award", "spend_against_award"],
    ),
    "a measure changed, only the metric over it bumped": (
        both(
            measure("chicago_contracts", "fixed_award", "expr", NEW_EXPR),
            bump("fixed_award"),
        ),
        ["spend_against_award"],
    ),
    "an input's filter changed, only the input bumped": (
        both(
            metric("chicago_repeat_rows", setting("filter", REPEATS), PAYMENTS),
            bump("chicago_repeat_rows", PAYMENTS),
        ),
        ["duplicate_payment_rate"],
    ),
    "a metric's own filter added, no version": (
        metric("paid_over_award", setting("filter", A_FILTER)),
        ["paid_over_award"],
    ),
    "a metric's description changed, no version": (
        metric("spend_against_award", setting("description", NEW_DESCRIPTION)),
        ["spend_against_award"],
    ),
    "a metric's label changed, no version": (
        metric("spend_against_award", setting("label", "Award spent")),
        ["spend_against_award"],
    ),
    "a metric's role changed, no version": (
        metric("fixed_award", meta(role="headline")),
        ["fixed_award"],
    ),
    "a dimension's expression changed, no version": (
        on(
            PAYMENTS,
            "semantic_models",
            "payments",
            lambda b: b["dimensions"][1].update(expr="lower(entity_id)"),
        ),
        [
            "chicago_payment_amount",
            "chicago_payment_rows",
            "chicago_repeat_amount",
            "chicago_repeat_rows",
            "duplicate_payment_amount_rate",
            "duplicate_payment_rate",
        ],
    ),
    "a semantic model's dbt model changed, no version": (
        on(PAYMENTS, "semantic_models", "payments", setting("model", "ref('payments_v2')")),
        [
            "chicago_payment_amount",
            "chicago_payment_rows",
            "chicago_repeat_amount",
            "chicago_repeat_rows",
            "duplicate_payment_amount_rate",
            "duplicate_payment_rate",
        ],
    ),
}


@pytest.mark.parametrize("name", list(FAILS))
def test_a_changed_definition_under_the_same_version_fails_the_gate(
    tmp_path: Path, name: str
) -> None:
    edit, named = FAILS[name]
    ok, report = versions.check(working(tmp_path, edit))
    assert not ok, report
    lines = [line for line in report.splitlines() if line.startswith("- ")]
    assert lines == [
        f"- metric {m}'s definition changed and its version is still 1" for m in named
    ], report


REFUSED: dict[str, tuple[Edit, str]] = {
    "a version gone": (
        metric("remaining_commitment", meta(version=None)),
        "metric remaining_commitment has no version that is an integer of at least 1",
    ),
    "a version of 0": (
        metric("remaining_commitment", meta(version=0)),
        "metric remaining_commitment has no version that is an integer of at least 1",
    ),
    "a semantic version": (
        metric("remaining_commitment", meta(version="1.1.0")),
        "metric remaining_commitment has no version that is an integer of at least 1",
    ),
    "a version that is true": (
        metric("remaining_commitment", meta(version=True)),
        "metric remaining_commitment has no version that is an integer of at least 1",
    ),
    "an owner gone": (
        metric("remaining_commitment", meta(owner=None)),
        "metric remaining_commitment has no owner",
    ),
    "an owner gone with a change and a new version": (
        metric("remaining_commitment", meta(owner=None, version=2, term="award")),
        "metric remaining_commitment has no owner",
    ),
    "a new metric with no owner": (
        both(new_metric, on(CONTRACTS, "metrics", "unpaid_award_total", meta(owner=None))),
        "metric unpaid_award_total has no owner",
    ),
    "a metric removed": (
        removed("paid_over_award"),
        "metric paid_over_award (origin/main defines it) is gone or renamed",
    ),
    "a metric renamed": (
        metric("paid_over_award", setting("name", "overpaid")),
        "metric paid_over_award (origin/main defines it) is gone or renamed",
    ),
    "a metric file moved out of the model paths": (
        moved(CONTRACTS, "semantics/contracts.yml"),
        "metric spend_against_award (origin/main defines it) is gone or renamed",
    ),
    "the metric folder cut from the model paths": (
        model_paths_cut,
        "metric spend_against_award (origin/main defines it) is gone or renamed",
    ),
    "an unreadable file": (
        lambda files: files.update({CONTRACTS: "metrics: [\n"}),
        f"{CONTRACTS} is unreadable",
    ),
    "a measure that is not defined": (
        metric("remaining_commitment", setting("type_params", {"measure": "nothing"})),
        "metric remaining_commitment cannot be resolved: measure nothing is referenced and not"
        " defined",
    ),
    "an input that is not defined": (
        metric(
            "spend_against_award",
            setting("type_params", {"numerator": "nothing", "denominator": "fixed_award"}),
        ),
        "metric spend_against_award cannot be resolved: metric nothing is referenced and not"
        " defined",
    ),
    "a metric type the gate does not know": (
        metric("remaining_commitment", setting("type", "invented")),
        "metric remaining_commitment cannot be resolved: type 'invented' is not one the gate can"
        " resolve",
    ),
    "a metric defined twice": (
        lambda files: files.update({f"{FOLDER}/again.yml": files[CONTRACTS]}),
        f"{CONTRACTS}: semantic_models chicago_contracts is defined twice",
    ),
}


@pytest.mark.parametrize("name", list(REFUSED))
def test_a_metric_without_a_version_an_owner_or_its_definition_fails_the_gate(
    tmp_path: Path, name: str
) -> None:
    edit, expected = REFUSED[name]
    ok, report = versions.check(working(tmp_path, edit))
    assert not ok, report
    assert expected in report


def test_a_version_that_goes_down_fails_the_gate(tmp_path: Path) -> None:
    main = dict(REAL)
    bump("paid_over_award", to=3)(main)
    ok, report = versions.check(working(tmp_path, bump("paid_over_award", to=2), main))
    assert not ok, report
    assert "metric paid_over_award's version went down from 3 to 2" in report


PASSES: dict[str, Edit] = {
    "no change": lambda files: None,
    "comments, another key order and folding": both(
        commented, metric("remaining_commitment", lambda b: None)
    ),
    "a measure changed, every metric over it bumped": both(
        measure("chicago_contracts", "fixed_award", "expr", NEW_EXPR),
        bump("fixed_award"),
        bump("spend_against_award"),
    ),
    "an input's filter changed, the input and its headline bumped": both(
        metric("chicago_repeat_rows", setting("filter", REPEATS), PAYMENTS),
        bump("chicago_repeat_rows", PAYMENTS),
        bump("duplicate_payment_rate", PAYMENTS),
    ),
    "a description changed with a new version": both(
        metric("spend_against_award", setting("description", NEW_DESCRIPTION)),
        bump("spend_against_award"),
    ),
    "a version raised by more than one": both(
        metric("paid_over_award", setting("filter", A_FILTER)), bump("paid_over_award", to=5)
    ),
    "a version raised with no change": bump("paid_over_award"),
    "an owner changed alone": metric("paid_over_award", meta(owner="group-controller")),
    "a measure's description changed": measure(
        "chicago_contracts", "fixed_award", "description", NEW_DESCRIPTION
    ),
    "an input's description changed, only the input bumped": both(
        metric("fixed_award", setting("description", "Not for direct use: said otherwise.")),
        bump("fixed_award"),
    ),
    "a new metric": new_metric,
    "a metric file moved inside the model paths": moved(CONTRACTS, f"{FOLDER}/awards/awards.yml"),
    "the project and the metric files moved together": project_moved,
}


@pytest.mark.parametrize("name", list(PASSES))
def test_a_change_that_keeps_every_version_passes_the_gate(tmp_path: Path, name: str) -> None:
    ok, report = versions.check(working(tmp_path, PASSES[name]))
    assert ok, report


def test_the_gate_names_the_versions_that_went_up(tmp_path: Path) -> None:
    edit = PASSES["a measure changed, every metric over it bumped"]
    ok, report = versions.check(working(tmp_path, edit))
    assert ok, report
    assert report.endswith(
        "changed with a new version: fixed_award 1 to 2, spend_against_award 1 to 2"
    )


def unversioned(files: dict[str, str]) -> dict[str, str]:
    return {p: t.replace(", version: 1}", "}") for p, t in files.items()}


def test_the_first_versions_pass_against_a_main_without_versions(tmp_path: Path) -> None:
    main = unversioned(REAL)
    assert main != REAL
    repo = working(tmp_path, lambda files: files.update(REAL), main)
    ok, report = versions.check(repo)
    assert ok, report


def test_a_change_without_a_version_fails_against_a_main_without_versions(tmp_path: Path) -> None:
    main = unversioned(REAL)
    edit = metric("paid_over_award", setting("filter", A_FILTER))
    ok, report = versions.check(working(tmp_path, edit, main))
    assert not ok, report
    assert "metric spend_against_award has no version" in report
    assert "metric paid_over_award has no version" in report


def test_the_first_metrics_pass_while_main_defines_none(tmp_path: Path) -> None:
    repo = working(tmp_path, lambda files: files.update(REAL), {PROJECT_FILE: REAL[PROJECT_FILE]})
    ok, report = versions.check(repo)
    assert ok, report
    assert "defines no metric" in report


def test_a_clone_without_main_fails_and_says_how_to_fetch_it(tmp_path: Path) -> None:
    repo = git_repo(tmp_path, None)
    write(repo, REAL)
    with pytest.raises(versions.GateError, match="git fetch origin main"):
        versions.check(repo)


def test_a_metric_can_join_another_semantic_model_through_an_entity() -> None:
    """A dimension of a semantic model reached through a shared entity is part of the definition,
    since a filter or group-by can read it; one with no shared entity is not."""
    models = {
        "spend": {
            "name": "spend",
            "entities": [{"name": "payment", "type": "primary"}],
            "measures": [{"name": "amount", "agg": "sum"}],
        },
        "vendors": {
            "name": "vendors",
            "entities": [{"name": "payment", "type": "foreign"}],
            "dimensions": [{"name": "region", "type": "categorical"}],
        },
        "elsewhere": {"name": "elsewhere", "entities": [{"name": "budget", "type": "primary"}]},
    }
    metrics = {"spent": {"name": "spent", "type": "simple", "type_params": {"measure": "amount"}}}
    resolved = versions.definition("spent", models, metrics)
    assert set(resolved["semantic_models"]) == {"spend", "vendors"}


def test_every_metric_carries_a_version(build: Build) -> None:
    for each in manifest(build)["metrics"]:
        assert each["config"]["meta"].get("version") == 1, each["name"]


def test_the_gate_reads_the_metrics_and_measures_dbt_reads(build: Build) -> None:
    """The gate's reading of the committed tree names the metrics dbt's parse writes, each resolved
    to the measures dbt resolves it to, so the gate is no second reading of the semantic layer."""
    read, problems = versions.read(versions.working_tree(versions.REPO))
    assert problems == []
    parsed = {m["name"]: m for m in manifest(build)["metrics"]}
    assert read.keys() == parsed.keys()
    for name, each in read.items():
        measures = {
            m["name"]
            for model in each.definition["semantic_models"].values()
            for m in model["measures"]
        }
        assert measures == {m["name"] for m in parsed[name]["type_params"]["input_measures"]}, name
