"""The metric versions: every metric carries one, and the gate that fails `make check` when a
definition changes against `origin/main` under the same version, or a metric loses its version or
its owner (`semantics/versions.py`).

Each case runs the gate over a temporary git repository whose `origin/main` holds the committed dbt
project and metric definitions; the working tree's change is made inside the test, so every edited
definition here is invented for the test. The gate parses both trees with dbt itself, so the cases
that turn on how dbt reads a tree (paths, `.dbtignore`, project configuration, filters, time
spines) run whole; the rules of the comparison alone run on the semantic manifest dbt wrote for the
committed tree, changed in place.
"""

import copy
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from semantics import versions
from tests.models.dbt_build import Build
from tests.semantics.metric_cli import manifest

FOLDER = "semantics/metricflow"
PROJECT_FILE = "models/dbt_project.yml"
GATE_FILES = ["semantics/versions.py", "semantics/offline/refusal.py"]


def tracked(*paths: str) -> dict[str, str]:
    listed = subprocess.run(
        ["git", "ls-files", "-z", *paths],  # noqa: S607
        cwd=versions.REPO,
        capture_output=True,
        check=True,
    ).stdout.decode()
    return {p: (versions.REPO / p).read_text() for p in listed.split("\0") if p}


# The committed dbt project, its metric definitions and the gate itself.
REAL = tracked("models", FOLDER, *GATE_FILES)
CONTRACTS = f"{FOLDER}/contracts.yml"
PAYMENTS = f"{FOLDER}/payments.yml"
CALENDARS = "models/models/calendars/calendars.yml"
Block = dict[str, Any]
Edit = Callable[[dict[str, str]], None]
GIT = ["git", "-c", "user.name=test", "-c", "user.email=test@example.com"]
GIT += ["-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main"]


def run(repo: Path, *args: str) -> str:
    done = subprocess.run([*GIT, *args], cwd=repo, check=True, capture_output=True, text=True)
    return done.stdout


