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

A dataset's authorization covers every view in it, so a view the catalogues no longer give (a
table a purpose stopped listing) would keep reading its source for the purpose's personas. Each
run therefore lists what every purpose dataset holds (`bq ls`, its tables and its routines, free
metadata reads) before it writes: each view the module does not generate is dropped, and anything
else there that it did not generate (a table, a materialized view, any other kind of table, a
routine, or a name it cannot write as text, a generated name held by something other than a view
among them) is refused, never dropped, and stops the run before any view is written. The live
test (`make bigquery-purposes`) holds each purpose dataset to exactly its generated views, each
with its generated definition. A purpose removed from the catalogue leaves its dataset to
`platform/gcp`, whose next apply removes its grants and its authorization.

Views are DDL, so the owner's login runs them: `python -m access.purpose_views plan` lists the
datasets and prints the statements, the drops first, `apply` runs them. The project comes from
`LEDGER_LAB_GCP_PROJECT` and the location from `LEDGER_LAB_BQ_LOCATION`; nothing is written to
disk. Views store nothing, and listing, creating and dropping one bills nothing.

The owner's steps, in order, cost read before and after with `make cost-check`:
1. a targeted `tofu apply` of the additions alone, with the product grants still in place: the
   purpose datasets (`google_bigquery_dataset.warehouse["<purpose dataset>"]`), their views'
   authorizations (`google_bigquery_dataset_access.purposes_read_sources`) and the purpose grants
   (`google_bigquery_dataset_access.persona_reads_purposes`);
2. `python -m access.purpose_views plan`, then `apply`, which also drops each view not generated;
3. `LEDGER_LAB_PRODUCT_GRANTS=kept make bigquery-purposes`, which skips the one case that needs
   the product grants gone;
4. the full `tofu apply`, which removes the product grants;
5. `make bigquery-purposes`, every case.
"""

import json
import re
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from access.identities import UnknownPersonaError, identity
from access.row_policies import RowPolicyError, bq, copies, path, query, run_tool

ACCESS = Path(__file__).parent
PERSONAS = ACCESS / "personas.yaml"
COLUMNS = ACCESS / "columns.yaml"
PURPOSES = ACCESS / "purposes.yaml"
AGGREGATES = ACCESS / "aggregates.yaml"
# The model layers platform/gcp declares beside the products' and the aggregates' datasets.
LAYERS = ("landed", "intermediate")
# The statements are built as text, so every column in them is checked against its shape.
COLUMN = re.compile(r"^[a-z][a-z0-9_]*$")
# A name found in a purpose dataset that the module can write into a statement, as `path` checks.
NAME = re.compile(r"^[a-z][a-z0-9_]*$")
# The most entries one listing returns; a listing that reaches it may leave entries out. BigQuery's
# routine listing refuses more than 1000 (`maxResults must be less than or equal to the maximum of
# 1000`), so both listings ask for 1000.
LISTED = 1000
VIEW_KIND = "VIEW"
ROUTINE_KIND = "ROUTINE"

Run = Callable[[Sequence[str]], str]


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

    def select(self, project: str) -> str:
        """The view's query: the purpose's columns of its source, and nothing else."""
        for column in self.columns:
            if not COLUMN.fullmatch(column):
                raise PurposeError(f"{column!r} is not a column name")
        return (
            f"select {', '.join(self.columns)}"  # noqa: S608
            f" from {path(project, self.source_dataset, self.source)}"
        )

    def sql(self, project: str) -> str:
        return f"create or replace view {path(project, self.dataset, self.name)} as " + (
            self.select(project)
        )


@dataclass(frozen=True)
class Found:
    """An entry a purpose dataset holds: its name and kind, a table's type as `bq ls` gives it
    (`VIEW`, `TABLE`, `MATERIALIZED_VIEW` and the like) or `ROUTINE`."""

    dataset: str
    name: str
    kind: str


def list_command(project: str, where: str, dataset: str, *kind: str) -> list[str]:
    return bq(
        project,
        where,
        "--format=json",
        "ls",
        *kind,
        f"--max_results={LISTED}",
        f"{project}:{dataset}",
    )


def _listed(answer: str, dataset: str, reference: str, key: str, kind: str | None) -> list[Found]:
    try:
        entries: Any = json.loads(answer) if answer.strip() else []
        found = [
            Found(
                dataset,
                str(entry[reference][key]),
                kind or str(entry["type"]),
            )
            for entry in entries
        ]
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        raise PurposeError(
            f"the listing of {dataset} has an unexpected shape ({type(error).__name__})"
        ) from None
    if len(found) >= LISTED:
        raise PurposeError(f"the listing of {dataset} may leave entries out: nothing is written")
    return found


def contents(project: str, where: str, dataset: str, run: Run) -> list[Found]:
    """Every table, view and routine a purpose dataset holds, by `bq ls`."""
    tables = run(list_command(project, where, dataset))
    routines = run(list_command(project, where, dataset, "--routines"))
    return [
        *_listed(tables, dataset, "tableReference", "tableId", None),
        *_listed(routines, dataset, "routineReference", "routineId", ROUTINE_KIND),
    ]


@dataclass(frozen=True)
class Drift:
    """What the purpose datasets hold beyond the generated views: the views to drop, the entries
    refused (never dropped) and the generated views missing."""

    stale: tuple[Found, ...]
    refused: tuple[Found, ...]
    missing: tuple[View, ...]

    def refusal(self) -> str:
        """One line naming each refused entry by dataset and kind, and by name where the name is
        one the module could write; a name outside that shape is only counted."""
        named = [
            f"{e.dataset}.{e.name} ({e.kind.lower()})"
            for e in self.refused
            if NAME.fullmatch(e.name)
        ]
        odd = len(self.refused) - len(named)
        parts = named + ([f"{odd} more whose names it cannot write"] if odd else [])
        return (
            "the purpose datasets hold what this module does not generate and will not drop: "
            + "; ".join(parts)
            + ": the owner removes each by hand"
        )


def drift(generated: Sequence[View], found: Sequence[Found]) -> Drift:
    """The found entries the generated views do not account for: a view of a name the module
    could write and does not generate is stale, anything else not a generated view is refused."""
    wanted = {(v.dataset, v.name) for v in generated}
    stale: list[Found] = []
    refused: list[Found] = []
    for entry in found:
        if not NAME.fullmatch(entry.name) or entry.kind != VIEW_KIND:
            refused.append(entry)
        elif (entry.dataset, entry.name) not in wanted:
            stale.append(entry)
    present = {(e.dataset, e.name) for e in found if e.kind == VIEW_KIND}
    missing = tuple(v for v in generated if (v.dataset, v.name) not in present)
    return Drift(tuple(stale), tuple(refused), missing)


def drop_sql(project: str, entry: Found) -> str:
    return f"drop view if exists {path(project, entry.dataset, entry.name)}"


def definition(project: str, where: str, dataset: str, name: str, run: Run) -> str:
    """A view's stored query, by `bq show`."""
    answer = run(bq(project, where, "--format=json", "show", f"{project}:{dataset}.{name}"))
    try:
        return str(json.loads(answer)["view"]["query"])
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        raise PurposeError(
            f"{dataset}.{name} has no view definition ({type(error).__name__})"
        ) from None


