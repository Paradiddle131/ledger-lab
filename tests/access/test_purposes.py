"""The purposes acted out: each persona reads a product's rows only through its purposes' tables.

`access/purposes.yaml` names, per kind of work, the columns of each product table it needs and the
personas that do it; the compiler stores each purpose's columns as `<purpose>.<table>` in the
persona's database and never the table itself. Here each persona's connection is asked every
purpose's columns of every table: the query succeeds exactly for the purposes the persona holds,
asking a column its purpose withholds fails, and so does asking a product table as the source holds
it, by its own name. The same check is run on databases a broken compiler would build, and goes
red. The withheld columns are worked out here from the source's columns, and the metric
definition's columns from the metrics' semantic model, apart from the catalogue.
"""

import dataclasses
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml

from access.compile import (
    Column,
    Plan,
    Rows,
    compile_persona,
    connect,
    materialise,
    quote,
    schema_of,
    source_schema,
)
from tests.access.test_compile import LANDED_SOURCES
from tests.conftest import REPO, landed_file

AsPersona = Callable[[str], duckdb.DuckDBPyConnection]
Doc = dict[str, Any]
# What a query refused for want of the table or the column raises.
REFUSED = (duckdb.CatalogException, duckdb.BinderException)


def product_columns(access: Doc, warehouse: Path) -> dict[str, list[str]]:
    """Each product table with its columns as the source holds them: what a reader of the product
    read before purposes, and what a base-table query asks."""
    schema = source_schema(warehouse)
    return {
        table: [c for c, _ in schema[table]]
        for product in access["data_products"]
        for table in product.get("tables", [])
    }


def name(purpose: str, table: str) -> str:
    return f"{quote(schema_of(purpose))}.{quote(table)}"


def answers(db: duckdb.DuckDBPyConnection, sql: str) -> bool:
    """Whether the persona's connection answers the query; a missing table or column is a no."""
    try:
        db.execute(sql).fetchall()
    except REFUSED:
        return False
    return True


def breaches(
    db: duckdb.DuckDBPyConnection, persona: str, purposes: Doc, base: dict[str, list[str]]
) -> list[str]:
    """Every way the persona's connection departs from its purposes, in names only: a purpose's
    table it holds and cannot read, or can read and does not hold, a withheld column it reads, and
    a product table it reads as the source holds it."""
    found: list[str] = []
    for purpose in purposes["purposes"]:
        holds = persona in purpose["personas"]
        for table, listed in purpose["tables"].items():
            where = name(purpose["id"], table)
            read = answers(db, f"select {', '.join(quote(c) for c in listed)} from {where}")  # noqa: S608
            if holds and not read:
                found.append(f"{persona} cannot read {where}, which {purpose['id']} gives it")
            if not holds and read:
                found.append(f"{persona} reads {where}, of {purpose['id']}, which it does not hold")
            for column in base[table] if holds else []:
                if column not in listed and answers(db, f"select {quote(column)} from {where}"):  # noqa: S608
                    found.append(
                        f"{persona} reads {column} in {where}, which its purpose withholds"
                    )
    for table, columns in base.items():
        every = ", ".join(quote(c) for c in columns)
        where = f"{quote(persona)}.main.{quote(table)}"
        if answers(db, f"select {every} from {where}"):  # noqa: S608
            found.append(f"{persona} reads the product table {table} as {where}")
    return found


def test_each_persona_reads_its_purposes_and_nothing_else(
    as_persona: AsPersona, access: Doc, purposes: Doc, warehouse: Path, persona: str
) -> None:
    base = product_columns(access, warehouse)
    assert base, "the models build tables of some product"
    assert breaches(as_persona(persona), persona, purposes, base) == []


# A broken compiler: each change below is made to the plan the compiler gives, built as the
# compiler builds it, and the check above must find it.


def payments_of(plan: Plan) -> Rows:
    (rows,) = [t for t in plan.tables if isinstance(t, Rows) and t.table == "payments"]
    return rows


