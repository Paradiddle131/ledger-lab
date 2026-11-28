"""BigQuery's aggregate views: the tables of `access/aggregates.yaml` as views an aggregate grant
reads in place of a product's rows, each returning only the groups of at least the minimum group.

Each product with aggregate tables has its own dataset of views, `<product>_aggregates`, which
`platform/gcp/warehouse.tf` grants to that product's aggregate grantees alone and authorizes on
its source: an entity-scoped product's mart in `intermediate` (its product dataset holds the
governed copy, under row access policies a view would read as its querying persona), a group
product's own dataset. A view therefore reads its source as itself, and its reader holds no
grant on the source.

Every view is the table itself, grouped at its grain (its `group_by` and `entity_group_by`, its
sums and its units, the columns `access/compile.py` gives the local engine's aggregate table) with
the minimum in a HAVING, so it holds only the kept groups and no column finer than its grain: each
of its rows is one kept group, so no filter, coarser grouping or difference of two answers over
one view reaches a group the minimum dropped (two tables that sum the same figures at different
grains are `access/aggregates.yaml`'s open point). Two kinds, by what a table's units count:
- `count_distinct`: `count(distinct <units_of>)` over the source rows, as `<units>`, kept when it
  is at least the minimum group.
- `max`: the largest per-row count of payees the source rows already hold, `max(<units_of>)`, kept
  likewise.
Both are the condition `access/compile.py` writes for the local engine. BigQuery's HAVING reads a
bare name as the select list's alias first, so the condition names the source's column
(`source.<units_of>`). No view carries an analysis rule: the grouping already returns only kept
groups, and an aggregation threshold over row-level rows checks each query's groups apart, so two
answers that each pass it can be subtracted to a group under the minimum.

One view per table serves every aggregate reader of its product, so the readers must share one
set of entities, which the view's filter names; a table whose readers do not is refused. A table
of figures across all entities (`entity_rows: false`) is refused too: its minimum entities rule
is the local engine's alone.

Views are DDL, so the owner's login runs them: `python -m access.thresholds plan` prints the
statements, `apply` runs them. The project comes from `LEDGER_LAB_GCP_PROJECT` and the location
from `LEDGER_LAB_BQ_LOCATION`.
"""

import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from access.compile import AGGREGATE, _entities
from access.row_policies import ENTITY, RowPolicyError, path, query, run_tool

ACCESS = Path(__file__).parent
PERSONAS = ACCESS / "personas.yaml"
AGGREGATES = ACCESS / "aggregates.yaml"
# The dataset holding an entity-scoped product's marts, which no persona reads.
MARTS = "intermediate"
# The statements are built as text, so every name in them is checked against its shape.
ENTITY_ID = re.compile(r"^[a-z][a-z0-9-]*$")
COLUMN = re.compile(r"^[a-z][a-z0-9_]*$")


class ThresholdError(ValueError):
    """The catalogues ask for a view this module cannot write as one view for every reader."""


@dataclass(frozen=True)
class View:
    dataset: str
    name: str
    source_dataset: str
    source: str
    entities: frozenset[str] | None
    group_by: tuple[str, ...]
    sums: tuple[str, ...]
    units: str
    # `count_distinct` or `max`, over `units_of`: how a group's units are counted.
    count: str
    units_of: str
    minimum: int

    def sql(self, project: str) -> str:
        for column in (*self.group_by, *self.sums, self.units, self.units_of):
            if not COLUMN.fullmatch(column):
                raise ThresholdError(f"{column!r} is not a column name")
        for entity in self.entities or ():
            if not ENTITY_ID.fullmatch(entity):
                raise ThresholdError(f"{entity!r} is not an entity id")
        view = path(project, self.dataset, self.name)
        source = path(project, self.source_dataset, self.source)
        where = ""
        if self.entities is not None:
            listed = ", ".join(f"'{e}'" for e in sorted(self.entities)) or "null"
            where = f" where {ENTITY} in ({listed})"
        # BigQuery's HAVING reads a bare name as the select list's alias first, so `max(payees)`
        # beside `max(payees) as payees` is an aggregate of an aggregate: the name is the source's.
        units = "count(distinct {})" if self.count == "count_distinct" else "max({})"
        keys = ", ".join(self.group_by)
        sums = "".join(f", sum({c}) as {c}" for c in self.sums)
        return (
            f"create or replace view {view} as select {keys}{sums},"  # noqa: S608
            f" {units.format(self.units_of)} as {self.units} from {source} as source{where}"
            f" group by {keys} having {units.format(f'source.{self.units_of}')} >= {self.minimum}"
        )


