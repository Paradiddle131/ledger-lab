"""The policy tags on the clear governed copy of each product table, the second lock on its
sensitive columns (`access/columns.yaml`).

`platform/gcp/columns.tf` declares one taxonomy with a policy tag per tag of columns.yaml, and its
clear readers' fine-grained reader role on each; the owner applies it. This module attaches each
tag to its columns on the clear copy `access/row_policies.py` creates: it finds the taxonomy by its
display name and its tags by theirs (`gcloud data-catalog`), reads the copy's schema
(`bq show --schema`), sets each tagged column's policy tag and writes the schema back
(`bq update`), leaving every other column as it is. The masked copy carries no tag: its tagged
columns hold the masked values already. The row access policies are the first lock: a masked
reader gets no row of the clear copy, so a tag only matters if those policies are ever lost, and
whether a tag denies a column on a project with no organization is what the live test shows.

Reading the taxonomy and setting a column's tag are free; nothing here grants a role.
"""

import json
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from access.row_policies import COLUMNS, PERSONAS, RowPolicyError, bq, copies, load, path

TAXONOMY = "ledger-lab sensitive columns"
Run = Callable[[Sequence[str]], str]


def run_tool(command: Sequence[str]) -> str:
    return subprocess.run(command, capture_output=True, text=True, check=True).stdout  # noqa: S603


def gcloud(project: str, where: str, *command: str) -> list[str]:
    return [
        "gcloud",
        "data-catalog",
        "taxonomies",
        *command,
        f"--project={project}",
        f"--location={where}",
        "--format=json",
    ]


def tag_names(project: str, where: str, run: Run) -> dict[str, str]:
    """Each policy tag's display name, a tag id of columns.yaml, to its resource name."""
    taxonomies = [
        t
        for t in json.loads(run(gcloud(project, where, "list")) or "[]")
        if t.get("displayName") == TAXONOMY
    ]
    if len(taxonomies) != 1:
        raise RowPolicyError(
            f"{len(taxonomies)} taxonomies are named {TAXONOMY!r}: the owner applies"
            " platform/gcp/columns.tf first"
        )
    taxonomy = taxonomies[0]["name"].rsplit("/", 1)[-1]
    listed = json.loads(
        run(gcloud(project, where, "policy-tags", "list", f"--taxonomy={taxonomy}")) or "[]"
    )
    return {str(t["displayName"]): str(t["name"]) for t in listed}


def tagged(columns: Mapping[str, Any], product: str, table: str) -> dict[str, str]:
    """A product table's tagged columns, each with its tag's id."""
    listed = columns.get("products", {}).get(product, {}).get("columns", {}) or {}
    return {
        column.split(".", 1)[1]: tag
        for column, tag in listed.items()
        if column.split(".", 1)[0] == table
    }


def with_tags(
    schema: list[dict[str, Any]], wanted: Mapping[str, str], names: Mapping[str, str]
) -> list[dict[str, Any]]:
    """The schema with each wanted column's policy tag set, every other field unchanged."""
    missing = sorted(set(wanted.values()) - set(names))
    if missing:
        raise RowPolicyError(f"the taxonomy holds no policy tag {', '.join(missing)}")
    present = {field["name"] for field in schema}
    absent = sorted(set(wanted) - present)
    if absent:
        raise RowPolicyError(f"the copy has no column {', '.join(absent)}")
    return [
        {**field, "policyTags": {"names": [names[wanted[field["name"]]]]}}
        if field["name"] in wanted
        else field
        for field in schema
    ]


def tag(
    catalogue: Mapping[str, Any],
    columns: Mapping[str, Any],
    project: str,
    where: str,
    run: Run = run_tool,
) -> None:
    names = tag_names(project, where, run)
    for each in copies(catalogue, columns):
        if each.audience != "clear":
            continue
        table = path(project, each.dataset, each.table).strip("`").replace(".", ":", 1)
        schema = json.loads(run(bq(project, where, "--format=json", "show", "--schema", table)))
        updated = with_tags(schema, tagged(columns, each.product, each.table), names)
        with tempfile.TemporaryDirectory() as folder:
            written = Path(folder) / "schema.json"
            written.write_text(json.dumps(updated))
            run(bq(project, where, "update", table, str(written)))


def main(argv: Sequence[str], env: Mapping[str, str]) -> int:
    from ingest.load import LoadError, lab

    if list(argv) != ["tag"]:
        print("usage: python -m access.policy_tags tag", file=sys.stderr)
        return 2
    try:
        project, where = lab(env)
        tag(load(PERSONAS), load(COLUMNS), project, where)
    except (RowPolicyError, LoadError) as error:
        print(f"policy-tags: {error}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as error:
        # The tools' messages name the project and the taxonomy: only the exit status is reported.
        print(
            f"policy-tags: {error.cmd[0]} failed with exit status {error.returncode}",
            file=sys.stderr,
        )
        return 1
    print("policy-tags: tag done")
    return 0


if __name__ == "__main__":
    import os

    sys.exit(main(sys.argv[1:], os.environ))