def git_repo(tmp_path: Path, main: dict[str, str] | None) -> Path:
    """A repository whose `origin/main` holds the files `main`, or (None) no `origin/main`; its
    working tree starts as `main`."""
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    run(repo, "init", "--quiet")
    (repo / "README.md").write_text("a clone\n")
    write(repo, main or {})
    commit(repo, "main")
    if main is not None:
        run(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    return repo


def commit(repo: Path, message: str) -> None:
    run(repo, "add", "-A")
    run(repo, "commit", "--quiet", "--allow-empty", "-m", message)


def write(repo: Path, files: dict[str, str]) -> None:
    for path, text in files.items():
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(text)


def apply(repo: Path, edit: Edit, files: dict[str, str]) -> None:
    """The working tree of `repo`, which holds `files`, changed by `edit`."""
    changed = dict(files)
    edit(changed)
    for path in files.keys() - changed.keys():
        (repo / path).unlink()
    write(repo, {p: t for p, t in changed.items() if files.get(p) != t})


def working(tmp_path: Path, edit: Edit, main: dict[str, str] = REAL) -> Path:
    """A clone of `main` whose working tree is `main` changed by `edit`."""
    repo = git_repo(tmp_path, main)
    apply(repo, edit, main)
    return repo


def changed_from(main: dict[str, str], edit: Edit) -> dict[str, str]:
    files = dict(main)
    edit(files)
    return files


def replaced(path: str, old: str, new: str) -> Edit:
    """A text edit of one file, which must hold `old` exactly once."""

    def edit(files: dict[str, str]) -> None:
        assert files[path].count(old) == 1, f"{path} no longer holds {old!r} once"
        files[path] = files[path].replace(old, new)

    return edit


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


def setting(key: str, value: object) -> Callable[[Block], None]:
    return lambda block: block.update({key: value})


def measure(model: str, name: str, key: str, value: object, path: str = CONTRACTS) -> Edit:
    def change(block: Block) -> None:
        (found,) = [m for m in block["measures"] if m["name"] == name]
        found[key] = value

    return on(path, "semantic_models", model, change)


def bump(name: str, path: str = CONTRACTS, to: int = 2, was: int = 1) -> Edit:
    """The metric's version raised in its own line of text, so two such edits merge cleanly; in a
    file another edit wrote back as YAML of its own, through that YAML."""

    def edit(files: dict[str, str]) -> None:
        text = files[path]
        line = f"version: {was}}}"
        if f"\n  - name: {name}\n" not in text:
            metric(name, lambda b: b["config"]["meta"].update(version=to), path)(files)
            return
        at = text.index(line, text.index(f"\n  - name: {name}\n"))
        files[path] = text[:at] + f"version: {to}}}" + text[at + len(line) :]

    return edit


def both(*edits: Edit) -> Edit:
    def edit(files: dict[str, str]) -> None:
        for each in edits:
            each(files)

    return edit


def moved(old: str, new: str) -> Edit:
    def edit(files: dict[str, str]) -> None:
        files[new] = files.pop(old)

    return edit


def lines(report: str) -> list[str]:
    return [line for line in report.splitlines() if line.startswith("- ")]


def still(*names: str, at: int = 1) -> list[str]:
    return [f"- metric {m}'s definition changed and its version is still {at}" for m in names]


FIXED_AWARD_EXPR = "expr: case when award_held_to > 0 then award_held_to end"
NEW_EXPR = "expr: case when award_held_to > 1 then award_held_to end"
PAID_AGAINST_EXPR = "expr: case when award_held_to > 0 then paid end"
REPEATS = "{{ Dimension('payment__entity_id') }} = 'uk-transport'"
PAYMENT_METRICS = [
    "chicago_payment_amount",
    "chicago_payment_rows",
    "chicago_repeat_amount",
    "chicago_repeat_rows",
    "duplicate_payment_amount_rate",
    "duplicate_payment_rate",
]
ALL_METRICS = sorted(
    [
        "constant_currency_spend",
        "fixed_award",
        "paid_against_fixed_award",
        "paid_over_award",
        "remaining_commitment",
        "spend_against_award",
        "spend_bases_in_periods",
        "spend_bases_in_years",
        "spend_in_periods",
        "spend_in_years",
        *PAYMENT_METRICS,
    ]
)

# A metric that reads another in its filter: remaining commitment over contracts paid nothing
# over their award, invented for the test.
UNPAID = "      measure: unpaid_award\n    config:\n"
READS_A_METRIC = replaced(
    CONTRACTS,
    UNPAID,
    "      measure: unpaid_award\n"
    "    filter: \"{{ Metric('paid_over_award', group_by=['contract']) }} = 0\"\n"
    "    config:\n",
)
OVER_AWARD_EXPR = (
    "        expr: >\n"
    "          case when award_held_to > 0 then\n"
    "            case when difference > 0 then difference else 0 end\n"
    "          end\n"
)


def split_by_dbtignore(files: dict[str, str]) -> None:
    """The old block of a metric left in a file `.dbtignore` hides from dbt, and a changed copy put
    in the macros folder, which dbt reads YAML from too."""
    text = files[PAYMENTS]
    start = text.index("  - name: chicago_payment_amount\n")
    end = text.index("  - name: chicago_repeat_amount\n")
    block = text[start:end]
    files[PAYMENTS] = text[:start] + text[end:]
    files[f"{FOLDER}/payments_metrics.yml"] = "metrics:\n" + block
    files["models/.dbtignore"] = "payments_metrics.yml\n"
    changed = block.replace(
        "      measure: payment_amount\n",
        "      measure: {name: payment_amount, join_to_timespine: true, fill_nulls_with: 0}\n",
    )
    assert changed != block
    files["models/macros/payment_inputs.yml"] = "metrics:\n" + changed


def whole_file_dbtignored(files: dict[str, str]) -> None:
    """The metric file hidden from dbt by `.dbtignore`, and a changed copy in the macros folder."""
    files["models/.dbtignore"] = "contracts.yml\n"
    files["models/macros/awards.yml"] = files[CONTRACTS].replace(FIXED_AWARD_EXPR, NEW_EXPR)


def spine_moved(files: dict[str, str]) -> None:
    """MetricFlow's time spine moved from the day spine to the fiscal calendar's days."""
    text = files[CALENDARS].replace(
        "    time_spine:\n      standard_granularity_column: date_day\n", ""
    )
    columns = (
        "    columns:\n      - name: entity_id\n        data_tests:\n          - not_null\n"
        "      - name: calendar_date\n"
    )
    assert text.count(columns) == 1, "the fiscal calendar's columns moved: update the test"
    files[CALENDARS] = text.replace(
        columns,
        "    time_spine:\n      standard_granularity_column: calendar_date\n"
        + columns
        + "        granularity: day\n",
    )


FAILS: dict[str, tuple[Edit, list[str]]] = {
    "a measure's expression changed, no version": (
        replaced(CONTRACTS, FIXED_AWARD_EXPR, NEW_EXPR),
        still("fixed_award", "spend_against_award"),
    ),
    "a measure changed, only the metric over it bumped": (
        both(replaced(CONTRACTS, FIXED_AWARD_EXPR, NEW_EXPR), bump("fixed_award")),
        still("spend_against_award"),
    ),
    "an input's filter changed, only the input bumped": (
        both(
            metric("chicago_repeat_rows", setting("filter", REPEATS), PAYMENTS),
            bump("chicago_repeat_rows", PAYMENTS),
        ),
        still("duplicate_payment_rate"),
    ),
    "a dimension's expression changed, no version": (
        on(
            PAYMENTS,
            "semantic_models",
            "payments",
            lambda b: b["dimensions"][1].update(expr="lower(entity_id)"),
        ),
        still(*PAYMENT_METRICS),
    ),
    "a metric split between a file dbt ignores and the macros folder, changed": (
        split_by_dbtignore,
        still("chicago_payment_amount", "duplicate_payment_amount_rate"),
    ),
    "a metric file ignored by dbt and a changed copy in the macros folder": (
        whole_file_dbtignored,
        still("fixed_award", "spend_against_award"),
    ),
    "the time spine moved": (spine_moved, still(*ALL_METRICS)),
}


@pytest.mark.parametrize("name", list(FAILS))
def test_a_changed_definition_under_the_same_version_fails_the_gate(
    tmp_path: Path, name: str
) -> None:
    edit, expected = FAILS[name]
    ok, report = versions.check(working(tmp_path, edit))
    assert not ok, report
    assert lines(report) == expected, report


def test_a_metric_read_in_a_filter_is_part_of_the_definition(tmp_path: Path) -> None:
    """A metric whose filter reads another metric changes when that metric does."""
    main = changed_from(REAL, READS_A_METRIC)
    edit = both(
        replaced(CONTRACTS, OVER_AWARD_EXPR, "        expr: award_held_to\n"),
        bump("paid_over_award"),
    )
    ok, report = versions.check(working(tmp_path, edit, main))
    assert not ok, report
    assert lines(report) == still("remaining_commitment"), report


PAYMENTS_MODEL = "models/models/marts/payments/_payments__models.yml"


def v2_metric(files: dict[str, str]) -> None:
    """A semantic model and a metric written on the payments model itself (dbt's newer spec,
    `semantic_model:` and `metrics:` on a model's entry), invented for the test."""
    content = yaml.safe_load(files[PAYMENTS_MODEL])
    (model,) = [m for m in content["models"] if m["name"] == "payments"]
    model["semantic_model"] = {"name": "payments_on_the_model"}
    model["agg_time_dimension"] = "payment_date"
    for column in model["columns"]:
        if column["name"] == "payment_key":
            column["entity"] = {"type": "primary", "name": "payment_on_the_model"}
        if column["name"] == "payment_date":
            column["dimension"] = {"type": "time"}
            column["granularity"] = "day"
    meta = {"owner": "payables-clerk", "term": "award", "role": "input", "version": 1}
    model["metrics"] = [
        {
            "name": "amount_on_the_model",
            "label": "Amount",
            "description": "Not for direct use: invented for a test.",
            "type": "simple",
            "agg": "sum",
            "expr": "amount",
            "config": {"meta": meta},
        }
    ]
    files[PAYMENTS_MODEL] = yaml.safe_dump(content, sort_keys=False)


def v2_dimension_added(files: dict[str, str]) -> None:
    content = yaml.safe_load(files[PAYMENTS_MODEL])
    (model,) = [m for m in content["models"] if m["name"] == "payments"]
    (column,) = [c for c in model["columns"] if c["name"] == "currency"]
    column["dimension"] = {"type": "categorical"}
    files[PAYMENTS_MODEL] = yaml.safe_dump(content, sort_keys=False)


def test_a_metric_on_a_model_entry_is_compared_too(tmp_path: Path) -> None:
    """dbt's newer spec, a semantic model and metrics on a model's own entry, reaches the
    comparison through dbt's manifest like any other: a dimension added there changes the metric."""
    main = changed_from(REAL, v2_metric)
    ok, report = versions.check(working(tmp_path, v2_dimension_added, main))
    assert not ok, report
    assert lines(report) == still("amount_on_the_model"), report


REFUSED: dict[str, tuple[Edit, list[str]]] = {
    "dbt Jinja in a definition": (
        both(
            replaced(
                CONTRACTS,
                FIXED_AWARD_EXPR,
                "expr: \"case when award_held_to > {{ var('award_floor', 0) }} then"
                ' award_held_to end"',
            ),
            metric(
                "remaining_commitment",
                setting("label", "Remaining commitment {{ env_var('ANY', '') }}"),
            ),
            metric(
                "chicago_payment_rows",
                setting("description", "Paid by {{ run_started_at.year }}."),
                PAYMENTS,
            ),
            replaced(
                CALENDARS,
                "      standard_granularity_column: date_day\n",
                "      standard_granularity_column: \"{{ 'date_day' }}\"\n",
            ),
            replaced(
                "models/models/marts/payments/_payments__models.yml",
                "  - name: payments\n",
                "  - name: payments\n    config: {meta: {read: \"{{ env_var('ANY', '') }}\"}}\n",
            ),
        ),
        [
            "semantics/metricflow/contracts.yml: semantic_models chicago_contracts holds dbt Jinja"
            " at measures.0.expr",
            "semantics/metricflow/contracts.yml: metrics remaining_commitment holds dbt Jinja at"
            " label",
            "semantics/metricflow/payments.yml: metrics chicago_payment_rows holds dbt Jinja at"
            " description",
            "models/calendars/calendars.yml: models metric_time_spine holds dbt Jinja at"
            " time_spine.standard_granularity_column",
            "models/marts/payments/_payments__models.yml: models payments holds dbt Jinja at"
            " config.meta.read",
        ],
    ),
    "Jinja in the project file": (
        replaced(PROJECT_FILE, "  source_rows: landed\n", "  source_rows: \"{{ 'landed' }}\"\n"),
        [f"{PROJECT_FILE} holds Jinja, which dbt renders from outside the tree"],
    ),
    "metrics turned off by the project's configuration": (
        lambda files: files.update(
            {PROJECT_FILE: files[PROJECT_FILE] + "\nmetrics:\n  ledger_lab:\n    +enabled: false\n"}
        ),
        [f"metric {m} (origin/main defines it) is gone or renamed" for m in ALL_METRICS],
    ),
    "a metric file moved out of what dbt reads": (
        moved(CONTRACTS, "semantics/contracts.yml"),
        ["metric spend_against_award (origin/main defines it) is gone or renamed"],
    ),
    "the project moved": (
        lambda files: files.update({"dbt/dbt_project.yml": files.pop(PROJECT_FILE)}),
        [f"there is no dbt project at {PROJECT_FILE}"],
    ),
    "a file dbt cannot read": (
        lambda files: files.update({CONTRACTS: "metrics: [\n"}),
        ["dbt parse failed"],
    ),
    "a metric defined twice": (
        lambda files: files.update({f"{FOLDER}/again.yml": files[CONTRACTS]}),
        ["dbt parse failed"],
    ),
}


@pytest.mark.parametrize("name", list(REFUSED))
def test_a_definition_dbt_reads_otherwise_or_cannot_read_fails_the_gate(
    tmp_path: Path, name: str
) -> None:
    edit, expected = REFUSED[name]
    ok, report = versions.check(working(tmp_path, edit))
    assert not ok, report
    for each in expected:
        assert each in report, report


def test_a_metric_reading_a_dbt_var_fails_even_when_only_the_var_changes(tmp_path: Path) -> None:
    """A definition that reads a dbt variable is refused on `origin/main` too, so a change of the
    variable alone cannot move the number under the same version."""
    main = changed_from(
        REAL,
        both(
            replaced(
                CONTRACTS,
                FIXED_AWARD_EXPR,
                "expr: \"case when award_held_to > {{ var('award_floor') }} then"
                ' award_held_to end"',
            ),
            replaced(PROJECT_FILE, "vars:\n", "vars:\n  award_floor: 0\n"),
        ),
    )
    edit = replaced(PROJECT_FILE, "  award_floor: 0\n", "  award_floor: 1000000\n")
    with pytest.raises(versions.GateError, match="holds dbt Jinja at measures.0.expr"):
        versions.check(working(tmp_path, edit, main))


def test_a_file_reached_by_a_link_or_outside_the_tree_fails_the_gate(tmp_path: Path) -> None:
    linked = working(tmp_path / "link", lambda files: None)
    outside = tmp_path / "outside.yml"
    outside.write_text("metrics: []\n")
    (linked / FOLDER / "linked.yml").symlink_to(outside)
    ok, report = versions.check(linked)
    assert not ok, report
    assert f"{FOLDER}/linked.yml is a link, which the gate does not follow" in report
    assert "metrics: []" not in report

    folder = tmp_path / "elsewhere"
    folder.mkdir()
    (folder / "more.yml").write_text("version: 2\n")
    path = replaced(
        PROJECT_FILE, '"../semantics/metricflow"]', f'"../semantics/metricflow", "{folder}"]'
    )
    ok, report = versions.check(working(tmp_path / "path", path))
    assert not ok, report
    assert "which is outside the tree" in report


def meta_of(manifest_metrics: list[Block], name: str) -> Block:
    (found,) = [m for m in manifest_metrics if m["name"] == name]
    return found["config"]["meta"]


@pytest.fixture(scope="module")
def committed(tmp_path_factory: pytest.TempPathFactory) -> Block:
    """The semantic manifest dbt writes for the committed tree, parsed by the gate."""
    work = tmp_path_factory.mktemp("committed")
    repo = git_repo(work, REAL)
    tree = work / "tree"
    tree.mkdir()
    versions.export_working(repo, tree)
    return versions.parse(tree, work)


Change = Callable[[Block], None]


def on_meta(name: str, **values: object) -> Change:
    def change(parsed: Block) -> None:
        meta = meta_of(parsed["metrics"], name)
        for key, value in values.items():
            if value is None:
                meta.pop(key)
            else:
                meta[key] = value

    return change


def on_metric(name: str, key: str, value: object) -> Change:
    def change(parsed: Block) -> None:
        (found,) = [m for m in parsed["metrics"] if m["name"] == name]
        found[key] = value

    return change


def on_measure(name: str, key: str, value: object) -> Change:
    def change(parsed: Block) -> None:
        (found,) = [
            x for s in parsed["semantic_models"] for x in s["measures"] if x["name"] == name
        ]
        found[key] = value

    return change


def changes(*each: Change) -> Change:
    def change(parsed: Block) -> None:
        for one in each:
            one(parsed)

    return change


def renamed(name: str, to: str) -> Change:
    return on_metric(name, "name", to)


def removed(name: str) -> Change:
    def change(parsed: Block) -> None:
        parsed["metrics"] = [m for m in parsed["metrics"] if m["name"] != name]

    return change


def added(parsed: Block) -> None:
    (source,) = [m for m in parsed["metrics"] if m["name"] == "remaining_commitment"]
    new = copy.deepcopy(source)
    new["name"] = "remaining_commitment_again"
    parsed["metrics"].append(new)


def compared(committed: Block, change: Change, before: Change | None = None) -> tuple[bool, str]:
    """The comparison of the committed manifest (changed by `before`) with a copy changed by
    `change`, both read as the gate reads a manifest dbt wrote."""
    base, after = copy.deepcopy(committed), copy.deepcopy(committed)
    if before is not None:
        before(base)
        before(after)
    change(after)
    return versions.compare(versions.definitions(base), versions.definitions(after))


def relation_moved(parsed: Block) -> None:
    (found,) = [s for s in parsed["semantic_models"] if s["name"] == "chicago_contracts"]
    found["node_relation"]["alias"] = "another_model"


NO_VERSION = "metric remaining_commitment has no version that is an integer from 1 to 2^53 - 1"
RULE_FAILS: dict[str, tuple[Change, str]] = {
    "a version gone": (on_meta("remaining_commitment", version=None), NO_VERSION),
    "a version of 0": (on_meta("remaining_commitment", version=0), NO_VERSION),
    "a semantic version": (on_meta("remaining_commitment", version="1.1.0"), NO_VERSION),
    "a version that is true": (on_meta("remaining_commitment", version=True), NO_VERSION),
    "a version past 2^53 - 1": (on_meta("remaining_commitment", version=2**53), NO_VERSION),
    "an owner gone": (
        on_meta("remaining_commitment", owner=None),
        "metric remaining_commitment has no owner",
    ),
    "an owner gone with a change and a new version": (
        on_meta("remaining_commitment", owner=None, version=2, term="award"),
        "metric remaining_commitment has no owner",
    ),
    "a metric removed": (
        removed("paid_over_award"),
        "metric paid_over_award (origin/main defines it) is gone or renamed",
    ),
    "a metric renamed": (
        renamed("paid_over_award", "overpaid"),
        "metric paid_over_award (origin/main defines it) is gone or renamed",
    ),
    "a metric's description changed, no version": (
        on_metric("spend_against_award", "description", "Said otherwise."),
        "metric spend_against_award's definition changed and its version is still 1",
    ),
    "a metric's label changed, no version": (
        on_metric("spend_against_award", "label", "Award spent"),
        "metric spend_against_award's definition changed and its version is still 1",
    ),
    "a metric's role changed, no version": (
        on_meta("fixed_award", role="headline"),
        "metric fixed_award's definition changed and its version is still 1",
    ),
    "a semantic model's relation changed, no version": (
        relation_moved,
        "metric remaining_commitment's definition changed and its version is still 1",
    ),
    "a measure's aggregation changed, no version": (
        on_measure("unpaid_award", "agg", "max"),
        "metric remaining_commitment's definition changed and its version is still 1",
    ),
}


@pytest.mark.parametrize("name", list(RULE_FAILS))
def test_a_metric_without_a_version_an_owner_or_its_promise_fails_the_comparison(
    committed: Block, name: str
) -> None:
    change, expected = RULE_FAILS[name]
    ok, report = compared(committed, change)
    assert not ok, report
    assert f"- {expected}" in report.splitlines(), report


def test_a_version_that_goes_down_fails_the_comparison(committed: Block) -> None:
    ok, report = compared(
        committed, on_meta("paid_over_award", version=2), on_meta("paid_over_award", version=3)
    )
    assert not ok, report
    assert lines(report) == ["- metric paid_over_award's version went down from 3 to 2"], report


RULE_PASSES: dict[str, Change] = {
    "a version raised with no change": on_meta("paid_over_award", version=2),
    "a version raised by more than one with a change": changes(
        on_metric("paid_over_award", "label", "Overpaid"), on_meta("paid_over_award", version=5)
    ),
    "a description changed with a new version": changes(
        on_metric("spend_against_award", "description", "Said otherwise."),
        on_meta("spend_against_award", version=2),
    ),
    "an owner changed alone": on_meta("paid_over_award", owner="group-controller"),
    "a measure's description changed": on_measure("fixed_award", "description", "Said otherwise."),
    "an input's description changed, only the input bumped": changes(
        on_metric("fixed_award", "description", "Not for direct use: said otherwise."),
        on_meta("fixed_award", version=2),
    ),
    "a new metric": added,
}


@pytest.mark.parametrize("name", list(RULE_PASSES))
def test_a_change_that_keeps_every_version_passes_the_comparison(
    committed: Block, name: str
) -> None:
    ok, report = compared(committed, RULE_PASSES[name])
    assert ok, report


def unversioned(parsed: Block) -> None:
    for each in parsed["metrics"]:
        each["config"]["meta"].pop("version")


def test_the_first_versions_pass_against_a_main_without_versions(committed: Block) -> None:
    before = copy.deepcopy(committed)
    unversioned(before)
    ok, report = versions.compare(versions.definitions(before), versions.definitions(committed))
    assert ok, report


def test_no_version_fails_against_a_main_without_versions(committed: Block) -> None:
    ok, report = compared(committed, lambda parsed: None, unversioned)
    assert not ok, report
    assert NO_VERSION in report


PASSES: dict[str, Edit] = {
    "comments, another key order and folding, and a file moved to another folder dbt reads": both(
        replaced(CONTRACTS, "\nmetrics:", "\n# A comment.\nmetrics:"),
        metric("remaining_commitment", lambda b: None),
        moved(CONTRACTS, "models/macros/contracts.yml"),
    ),
}


@pytest.mark.parametrize("name", list(PASSES))
def test_a_change_that_keeps_every_version_passes_the_gate(tmp_path: Path, name: str) -> None:
    ok, report = versions.check(working(tmp_path, PASSES[name]))
    assert ok, report


def test_the_gate_names_the_versions_that_went_up(tmp_path: Path) -> None:
    edit = both(
        replaced(CONTRACTS, FIXED_AWARD_EXPR, NEW_EXPR),
        bump("fixed_award"),
        bump("spend_against_award"),
    )
    ok, report = versions.check(working(tmp_path, edit))
    assert ok, report
    assert report.endswith(
        "changed with a new version: fixed_award 1 to 2, spend_against_award 1 to 2"
    )


def test_the_first_metrics_pass_while_main_has_no_dbt_project(tmp_path: Path) -> None:
    repo = working(tmp_path, lambda files: files.update(REAL), {"other.txt": "no project\n"})
    ok, report = versions.check(repo)
    assert ok, report
    assert "defines no metric" in report


def test_a_clone_without_main_fails_and_says_how_to_fetch_it(tmp_path: Path) -> None:
    repo = git_repo(tmp_path, None)
    write(repo, REAL)
    with pytest.raises(versions.GateError, match="git fetch origin main"):
        versions.check(repo)


def test_a_main_whose_project_cannot_be_parsed_fails_the_gate(tmp_path: Path) -> None:
    main = changed_from(REAL, lambda files: files.update({CONTRACTS: "metrics: [\n"}))
    with pytest.raises(versions.GateError, match="origin/main's metrics cannot be read"):
        versions.check(working(tmp_path, lambda files: None, main))


def test_a_main_whose_project_moved_fails_the_gate(tmp_path: Path) -> None:
    main = changed_from(REAL, moved(PROJECT_FILE, "dbt/dbt_project.yml"))
    with pytest.raises(versions.GateError, match="there is no dbt project at models/"):
        versions.check(working(tmp_path, lambda files: None, main))


def test_a_main_git_cannot_read_fails_the_gate(tmp_path: Path) -> None:
    """A partial clone whose promisor is gone holds `origin/main`'s commits but not its files:
    the gate fails instead of reading the missing files as empty."""
    source = git_repo(tmp_path / "source", REAL)
    run(source, "config", "uploadpack.allowFilter", "true")
    apply(source, replaced(CONTRACTS, "\nmetrics:", "\n# A comment.\nmetrics:"), REAL)
    commit(source, "main moves")
    run(source, "branch", "-f", "published", "HEAD")
    apply(source, replaced(CONTRACTS, FIXED_AWARD_EXPR, NEW_EXPR), dict(REAL))
    commit(source, "a change")
    run(source, "branch", "feature", "HEAD")
    run(source, "checkout", "--quiet", "published")
    run(source, "branch", "-f", "main", "published")
    clone = tmp_path / "clone"
    url = f"file://{source}"
    run(tmp_path, "clone", "--quiet", "--filter=blob:none", "--no-checkout", url, str(clone))
    run(clone, "checkout", "--quiet", "-b", "feature", "origin/feature")
    run(clone, "remote", "set-url", "origin", f"file://{tmp_path / 'gone'}")
    with pytest.raises(versions.GateError, match="origin/main's metrics cannot be read"):
        versions.check(clone)
    run(clone, "remote", "set-url", "origin", url)
    ok, report = versions.check(clone)
    assert lines(report) == still("fixed_award", "spend_against_award"), report


def test_two_branches_publishing_one_version_cannot_both_pass(tmp_path: Path) -> None:
    """Branch A and branch B, both cut from one `main`, each change a metric and bump the
    headline over both to 2. Once A is merged, B is refused until it merges `main`, and then
    shows the headline changed again under version 2."""
    repo = git_repo(tmp_path, REAL)
    start = run(repo, "rev-parse", "HEAD").strip()
    run(repo, "checkout", "--quiet", "-b", "a")
    edit_a = both(
        replaced(CONTRACTS, FIXED_AWARD_EXPR, NEW_EXPR),
        bump("fixed_award"),
        bump("spend_against_award"),
    )
    apply(repo, edit_a, REAL)
    commit(repo, "a")
    ok, report = versions.check(repo)
    assert ok, report
    run(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    run(repo, "checkout", "--quiet", "-b", "b", start)
    edit_b = both(
        replaced(CONTRACTS, PAID_AGAINST_EXPR, "expr: case when award_held_to > 9 then paid end"),
        bump("paid_against_fixed_award"),
        bump("spend_against_award"),
    )
    apply(repo, edit_b, REAL)
    commit(repo, "b")
    with pytest.raises(versions.GateError, match="HEAD does not contain origin/main"):
        versions.check(repo)
    run(repo, "merge", "--quiet", "--no-edit", "origin/main")
    ok, report = versions.check(repo)
    assert not ok, report
    assert lines(report) == still("spend_against_award", at=2), report


def test_the_command_exits_non_zero_on_a_break_and_zero_without(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`python semantics/versions.py`, as `make metric-versions` runs it, in a clone of its own,
    exits 1 on a break; its entry exits 0 on a pass and 1 when the comparison cannot be made."""
    env = {k: v for k, v in os.environ.items() if k != "LEDGER_LAB_GCP_PROJECT"}
    repo = working(tmp_path, replaced(CONTRACTS, FIXED_AWARD_EXPR, NEW_EXPR))
    command = [sys.executable, "-I", str(repo / "semantics/versions.py")]
    broken = subprocess.run(command, cwd=repo, capture_output=True, text=True, env=env)
    assert broken.returncode == 1, broken.stdout
    assert still("fixed_award", "spend_against_award") == lines(broken.stderr)

    def cannot() -> tuple[bool, str]:
        raise versions.GateError("cannot")

    assert versions.main([], run=lambda: (True, "passes")) == 0
    assert versions.main([], run=lambda: (False, "breaks")) == 1
    assert versions.main([], run=cannot) == 1
    assert capsys.readouterr().err == "breaks\nmetric-versions: cannot\n"


def test_every_metric_carries_a_version(build: Build) -> None:
    for each in manifest(build)["metrics"]:
        assert versions.valid_version(each["config"]["meta"].get("version")), each["name"]


def test_the_gate_reads_the_metrics_dbt_parses(build: Build) -> None:
    """The gate's definitions name every metric dbt's own build parsed, each with its version."""
    parsed = manifest(build)
    read = versions.definitions(parsed)
    assert sorted(read) == ALL_METRICS
    for each in parsed["metrics"]:
        assert read[each["name"]].version == each["config"]["meta"]["version"]
    assert read["spend_against_award"].definition["inputs"].keys() == {
        "fixed_award",
        "paid_against_fixed_award",
    }
    assert read["constant_currency_spend"].definition["inputs"].keys() == {
        "spend_in_periods",
        "spend_in_years",
        "spend_bases_in_periods",
        "spend_bases_in_years",
    }