def load(file: Path) -> dict[str, Any]:
    return yaml.safe_load(file.read_text())


def dataset_of(product: str) -> str:
    return product.replace("-", "_")


def aggregates_dataset(product: str) -> str:
    return f"{dataset_of(product)}_aggregates"


def readers(catalogue: Mapping[str, Any], product: str) -> list[str]:
    """The personas with an aggregate grant on the product, in catalogue order."""
    return list(
        dict.fromkeys(
            g["persona"]
            for g in catalogue["grants"]
            if g["product"] == product and g["access"] == AGGREGATE
        )
    )


def views(catalogue: Mapping[str, Any], aggregates: Mapping[str, Any]) -> list[View]:
    """Every aggregate view, for each product with aggregate readers and aggregate tables."""
    products = {p["id"]: p for p in catalogue["data_products"]}
    found: list[View] = []
    for product, entry in aggregates["products"].items():
        if product not in products:
            raise ThresholdError(f"{product}: aggregate tables for no data product")
        who = readers(catalogue, product)
        if not who:
            continue
        if entry["entity_rows"] is not True:
            raise ThresholdError(f"{product}: figures across all entities are not written here")
        sets = {_entities(catalogue, persona, product) for persona in who}
        if len(sets) != 1:
            raise ThresholdError(f"{product}: its aggregate readers read different entities")
        (entities,) = sets
        scoped = products[product]["scope"] == "entity" and bool(products[product].get("tables"))
        for table in entry["tables"]:
            units = table["units"]
            count = "count_distinct" if "count_distinct" in units else "max"
            if set(units) != {"name", count}:
                raise ThresholdError(f"{table['name']}: units count no column it knows: {units}")
            found.append(
                View(
                    dataset=aggregates_dataset(product),
                    name=table["name"],
                    source_dataset=MARTS if scoped else dataset_of(product),
                    source=table["from"],
                    entities=entities,
                    group_by=(*table["group_by"], *table["entity_group_by"]),
                    sums=(*table["sum"], *table.get("entity_sum", [])),
                    units=units["name"],
                    count=count,
                    units_of=units[count],
                    minimum=aggregates["minimum_group"],
                )
            )
    return found


def plan(catalogue: Mapping[str, Any], aggregates: Mapping[str, Any], project: str) -> list[str]:
    """The statements the owner runs: every aggregate view, created or replaced."""
    return [v.sql(project) for v in views(catalogue, aggregates)]


def main(argv: Sequence[str], env: Mapping[str, str]) -> int:
    from ingest.load import LoadError, lab

    command = argv[0] if argv else ""
    if command not in ("plan", "apply"):
        print("usage: python -m access.thresholds plan|apply", file=sys.stderr)
        return 2
    try:
        project, where = lab(env)
        statements = plan(load(PERSONAS), load(AGGREGATES), project)
        if command == "plan":
            print("\n".join(statements))
            return 0
        for statement in statements:
            run_tool(query(project, where, statement))
    except (ThresholdError, RowPolicyError, LoadError) as error:
        print(f"thresholds: {error}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as error:
        # bq's message names the project and the accounts: only its exit status is reported.
        print(f"thresholds: bq failed with exit status {error.returncode}", file=sys.stderr)
        return 1
    print(f"thresholds: {len(statements)} views written")
    return 0


if __name__ == "__main__":
    import os

    sys.exit(main(sys.argv[1:], os.environ))