def same_query(stored: str, generated: str) -> bool:
    """A stored view query against the generated one, compared without spacing, case or a trailing
    semicolon."""

    def normal(text: str) -> str:
        return re.sub(r"\s+", " ", text).strip().rstrip(";").strip().lower()

    return normal(stored) == normal(generated)


def survey(generated: Sequence[View], project: str, where: str, run: Run) -> Drift:
    """Each purpose dataset's contents against its generated views."""
    found = [
        entry
        for dataset in sorted({v.dataset for v in generated})
        for entry in contents(project, where, dataset, run)
    ]
    return drift(generated, found)


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


def run_plan(
    generated: Sequence[View], project: str, where: str, run: Run, write: bool
) -> tuple[list[str], list[str]]:
    """The drops of every stale view, then the generated views; with `write`, each is run. The
    drops run first, so a view the catalogues took away stops reading its source even when the
    rest is refused; a refused entry stops the run before any view is written."""
    found = survey(generated, project, where, run)
    drops = [drop_sql(project, e) for e in found.stale]
    for statement in drops if write else []:
        run(query(project, where, statement))
    if found.refused:
        raise PurposeError(found.refusal())
    creates = [v.sql(project) for v in generated]
    for statement in creates if write else []:
        run(query(project, where, statement))
    return drops, creates


def main(argv: Sequence[str], env: Mapping[str, str]) -> int:
    from ingest.load import LoadError, lab

    command = argv[0] if argv else ""
    if command not in ("plan", "apply"):
        print("usage: python -m access.purpose_views plan|apply", file=sys.stderr)
        return 2
    try:
        project, where = lab(env)
        generated = views(load(PERSONAS), load(COLUMNS), load(PURPOSES), load(AGGREGATES))
        drops, creates = run_plan(generated, project, where, run_tool, write=command == "apply")
        if command == "plan":
            print("\n".join([*drops, *creates]))
            return 0
    except (PurposeError, RowPolicyError, LoadError) as error:
        print(f"purpose-views: {error}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as error:
        # bq's message names the project and the accounts: only its exit status is reported.
        print(f"purpose-views: bq failed with exit status {error.returncode}", file=sys.stderr)
        return 1
    print(f"purpose-views: {len(creates)} views written, {len(drops)} not generated dropped")
    return 0


if __name__ == "__main__":
    import os

    sys.exit(main(sys.argv[1:], os.environ))
