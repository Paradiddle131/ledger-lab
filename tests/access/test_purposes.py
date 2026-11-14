"""The purposes acted out: each persona reads a product's rows only through its purposes' tables.

`access/purposes.yaml` names, per kind of work, the columns of each product table it needs and the
personas that do it; the compiler stores each purpose's columns as `<purpose>.<table>` in the
persona's database and never the table itself. Here each persona's connection is asked every
purpose's columns of every table: the query succeeds exactly for the purposes the persona holds,
asking a column none of its purposes lists fails, and no table of a product's name answers under
another purpose, under a schema that is no purpose, or as the source holds it, by its own name,
other than as the persona's aggregate table of that name. The same check is run on databases a
broken compiler would build, and goes red. The absent columns are worked out here from the
source's columns, and the metric definition's columns from the metrics' semantic model, apart
from the catalogue. A column absent is not a fact withheld: what the kept columns still tell is
pinned here too.
"""

import copy
import dataclasses
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml
from jsonschema import Draft202012Validator

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


def aggregate_columns(access: Doc, aggregates: Doc, persona: str) -> dict[str, set[str]]:
    """The persona's aggregate tables, each with the columns its aggregate grant shows: the only
    tables of `main` it holds, worked out from the aggregates catalogue apart from the compiler."""
    granted = {
        g["product"]
        for g in access["grants"]
        if g["persona"] == persona and g["access"] == "aggregate"
    }
    shown: dict[str, set[str]] = {}
    for product, entry in aggregates["products"].items():
        if product not in granted:
            continue
        for view in entry["tables"]:
            columns = [*view["group_by"], *view["sum"], view["units"]["name"]]
            if entry["entity_rows"]:
                columns += [*view["entity_group_by"], *view.get("entity_sum", [])]
            shown[view["name"]] = set(columns)
    return shown


def name(purpose: str, table: str) -> str:
    return f"{quote(schema_of(purpose))}.{quote(table)}"


def answers(db: duckdb.DuckDBPyConnection, sql: str) -> bool:
    """Whether the persona's connection answers the query; a missing table or column is a no."""
    try:
        db.execute(sql).fetchall()
    except REFUSED:
        return False
    return True


def held(db: duckdb.DuckDBPyConnection) -> list[tuple[str, str]]:
    """Every table and view of the persona's own catalog, as (schema, name), lowercased as DuckDB
    compares names."""
    rows = db.execute(
        "select lower(schema_name), lower(table_name) from duckdb_tables()"
        " where database_name = current_database()"
        " union all select lower(schema_name), lower(view_name) from duckdb_views()"
        " where database_name = current_database() and not internal"
    ).fetchall()
    return sorted(rows)


def breaches(
    db: duckdb.DuckDBPyConnection,
    persona: str,
    purposes: Doc,
    base: dict[str, list[str]],
    aggregated: dict[str, set[str]],
) -> list[str]:
    """Every way the persona's connection departs from its purposes, in names only: a purpose's
    table it holds and cannot read, a column of it the purpose does not list, any table of a
    product's name under a purpose that does not give it the table, under a schema that is no
    purpose, or in `main` other than as its aggregate table of that name with that table's
    columns."""
    found: list[str] = []
    for purpose in purposes["purposes"]:
        holds = persona in purpose["personas"]
        for table in base:
            where = name(purpose["id"], table)
            listed = purpose["tables"].get(table) if holds else None
            if listed is None:
                if answers(db, f"select * from {where} limit 0"):  # noqa: S608
                    whose = "which it does not hold" if not holds else "which does not list it"
                    found.append(f"{persona} reads {where}, of {purpose['id']}, {whose}")
                continue
            read = answers(db, f"select {', '.join(quote(c) for c in listed)} from {where}")  # noqa: S608
            if not read:
                found.append(f"{persona} cannot read {where}, which {purpose['id']} gives it")
            for column in base[table]:
                if column not in listed and answers(db, f"select {quote(column)} from {where}"):  # noqa: S608
                    found.append(f"{persona} reads {column} in {where}, which its purpose omits")
    for table in base:
        where = f"{quote(persona)}.main.{quote(table)}"
        if not answers(db, f"select * from {where} limit 0"):  # noqa: S608
            continue
        got = {c for (c,) in db.execute(f"select column_name from (describe {where})").fetchall()}  # noqa: S608
        if aggregated.get(table) != got:
            found.append(f"{persona} reads the product table {table} as {where}")
    purpose_schemas = {schema_of(p["id"]).lower() for p in purposes["purposes"]}
    for schema, table in held(db):
        if table in base and schema != "main" and schema not in purpose_schemas:
            found.append(f"{persona} reads {schema}.{table}, under a schema that is no purpose")
    return found