def broken(name: str, plans: Callable[[str], Plan]) -> tuple[str, Plan, str]:
    """The persona whose plan a change breaks, the broken plan, and the breach it must show."""
    if name == "leave-the-base-table-in":
        plan = plans("accountant-chicago")
        whole = dataclasses.replace(payments_of(plan), purpose="main")
        return plan.persona, Plan(plan.persona, (*plan.tables, whole)), "the product table"
    if name == "give-another-purposes-table":
        plan = plans("accountant-chicago")
        other = dataclasses.replace(
            payments_of(plans("group-controller")), entities=frozenset({"chicago"})
        )
        return plan.persona, Plan(plan.persona, (*plan.tables, other)), "which it does not hold"
    if name == "add-a-withheld-column":
        plan = plans("group-controller")
        rows = payments_of(plan)
        wider = dataclasses.replace(
            rows, columns=(*rows.columns, Column("contract_number", "VARCHAR", "clear"))
        )
        tables = tuple(wider if t is rows else t for t in plan.tables)
        return plan.persona, Plan(plan.persona, tables), "which its purpose withholds"
    assert name == "drop-a-listed-column"
    plan = plans("accountant-chicago")
    rows = payments_of(plan)
    narrower = dataclasses.replace(
        rows, columns=tuple(c for c in rows.columns if c.name != "amount")
    )
    tables = tuple(narrower if t is rows else t for t in plan.tables)
    return plan.persona, Plan(plan.persona, tables), "cannot read"


@pytest.mark.parametrize(
    "change",
    [
        "leave-the-base-table-in",
        "give-another-purposes-table",
        "add-a-withheld-column",
        "drop-a-listed-column",
    ],
)
def test_the_check_goes_red_on_a_database_a_broken_compiler_builds(
    access: Doc,
    columns: Doc,
    aggregates: Doc,
    purposes: Doc,
    warehouse: Path,
    tmp_path: Path,
    change: str,
) -> None:
    schema = source_schema(warehouse)

    def plans(persona: str) -> Plan:
        return compile_persona(access, columns, aggregates, purposes, persona, schema)

    persona, plan, breach = broken(change, plans)
    db = connect(materialise(plan, warehouse, tmp_path))
    try:
        found = breaches(db, persona, purposes, product_columns(access, warehouse))
    finally:
        db.close()
    assert len(found) == 1 and breach in found[0], found


# What the purposes withhold, worked out from the source's columns: before purposes, a persona with
# a row grant on a product read every column of every table of it.
WITHHELD = {
    "group-controller": {
        "payments": [
            "vendor_name",
            "vendor_is_hashed",
            "document_number",
            "contract_number",
            "award_after_revisions",
            "vendor_person_key",
            "document_reference",
        ],
    },
    "metric-owner": {
        "group_totals": [
            "period_end",
            "figures",
            "payees",
            "currency",
            "amount",
            "amount_at_actual_rates",
        ],
        "group_totals_left_out": [
            "entity_id",
            "fiscal_year",
            "basis",
            "left_out_by",
            "figures",
            "payees",
            "currency",
            "amount",
        ],
    },
}


def withheld(access: Doc, purposes: Doc, base: dict[str, list[str]]) -> dict[str, Any]:
    """Per persona and product table, the columns it read before purposes and reads no longer."""
    tables = {p["id"]: p.get("tables", []) for p in access["data_products"]}
    found: dict[str, dict[str, list[str]]] = {}
    for grant in access["grants"]:
        if grant["access"] not in ("read", "edit"):
            continue
        persona = grant["persona"]
        for table in tables[grant["product"]]:
            listed = {
                c
                for p in purposes["purposes"]
                if persona in p["personas"]
                for c in p["tables"].get(table, [])
            }
            if gone := [c for c in base[table] if c not in listed]:
                found.setdefault(persona, {})[table] = gone
    return found


