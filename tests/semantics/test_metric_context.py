"""The metric context (`semantics/context.json`, written by `python -m semantics.export`): the one
export of the metrics, glossary and verified queries the agent and the catalog read.

What is checked:

- the committed file is byte for byte what a fresh export of the tree writes;
- the stale check names a metric changed, added or removed in the committed file, a glossary term
  changed, a text-only edit, a file that is not JSON and a missing file;
- one metric's definition changed and its version raised, in a copy of the tree read back through
  dbt's parse, changes that metric's entry alone, which carries the new version and the change;
  every other entry is equal to the committed tree's, though the copy sits in another folder;
- in that copy, `python -m semantics.export --check` fails naming the metric, and the command
  without `--check` writes the fresh export;
- a verified query whose version is not its metric's, one naming no defined metric, and a metric
  whose term the glossary lacks are refused;
- the export holds no path of the folder it was made in.
"""

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from semantics import export
from tests.semantics.test_metric_versions import git_repo, replaced, tracked

Block = dict[str, Any]
CHANGED = "duplicate_payment_amount_rate"
PAYMENTS = "semantics/metricflow/payments.yml"
VERIFIED = "semantics/verified_queries.yaml"
# The tree a fresh export reads: the dbt project, the definitions, the export and the gate it uses,
# with the files that name the gate's BigQuery environment, so the copy shares that environment.
TREE = tracked(
    "models", "semantics", "platform/group_env.py", "pyproject.toml", "uv.lock", ".python-version"
)
RATIO = (
    "      denominator: chicago_payment_amount\n"
    "    config:\n"
    "      meta: {owner: payables-clerk, term: duplicate-payment-rate, role: headline,"
    " version: 1}\n"
)
FILTER = "{{ Dimension('payment__entity_id') }} = 'chicago'"
RATIO_CHANGED = (
    "      denominator: chicago_payment_amount\n"
    f'    filter: "{FILTER}"\n'
    "    config:\n"
    "      meta: {owner: payables-clerk, term: duplicate-payment-rate, role: headline,"
    " version: 2}\n"
)
QUERY = "    metric: duplicate_payment_amount_rate\n    version: 1\n"
QUERY_CHANGED = "    metric: duplicate_payment_amount_rate\n    version: 2\n"


@pytest.fixture(scope="module")
def fresh() -> Block:
    """A fresh export of the working tree."""
    return export.context()


def committed_text() -> str:
    return (export.REPO / export.CONTEXT).read_text()


def test_the_committed_export_is_current(fresh: Block) -> None:
    found = export.differences(committed_text(), fresh)
    assert found == [], (
        f"{export.CONTEXT} is stale ({'; '.join(found)}): "
        "run `uv run python -m semantics.export` and commit the result"
    )


def edited(fresh: Block, change: Any) -> str:
    """The fresh export's text with `change` applied to a copy of its content."""
    copy = json.loads(export.render(fresh))
    change(copy)
    return export.render(copy)


def changed_label(c: Block) -> None:
    c["metrics"]["remaining_commitment"]["label"] += " (edited)"


def removed(c: Block) -> None:
    del c["metrics"]["paid_over_award"]


def added(c: Block) -> None:
    c["metrics"]["invented_metric"] = {"version": 1}


def changed_version(c: Block) -> None:
    c["metrics"]["fixed_award"]["version"] = 7


def changed_term(c: Block) -> None:
    c["glossary"]["award"]["definition"] = "An invented definition."


@pytest.mark.parametrize(
    ("change", "said"),
    [
        (changed_label, "metrics.remaining_commitment: differs from a fresh export"),
        (changed_version, "metrics.fixed_award: differs from a fresh export"),
        (removed, "metrics.paid_over_award: in a fresh export, not in the committed file"),
        (added, "metrics.invented_metric: in the committed file, not in a fresh export"),
        (changed_term, "glossary.award: differs from a fresh export"),
    ],
)
def test_a_stale_entry_is_named(fresh: Block, change: Any, said: str) -> None:
    assert export.differences(edited(fresh, change), fresh) == [said]