def test_each_persona_reads_its_purposes_and_nothing_else(
    as_persona: AsPersona,
    access: Doc,
    aggregates: Doc,
    purposes: Doc,
    warehouse: Path,
    persona: str,
) -> None:
    base = product_columns(access, warehouse)
    assert base, "the models build tables of some product"
    shown = aggregate_columns(access, aggregates, persona)
    assert breaches(as_persona(persona), persona, purposes, base, shown) == []


# A broken compiler: each change below is made to the plan the compiler gives, built as the
# compiler builds it, and the check above must find it.


def payments_of(plan: Plan) -> Rows:
    (rows,) = [t for t in plan.tables if isinstance(t, Rows) and t.table == "payments"]
    return rows


def only(rows: Rows, *names: str) -> Rows:
    """The table with only the named columns, in the source's order."""
    return dataclasses.replace(rows, columns=tuple(c for c in rows.columns if c.name in names))


def broken(name: str, plans: Callable[[str], Plan]) -> tuple[str, Plan, str]:
    """The persona whose plan a change breaks, the broken plan, and the breach it must show."""
    if name == "leave-the-base-table-in":
        plan = plans("accountant-chicago")
        whole = dataclasses.replace(payments_of(plan), purpose="main")
        return plan.persona, Plan(plan.persona, (*plan.tables, whole)), "the product table"
    if name == "leave-a-narrowed-base-table-in":
        plan = plans("group-controller")
        narrow = dataclasses.replace(only(payments_of(plan), "amount"), purpose="main")
        return plan.persona, Plan(plan.persona, (*plan.tables, narrow)), "the product table"
    if name == "give-another-purposes-table":
        plan = plans("accountant-chicago")
        other = dataclasses.replace(
            payments_of(plans("group-controller")), entities=frozenset({"chicago"})
        )
        return plan.persona, Plan(plan.persona, (*plan.tables, other)), "which it does not hold"
    if name == "give-one-column-of-another-purposes-table":
        plan = plans("group-controller")
        # Audit's payments, holding only the document number: none of group close's columns.
        other = dataclasses.replace(only(payments_of(plan), "amount"), purpose="audit")
        other = dataclasses.replace(other, columns=(Column("document_number", "VARCHAR", "clear"),))
        return plan.persona, Plan(plan.persona, (*plan.tables, other)), "which it does not hold"
    if name == "give-a-table-its-purpose-does-not-list":
        plan = plans("metric-owner")
        (totals,) = [t for t in plan.tables if isinstance(t, Rows)]
        extra = Rows(
            "group_totals_left_out", None, (Column("figures", "BIGINT", "clear"),), totals.purpose
        )
        return plan.persona, Plan(plan.persona, (*plan.tables, extra)), "which does not list it"
    if name == "store-under-a-schema-that-is-no-purpose":
        plan = plans("group-controller")
        spare = dataclasses.replace(only(payments_of(plan), "amount"), purpose="spare")
        return plan.persona, Plan(plan.persona, (*plan.tables, spare)), "no purpose"
    if name == "add-an-omitted-column":
        plan = plans("group-controller")
        rows = payments_of(plan)
        wider = dataclasses.replace(
            rows, columns=(*rows.columns, Column("contract_number", "VARCHAR", "clear"))
        )
        tables = tuple(wider if t is rows else t for t in plan.tables)
        return plan.persona, Plan(plan.persona, tables), "which its purpose omits"
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
        "leave-a-narrowed-base-table-in",
        "give-another-purposes-table",
        "give-one-column-of-another-purposes-table",
        "give-a-table-its-purpose-does-not-list",
        "store-under-a-schema-that-is-no-purpose",
        "add-an-omitted-column",
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
        shown = aggregate_columns(access, aggregates, persona)
        found = breaches(db, persona, purposes, product_columns(access, warehouse), shown)
    finally:
        db.close()
    assert len(found) == 1 and breach in found[0], found


def test_an_aggregate_table_of_a_product_tables_name_is_no_breach_with_its_own_columns(
    as_persona: AsPersona, access: Doc, aggregates: Doc
) -> None:
    """The accountants hold the group totals in aggregate as `main.group_totals`: the breach check
    passes that table only because its columns are the aggregate's, so the case is pinned."""
    shown = aggregate_columns(access, aggregates, "accountant-chicago")
    assert {"group_totals", "group_totals_left_out"} <= set(shown)
    db = as_persona("accountant-chicago")
    got = db.execute("select column_name from (describe main.group_totals)").fetchall()
    assert {c for (c,) in got} == shown["group_totals"]


