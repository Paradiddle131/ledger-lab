"""BigQuery's purposes: each purpose of `access/purposes.yaml` as a dataset of authorised views, the
only way a persona reads the rows of a product the models build tables of.

A purpose's dataset is its id with `_` for `-` (`payment_review`, `group_close`, `audit`), the
schema the local engine stores the purpose's tables in. In it, for each table the purpose lists,
a view of that name selects exactly the purpose's columns, in the purpose's order, from the table
as a persona read it before: for an entity-scoped product, its governed copy in the product's
dataset (`access/row_policies.py`), and for a table with a tagged column also a view
`<table>_masked` over the masked copy, since the row access policies give a tag's masked readers
rows of the masked copy alone; for a group product, its own table in its dataset. A view holds no
filter and no other logic: the row access policies and the policy tags on its source act on the
persona querying it, so a persona reads through a view exactly what it read of the source.

`platform/gcp/warehouse.tf` declares the datasets, keeps the agent out of them (a view there reads
its source as itself), authorizes each one's views on the datasets its views read, and grants each
persona data viewer on its purposes' datasets in place of the product's.

Views are DDL, so the owner's login runs them: `python -m access.purpose_views plan` prints the
statements, `apply` runs them. The project comes from `LEDGER_LAB_GCP_PROJECT` and the location
from `LEDGER_LAB_BQ_LOCATION`; nothing is written to disk. Views store nothing and creating one
bills nothing.

The owner's steps, in order, cost read before and after with `make cost-check`:
1. a targeted `tofu apply` of the additions alone, with the product grants still in place: the
   purpose datasets (`google_bigquery_dataset.warehouse["<purpose dataset>"]`), their views'
   authorizations (`google_bigquery_dataset_access.purposes_read_sources`) and the purpose grants
   (`google_bigquery_dataset_access.persona_reads_purposes`);
2. `python -m access.purpose_views apply`;
3. `LEDGER_LAB_PRODUCT_GRANTS=kept make bigquery-purposes`, which skips the one case that needs
   the product grants gone;
4. the full `tofu apply`, which removes the product grants;
5. `make bigquery-purposes`, every case.
"""

import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from access.identities import UnknownPersonaError, identity
from access.row_policies import RowPolicyError, copies, path, query, run_tool

ACCESS = Path(__file__).parent
PERSONAS = ACCESS / "personas.yaml"
COLUMNS = ACCESS / "columns.yaml"
PURPOSES = ACCESS / "purposes.yaml"
AGGREGATES = ACCESS / "aggregates.yaml"
# The model layers platform/gcp declares beside the products' and the aggregates' datasets.
LAYERS = ("landed", "intermediate")
# The statements are built as text, so every column in them is checked against its shape.
COLUMN = re.compile(r"^[a-z][a-z0-9_]*$")


class PurposeError(ValueError):
    """The catalogues ask for a purpose view this module cannot write."""


@dataclass(frozen=True)
class View:
    """One view of a purpose's dataset: the purpose, the view, the source it selects from, the
    purpose's columns of it, and whom its source's rows are for (`all`, or `clear` or `masked` for
    a table with a tagged column, as `access/row_policies.py` names a governed copy's readers)."""

    purpose: str
    dataset: str
    name: str
    product: str
    table: str
    source_dataset: str
    source: str
    columns: tuple[str, ...]
    audience: str

    def sql(self, project: str) -> str:
        for column in self.columns:
            if not COLUMN.fullmatch(column):
                raise PurposeError(f"{column!r} is not a column name")
        return (
            f"create or replace view {path(project, self.dataset, self.name)}"  # noqa: S608
            f" as select {', '.join(self.columns)}"
            f" from {path(project, self.source_dataset, self.source)}"
        )


def load(file: Path) -> dict[str, Any]:
    return yaml.safe_load(file.read_text())


def dataset_of(name: str) -> str:
    """A dataset id from a product's or a purpose's id: underscores, not hyphens."""
    return name.replace("-", "_")


def declared_datasets(catalogue: Mapping[str, Any], aggregates: Mapping[str, Any]) -> set[str]:
    """Every other dataset `platform/gcp` declares: the layers, one per product and one per
    product with aggregate tables."""
    products = {dataset_of(p["id"]) for p in catalogue["data_products"]}
    return {*LAYERS, *products, *(f"{dataset_of(p)}_aggregates" for p in aggregates["products"])}