def test_a_text_only_edit_a_broken_or_missing_file_is_stale(fresh: Block) -> None:
    text = export.render(fresh)
    reindented = json.dumps(fresh, indent=4, sort_keys=True) + "\n"
    assert export.differences(reindented, fresh) == [
        f"{export.CONTEXT}'s text differs from what the export writes, its entries agree"
    ]
    assert export.differences(text.rstrip("\n"), fresh) != []
    assert export.differences(text[:-10], fresh) == [f"{export.CONTEXT} is not JSON"]
    assert export.differences(None, fresh) == [f"{export.CONTEXT} is missing"]
    assert export.differences(text, fresh) == []


def changed_tree(tmp_path: Path, *edits: Any) -> Path:
    files = dict(TREE)
    for edit in edits:
        edit(files)
    return git_repo(tmp_path, files)


def test_one_definition_changed_changes_that_metric_alone(fresh: Block, tmp_path: Path) -> None:
    before = fresh["metrics"][CHANGED]
    assert before["version"] == 1 and "filter" not in before
    repo = changed_tree(
        tmp_path,
        replaced(PAYMENTS, RATIO, RATIO_CHANGED),
        replaced(VERIFIED, QUERY, QUERY_CHANGED),
    )
    after = export.context(repo)
    assert export.differences(export.render(fresh), after) == [
        f"metrics.{CHANGED}: differs from a fresh export"
    ]
    entry = after["metrics"][CHANGED]
    assert entry["version"] == 2
    assert entry["filter"] == {"where_filters": [{"where_sql_template": FILTER}]}
    assert {k: v for k, v in entry.items() if k not in ("version", "filter")} == {
        k: v for k, v in before.items() if k != "version"
    }
    others = {k: v for k, v in after["metrics"].items() if k != CHANGED}
    assert others == {k: v for k, v in fresh["metrics"].items() if k != CHANGED}
    assert after["glossary"] == fresh["glossary"]
    text = export.render(after)
    assert str(tmp_path) not in text and str(export.REPO) not in text

    # The committed export in the copy is now stale: the command says so, then rewrites it.
    command = [sys.executable, "-m", "semantics.export"]
    stale = subprocess.run([*command, "--check"], cwd=repo, capture_output=True, text=True)
    assert stale.returncode == 1, stale.stdout + stale.stderr
    assert f"- metrics.{CHANGED}: differs from a fresh export\n" in stale.stderr
    written = subprocess.run(command, cwd=repo, capture_output=True, text=True)
    assert written.returncode == 0, written.stdout + written.stderr
    assert (repo / export.CONTEXT).read_text() == text


def test_a_query_or_term_out_of_step_with_the_metrics_is_refused(fresh: Block) -> None:
    metrics = [
        {"name": name, "config": {"meta": {k: entry.get(k) for k in export.META}}}
        for name, entry in fresh["metrics"].items()
    ]
    glossary = export.checked(export.REPO, export.GLOSSARY)
    verified = export.checked(export.REPO, export.VERIFIED)
    export.build(metrics, glossary, verified)

    old = json.loads(json.dumps(verified))
    (query,) = [q for q in old["verified_queries"] if q["metric"] == CHANGED]
    query["version"] = 2
    with pytest.raises(export.ExportError, match=f"has version 2, but {CHANGED} has 1"):
        export.build(metrics, glossary, old)

    query["version"], query["metric"] = 1, "invented_metric"
    with pytest.raises(export.ExportError, match="metric invented_metric is not defined"):
        export.build(metrics, glossary, old)

    without = {"terms": [t for t in glossary["terms"] if t["id"] != "constant-currency"]}
    with pytest.raises(export.ExportError, match="term constant-currency is not in the glossary"):
        export.build(metrics, without, verified)