def test_what_the_purposes_withhold_from_each_persona(
    access: Doc, purposes: Doc, warehouse: Path
) -> None:
    found = withheld(access, purposes, product_columns(access, warehouse))
    print("\nwithheld by the purposes, compared with each persona's grants alone:")
    for persona, tables in found.items():
        print(f"  {persona}: {', '.join(f'{t} {len(c)}' for t, c in tables.items())}")
    assert found == WITHHELD


def test_a_purpose_that_withholds_nothing_serves_a_duty_that_reads_every_column(
    access: Doc, purposes: Doc, warehouse: Path
) -> None:
    """Two purposes give every column of their tables; the duties of their personas are the
    ones that follow a payment or a figure to everything recorded about it, stated here."""
    base = product_columns(access, warehouse)
    whole = {
        p["id"]
        for p in purposes["purposes"]
        if all(set(c) == set(base[t]) for t, c in p["tables"].items())
    }
    assert whole == {"payment-review", "audit"}


def semantic_columns(table: str, columns: list[str]) -> set[str]:
    """The columns of the table the metrics' semantic model over it reads, by name or in an
    expression."""
    doc = yaml.safe_load((REPO / "semantics/metricflow" / f"{table}.yml").read_text())
    (model,) = [m for m in doc["semantic_models"] if m["name"] == table]
    words: set[str] = set()
    for kind in ("entities", "dimensions", "measures"):
        for item in model.get(kind, []):
            words |= set(re.findall(r"[a-z_]+", item.get("expr", item["name"])))
    return words & set(columns)


def test_the_metric_definition_reads_exactly_what_the_metrics_read(
    access: Doc, purposes: Doc, warehouse: Path
) -> None:
    (definition,) = [p for p in purposes["purposes"] if p["id"] == "metric-definition"]
    base = product_columns(access, warehouse)
    for table, listed in definition["tables"].items():
        assert set(listed) == semantic_columns(table, base[table]), table


# Over the whole landed build.


@pytest.fixture(scope="module")
def landed_warehouse(tmp_path_factory: pytest.TempPathFactory) -> Path:
    from ingest.fetch import landing_path
    from tests.models.dbt_build import built

    for source in LANDED_SOURCES:
        landed_file(source)
    return built(tmp_path_factory.mktemp("dbt-landed"), "landed", landing_path().resolve()).database


# Per persona, each purpose's table it reads: its columns and its rows by entity. The readers of
# one entity's payments read that entity's rows; the group controller and the auditor every
# entity's; the Dutch government's payments have no rows.
CHICAGO = {"chicago": 475609}
UK = {"uk-transport": 31103}
BOTH = {**CHICAGO, **UK}
LANDED_READ: dict[str, dict[str, tuple[int, dict[str, int]]]] = {}


@pytest.mark.landed
def test_each_persona_reads_its_purposes_over_the_landed_build(
    access: Doc,
    columns: Doc,
    aggregates: Doc,
    purposes: Doc,
    landed_warehouse: Path,
    tmp_path: Path,
) -> None:
    base = product_columns(access, landed_warehouse)
    schema = source_schema(landed_warehouse)
    read: dict[str, dict[str, tuple[int, dict[str, int]]]] = {}
    found: list[str] = []
    for persona in (p["id"] for p in access["personas"]):
        directory = tmp_path / persona
        directory.mkdir()
        plan = compile_persona(access, columns, aggregates, purposes, persona, schema)
        db = connect(materialise(plan, landed_warehouse, directory))
        try:
            found += breaches(db, persona, purposes, base)
            for rows in (t for t in plan.tables if isinstance(t, Rows)):
                where = name(rows.purpose, rows.table)
                by = "entity_id" if "entity_id" in {c.name for c in rows.columns} else "'all'"
                counted = db.execute(f"select {by}, count(*) from {where} group by 1").fetchall()  # noqa: S608
                read.setdefault(persona, {})[rows.name] = (len(rows.columns), dict(counted))
        finally:
            db.close()
    print("\npurposes held over the landed build: columns, rows by entity")
    for persona, tables in read.items():
        for table, (n, rows) in tables.items():
            print(f"  {persona} {table}: {n} columns, {rows}")
    assert found == []
    assert read == LANDED_READ