def purpose_datasets(
    purposes: Mapping[str, Any], catalogue: Mapping[str, Any], aggregates: Mapping[str, Any]
) -> dict[str, str]:
    """Each purpose's dataset, by purpose id; refuses two purposes on one dataset and a purpose
    whose dataset is another dataset platform/gcp declares."""
    found: dict[str, str] = {}
    taken = declared_datasets(catalogue, aggregates)
    for purpose in purposes["purposes"]:
        dataset = dataset_of(purpose["id"])
        if dataset in found.values():
            raise PurposeError(f"purpose {purpose['id']}: another purpose's dataset, {dataset}")
        if dataset in taken:
            raise PurposeError(f"purpose {purpose['id']}: {dataset} is another declared dataset")
        found[purpose["id"]] = dataset
    return found


def views(
    catalogue: Mapping[str, Any],
    columns: Mapping[str, Any],
    purposes: Mapping[str, Any],
    aggregates: Mapping[str, Any],
) -> list[View]:
    """Every purpose's views, in the purposes' and their tables' order; refuses a table no product
    has, a purpose persona with no row grant on the table's product, and two views of one name."""
    datasets = purpose_datasets(purposes, catalogue, aggregates)
    products = {
        table: product
        for product in catalogue["data_products"]
        for table in product.get("tables", [])
    }
    governed = copies(catalogue, columns)
    found: list[View] = []
    for purpose in purposes["purposes"]:
        dataset = datasets[purpose["id"]]
        for table, listed in purpose["tables"].items():
            if table not in products:
                raise PurposeError(f"purpose {purpose['id']}: {table} is no product's table")
            product = products[table]
            for persona in purpose["personas"]:
                try:
                    reads = identity(catalogue, persona).reads
                except UnknownPersonaError:
                    raise PurposeError(f"purpose {purpose['id']}: no persona {persona}") from None
                if product["id"] not in reads:
                    raise PurposeError(
                        f"purpose {purpose['id']}: {persona} holds no row grant on {product['id']}"
                    )
            if product["scope"] == "entity":
                sources = [
                    (c.dataset, c.table, c.audience)
                    for c in governed
                    if c.product == product["id"] and c.table in (table, f"{table}_masked")
                ]
            else:
                sources = [(dataset_of(product["id"]), table, "all")]
            for source_dataset, source, audience in sources:
                found.append(
                    View(
                        purpose=purpose["id"],
                        dataset=dataset,
                        name=source,
                        product=product["id"],
                        table=table,
                        source_dataset=source_dataset,
                        source=source,
                        columns=tuple(listed),
                        audience=audience,
                    )
                )
    names = [(v.dataset, v.name) for v in found]
    if len(set(names)) != len(names):
        twice = sorted({f"{d}.{n}" for d, n in names if names.count((d, n)) > 1})
        raise PurposeError(f"two views of one name: {', '.join(twice)}")
    return found


def plan(
    catalogue: Mapping[str, Any],
    columns: Mapping[str, Any],
    purposes: Mapping[str, Any],
    aggregates: Mapping[str, Any],
    project: str,
) -> list[str]:
    """The statements the owner runs: every purpose view, created or replaced."""
    return [v.sql(project) for v in views(catalogue, columns, purposes, aggregates)]


def main(argv: Sequence[str], env: Mapping[str, str]) -> int:
    from ingest.load import LoadError, lab

    command = argv[0] if argv else ""
    if command not in ("plan", "apply"):
        print("usage: python -m access.purpose_views plan|apply", file=sys.stderr)
        return 2
    try:
        project, where = lab(env)
        statements = plan(load(PERSONAS), load(COLUMNS), load(PURPOSES), load(AGGREGATES), project)
        if command == "plan":
            print("\n".join(statements))
            return 0
        for statement in statements:
            run_tool(query(project, where, statement))
    except (PurposeError, RowPolicyError, LoadError) as error:
        print(f"purpose-views: {error}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as error:
        # bq's message names the project and the accounts: only its exit status is reported.
        print(f"purpose-views: bq failed with exit status {error.returncode}", file=sys.stderr)
        return 1
    print(f"purpose-views: {len(statements)} views written")
    return 0


if __name__ == "__main__":
    import os

    sys.exit(main(sys.argv[1:], os.environ))