# The columns the purposes leave out of each persona's purpose tables, worked out from the source's
# columns: before purposes, a persona with a row grant on a product read every column of every
# table of it. These are columns absent, not facts withheld: what a kept column still tells of an
# absent one is pinned in the tests after this one.
ABSENT_COLUMNS = {
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


def absent_columns(access: Doc, purposes: Doc, base: dict[str, list[str]]) -> dict[str, Any]:
    """Per persona and product table, the columns it read before purposes and its purposes' tables
    no longer hold."""
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


def test_the_columns_absent_from_each_persona_s_purpose_tables(
    access: Doc, purposes: Doc, warehouse: Path
) -> None:
    found = absent_columns(access, purposes, product_columns(access, warehouse))
    print("\ncolumns absent from the purposes' tables, compared with each persona's grants alone:")
    for persona, tables in found.items():
        print(f"  {persona}: {', '.join(f'{t} {len(c)}' for t, c in tables.items())}")
    assert found == ABSENT_COLUMNS


def test_the_group_controller_reads_whether_a_payee_is_a_person_from_the_organisation_name(
    as_persona: AsPersona, warehouse: Path
) -> None:
    """Group close leaves `vendor_is_hashed` out but keeps the organisation paid, which the models
    leave empty exactly for a person: so whether each payment went to a person stays readable to
    the group controller. A change that closes this must change this test."""
    controller = as_persona("group-controller")
    with pytest.raises(duckdb.BinderException):
        controller.execute("select vendor_is_hashed from group_close.payments")
    kept = dict(
        controller.execute(
            "select payment_key, vendor_organisation_name is null from group_close.payments"
        ).fetchall()
    )
    with duckdb.connect(str(warehouse), read_only=True) as source:
        truth = dict(
            source.execute("select payment_key, vendor_is_hashed from payments").fetchall()
        )
    assert any(truth.values()) and not all(truth.values()), "the sample pays persons and others"
    assert kept == truth


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


# The schema refuses a document the compiler could be handed wrong.


def schema_errors(doc: Doc) -> list[str]:
    schema = json.loads((REPO / "access/purposes.schema.json").read_text())
    return [
        "/".join(map(str, e.absolute_path)) for e in Draft202012Validator(schema).iter_errors(doc)
    ]


@pytest.mark.parametrize(
    ("change", "where"),
    [
        ("an-id-in-capitals", "purposes/0/id"),
        ("an-id-with-an-underscore", "purposes/0/id"),
        ("a-purpose-with-no-tables", "purposes/0/tables"),
    ],
)
def test_the_schema_refuses_a_bad_purposes_document(purposes: Doc, change: str, where: str) -> None:
    assert schema_errors(purposes) == []
    doc = copy.deepcopy(purposes)
    first = doc["purposes"][0]
    if change == "an-id-in-capitals":
        first["id"] = "MAIN"
    elif change == "an-id-with-an-underscore":
        first["id"] = "payment_review"
    else:
        first["tables"] = {}
    assert schema_errors(doc) == [where]


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
SPANS = {"chicago": 93, "uk-transport": 12, "nl-government": 3}
LEFT_OUT = {"chicago": 12, "nl-government": 1}
LANDED_READ = {
    "accountant-chicago": {"payment_review.payments": (17, CHICAGO)},
    "accountant-uk-transport": {"payment_review.payments": (17, UK)},
    "accountant-nl-government": {"payment_review.payments": (17, {})},
    "group-controller": {
        "group_close.payments": (10, {**CHICAGO, **UK}),
        "group_close.group_totals": (15, SPANS),
        "group_close.group_totals_left_out": (8, LEFT_OUT),
    },
    "payables-clerk": {"payment_review.payments": (17, CHICAGO)},
    "approver": {"payment_review.payments": (17, CHICAGO)},
    "auditor": {
        "audit.payments": (17, {**CHICAGO, **UK}),
        "audit.group_totals": (15, SPANS),
        "audit.group_totals_left_out": (8, LEFT_OUT),
    },
    "metric-owner": {"metric_definition.group_totals": (9, SPANS)},
}


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
            shown = aggregate_columns(access, aggregates, persona)
            found += breaches(db, persona, purposes, base, shown)
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
    # The rows counted again in the source, read with full rights.
    with duckdb.connect(str(landed_warehouse), read_only=True) as db:
        for table, want in (("payments", {**CHICAGO, **UK}), ("group_totals", SPANS)):
            got = db.execute(f"select entity_id, count(*) from {table} group by 1").fetchall()  # noqa: S608
            assert dict(got) == want, table
        got = db.execute("select entity_id, count(*) from group_totals_left_out group by 1")
        assert dict(got.fetchall()) == LEFT_OUT
