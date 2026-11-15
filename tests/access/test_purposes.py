"""The purposes acted out: each persona reads a product's rows only through its purposes' tables.

`access/purposes.yaml` names, per kind of work, the columns of each product table it needs and the
personas that do it; the compiler stores each purpose's columns as `<purpose>.<table>` in the
persona's database, and not the table itself. Here each persona's database is checked against
its purposes and aggregate grants, reading `duckdb_tables()`, `duckdb_views()`,
`duckdb_columns()`, `duckdb_functions()`, `duckdb_types()`, `duckdb_sequences()`,
`duckdb_constraints()`, `duckdb_indexes()` and `duckdb_schemas()` by qualified name. The check
flags:
- a purpose table missing, not answering a query of the purpose's columns, holding a column the
  purpose does not list, stored as a view, or holding a column in another type than the source's;
- an aggregate table missing, stored as a view, holding other columns, a column in another type
  than the aggregate's, a group twice, or a group whose payees are null or under the minimum;
- a table or view of the persona's catalog, in any schema, that no purpose or aggregate gives it;
- a macro, a type that is not internal, a sequence, a constraint, an index, a schema other than
  `main` and the persona's purpose schemas, a comment on a table, view or column, and a column
  default, a generated column among them (DuckDB reports its expression as the column's default).
Its limits:
- it compares names and types; in an aggregate table it also counts the rows and the distinct
  groups, and compares each group's payees with the minimum;
- it does not look at which rows a purpose table holds or how many: one holding another entity's
  rows, or each row twice, passes it; the entity filter is checked elsewhere:
  `tests/access/test_compile.py`, `tests/access/test_visibility.py` and
  `tests/access/test_identities.py` go red when the compiler's entity filter is dropped from its
  purpose and aggregate tables;
- it does not look at the values a purpose table's listed column holds: a listed column
  overwritten with another listed column's values passes it;
- of the catalogue it reads only the functions above and `current_database()`.
The same check is run on databases a broken compiler would build, at least one for each item
flagged above and one for each catalogue function above, and `current_database()`, shadowed by
a stored macro of its name, and goes red on each. The absent columns are worked out here from
the source's columns, and the metric definition's columns from the metrics' semantic model, apart
from the catalogue.
"""

import copy
import dataclasses
import json
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml
from jsonschema import Draft202012Validator

from access.compile import (
    SOURCE,
    Aggregate,
    Column,
    Plan,
    Rows,
    compile_persona,
    connect,
    literal,
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
    read before purposes."""
    schema = source_schema(warehouse)
    return {
        table: [c for c, _ in schema[table]]
        for product in access["data_products"]
        for table in product.get("tables", [])
    }


@dataclass(frozen=True)
class Shape:
    """An aggregate table as its grant shows it: its columns with their types, the ones it groups
    by, the column counting each group's payees and the fewest a group may hold."""

    types: tuple[tuple[str, str], ...]
    keys: tuple[str, ...]
    units: str
    minimum: int

    @property
    def columns(self) -> frozenset[str]:
        return frozenset(c for c, _ in self.types)


def product_types(access: Doc, warehouse: Path) -> dict[str, dict[str, str]]:
    """Each product table with its columns' types as the source holds them."""
    schema = source_schema(warehouse)
    return {
        table: dict(schema[table])
        for product in access["data_products"]
        for table in product.get("tables", [])
    }


def sum_type(kind: str) -> str:
    """The type DuckDB gives the sum of a column of that type."""
    with duckdb.connect() as engine:
        (got,) = engine.execute(f"select typeof(sum(cast(null as {kind})))").fetchone() or ("",)
    return str(got)


def aggregate_columns(
    access: Doc, aggregates: Doc, persona: str, kinds: dict[str, dict[str, str]]
) -> dict[str, Shape]:
    """The persona's aggregate tables, each as its aggregate grant shows it: the tables of `main`
    it may hold, worked out from the aggregates catalogue and the source's types apart from the
    compiler. A key keeps the source's type, a sum takes DuckDB's type for that sum, and the
    payees are a count (BIGINT) or the largest of the source's own counts."""
    granted = {
        g["product"]
        for g in access["grants"]
        if g["persona"] == persona and g["access"] == "aggregate"
    }
    shown: dict[str, Shape] = {}
    for product, entry in aggregates["products"].items():
        if product not in granted:
            continue
        for view in entry["tables"]:
            if view["from"] not in kinds:
                continue  # the models build no table of it yet
            source = kinds[view["from"]]
            keys, sums = list(view["group_by"]), list(view["sum"])
            if entry["entity_rows"]:
                keys += view["entity_group_by"]
                sums += view.get("entity_sum", [])
            counted = view["units"]
            units = counted["name"]
            unit_type = "BIGINT" if "count_distinct" in counted else source[counted["max"]]
            types = (
                *((k, source[k]) for k in keys),
                *((s, sum_type(source[s])) for s in sums),
                (units, unit_type),
            )
            shown[view["name"]] = Shape(types, tuple(keys), units, aggregates["minimum_group"])
    return shown


@dataclass(frozen=True)
class Expected:
    """What the persona's purposes and aggregate grants give it: each purpose table, by (schema,
    table), with its purpose and its columns' types; each aggregate table; and its schemas."""

    tables: dict[tuple[str, str], tuple[str, dict[str, str]]]
    aggregated: dict[str, Shape]
    schemas: frozenset[str]


def expected(
    access: Doc, aggregates: Doc, purposes: Doc, persona: str, warehouse: Path
) -> Expected:
    kinds = product_types(access, warehouse)
    mine = [p for p in purposes["purposes"] if persona in p["personas"]]
    tables = {
        (schema_of(p["id"]).lower(), table): (
            p["id"],
            {c: kinds[table][c] for c in listed},
        )
        for p in mine
        for table, listed in p["tables"].items()
    }
    schemas = frozenset({"main", *(schema_of(p["id"]).lower() for p in mine)})
    return Expected(tables, aggregate_columns(access, aggregates, persona, kinds), schemas)


def name(purpose: str, table: str) -> str:
    return f"{quote(schema_of(purpose))}.{quote(table)}"


def answers(db: duckdb.DuckDBPyConnection, sql: str) -> bool:
    """Whether the persona's connection answers the query; a missing table or column is a no."""
    try:
        db.execute(sql).fetchall()
    except REFUSED:
        return False
    return True


# The catalogue functions are called by qualified name, so a macro stored in the persona's database
# under the same name does not stand in for them; a broken-database case pins each one.
CATALOG = "system.main.current_database()"


@dataclass(frozen=True)
class Held:
    """A table or view of the persona's catalog as the catalogue shows it: its kind, its comment,
    and each column's type, comment and default."""

    kind: str
    comment: str | None
    columns: dict[str, tuple[str, str | None, str | None]]


def held(db: duckdb.DuckDBPyConnection) -> dict[tuple[str, str], Held]:
    """Each table and view of the persona's own catalog, as (schema, name) lowercased as DuckDB
    compares names, read from `duckdb_tables()`, `duckdb_views()` and `duckdb_columns()`."""
    rows = db.execute(
        "select lower(t.schema_name), lower(t.table_name), t.kind, t.comment,"  # noqa: S608
        " lower(c.column_name), c.data_type, c.comment, c.column_default"
        " from (select schema_name, table_name, 'table' as kind, comment"
        f"  from system.main.duckdb_tables() where database_name = {CATALOG}"
        "  union all select schema_name, view_name, 'view', comment"
        f"  from system.main.duckdb_views() where database_name = {CATALOG} and not internal) as t"
        f" left join system.main.duckdb_columns() as c on c.database_name = {CATALOG}"
        "  and c.schema_name = t.schema_name and c.table_name = t.table_name"
    ).fetchall()
    found: dict[tuple[str, str], Held] = {}
    for schema, table, kind, comment, column, data_type, noted, default in rows:
        entry = found.setdefault((schema, table), Held(kind, comment, {}))
        if column:
            entry.columns[column] = (data_type, noted, default)
    return found


def names(db: duckdb.DuckDBPyConnection, sql: str) -> list[str]:
    return sorted(str(name) for (name,) in db.execute(sql).fetchall())


def stored(
    db: duckdb.DuckDBPyConnection,
    persona: str,
    tables: dict[tuple[str, str], Held],
    schemas: frozenset[str],
) -> list[str]:
    """What the persona's own catalog holds beside its tables' and views' names and columns, from
    the catalogue functions named here: a function (`duckdb_functions()`, so any macro), a type
    that is not internal (`duckdb_types()`), a sequence (`duckdb_sequences()`), a constraint
    (`duckdb_constraints()`), an index (`duckdb_indexes()`), a schema other than `main` and the
    persona's purpose schemas (`duckdb_schemas()`), and a comment on a table, view or column or a
    column default, a generated column's expression among them (from `held`)."""
    found: list[str] = []
    for macro in names(
        db,
        "select schema_name || '.' || function_name from system.main.duckdb_functions()"  # noqa: S608
        f" where database_name = {CATALOG}",
    ):
        found.append(f"{persona} holds the macro {macro}")
    for kind in names(
        db,
        "select schema_name || '.' || type_name from system.main.duckdb_types()"  # noqa: S608
        f" where database_name = {CATALOG} and not internal",
    ):
        found.append(f"{persona} holds the type {kind}")
    for sequence in names(
        db,
        "select schema_name || '.' || sequence_name from system.main.duckdb_sequences()"  # noqa: S608
        f" where database_name = {CATALOG}",
    ):
        found.append(f"{persona} holds the sequence {sequence}")
    for constraint in names(
        db,
        "select constraint_type || ' constraint on ' || schema_name || '.' || table_name"  # noqa: S608
        f" from system.main.duckdb_constraints() where database_name = {CATALOG}",
    ):
        found.append(f"{persona} holds a {constraint}")
    for index in names(
        db,
        "select schema_name || '.' || index_name from system.main.duckdb_indexes()"  # noqa: S608
        f" where database_name = {CATALOG}",
    ):
        found.append(f"{persona} holds the index {index}")
    for schema in names(
        db,
        "select lower(schema_name) from system.main.duckdb_schemas()"  # noqa: S608
        f" where database_name = {CATALOG}",
    ):
        if schema not in schemas:
            found.append(f"{persona} holds the schema {schema}, which no purpose gives it")
    for (schema, table), entry in sorted(tables.items()):
        if entry.comment is not None:
            found.append(f"{persona} holds a comment on {schema}.{table}")
    for (schema, table), entry in sorted(tables.items()):
        for column, (_, noted, _) in sorted(entry.columns.items()):
            if noted is not None:
                found.append(f"{persona} holds a comment on {schema}.{table}.{column}")
    for (schema, table), entry in sorted(tables.items()):
        for column, (_, _, default) in sorted(entry.columns.items()):
            if default is not None:
                found.append(f"{persona} holds a default on {schema}.{table}.{column}")
    return found


def retyped(entry: Held, want: dict[str, str]) -> list[str]:
    """Each column the table holds in another type than the one wanted, as `column as type`."""
    return [
        f"{column} as {entry.columns[column][0]}, not {kind}"
        for column, kind in sorted(want.items())
        if column in entry.columns and entry.columns[column][0] != kind
    ]


def aggregate_faults(db: duckdb.DuckDBPyConnection, where: str, shape: Shape) -> list[str]:
    """How a table in `main` departs from the aggregate it stands for: a group held twice, or a
    group whose payees are null or under the minimum."""
    keys = ", ".join(quote(k) for k in shape.keys)
    (rows,) = db.execute(f"select count(*) from {where}").fetchone() or (0,)  # noqa: S608
    distinct = f"select count(*) from (select distinct {keys} from {where})"  # noqa: S608
    (groups,) = db.execute(distinct).fetchone() or (0,)
    units = quote(shape.units)
    (small,) = db.execute(
        f"select count(*) from {where} where {units} is null or {units} < {shape.minimum}"  # noqa: S608
    ).fetchone() or (0,)
    faults = []
    if groups != rows:
        faults.append("a group held twice")
    if small:
        faults.append("a group under the minimum")
    return faults


def breaches(db: duckdb.DuckDBPyConnection, persona: str, want: Expected) -> list[str]:
    """The departures of the persona's database from its purposes and aggregate grants that this
    reads for, by names and types, and in an aggregate table by its rows, groups and payees:
    - a purpose table missing, not answering a query of the purpose's columns, holding a column
      the purpose does not list, stored as a view, or holding a column in another type than the
      source's;
    - an aggregate table missing, stored as a view, holding other columns, a column in another
      type than the aggregate's, a group twice, or a group whose payees are null or under the
      minimum group;
    - a table or view of its catalog, in any schema, that no purpose or aggregate gives it;
    - what `stored` lists.
    It does not look at a purpose table's rows or its listed columns' values, and of the catalogue
    reads only the functions `held` and `stored` name and `current_database()`."""
    found: list[str] = []
    tables = held(db)
    for (schema, table), (purpose, kinds) in want.tables.items():
        where = f"{quote(schema)}.{quote(table)}"
        got = tables.get((schema, table))
        listed = ", ".join(map(quote, kinds))
        if got is None or not answers(db, f"select {listed} from {where}"):  # noqa: S608
            found.append(f"{persona} cannot read {schema}.{table}, which {purpose} gives it")
        elif extra := sorted(set(got.columns) - set(kinds)):
            found.append(
                f"{persona} reads {', '.join(extra)} in {schema}.{table}, which {purpose} omits"
            )
        elif got.kind != "table":
            found.append(f"{persona} holds {schema}.{table} as a view, which {purpose} gives it")
        elif other := retyped(got, kinds):
            found.append(f"{persona} holds in {schema}.{table} {', '.join(other)}")
    for table, shape in want.aggregated.items():
        where = f"main.{quote(table)}"
        got = tables.get(("main", table))
        if got is None:
            found.append(f"{persona} cannot read main.{table}, which its aggregate grant gives it")
            continue
        if got.kind != "table":
            faults = ["a view"]
        elif set(got.columns) != shape.columns:
            faults = ["other columns"]
        else:
            faults = retyped(got, dict(shape.types)) + aggregate_faults(db, where, shape)
        if faults:
            found.append(f"{persona} holds main.{table} as no aggregate: {', '.join(faults)}")
    given = set(want.tables) | {("main", t) for t in want.aggregated}
    for schema, table in sorted(set(tables) - given):
        found.append(f"{persona} holds {schema}.{table}, which no purpose or aggregate gives it")
    return found + stored(db, persona, tables, want.schemas)


def test_each_persona_s_database_shows_none_of_the_breaches_checked(
    as_persona: AsPersona,
    access: Doc,
    aggregates: Doc,
    purposes: Doc,
    warehouse: Path,
    persona: str,
) -> None:
    assert product_types(access, warehouse), "the models build tables of some product"
    want = expected(access, aggregates, purposes, persona, warehouse)
    assert breaches(as_persona(persona), persona, want) == []


# A broken compiler: each change below is made to the plan the compiler gives, or to the database
# it builds, and the check above must find it.


def payments_of(plan: Plan) -> Rows:
    (rows,) = [t for t in plan.tables if isinstance(t, Rows) and t.table == "payments"]
    return rows


def only(rows: Rows, *names: str) -> Rows:
    """The table with only the named columns, in the source's order."""
    return dataclasses.replace(rows, columns=tuple(c for c in rows.columns if c.name in names))


def swap_aggregate(plan: Plan, table: str, columns: tuple[Column, ...]) -> Plan:
    """The plan with the aggregate table of that name replaced by the source's rows, ungrouped."""
    (agg,) = [t for t in plan.tables if isinstance(t, Aggregate) and t.name == table]
    raw = Rows(agg.source, None, columns, "main")
    return Plan(plan.persona, tuple(raw if t is agg else t for t in plan.tables))


Build = duckdb.DuckDBPyConnection
Statements = list[str] | Callable[[Build], list[str]]


def columns_of(build: Build, schema: str, table: str) -> list[tuple[str, str]]:
    return [
        (str(c), str(k))
        for c, k in build.execute(
            "select column_name, data_type from system.main.duckdb_columns()"
            " where database_name = system.main.current_database()"
            " and schema_name = ? and table_name = ? order by column_index",
            [schema, table],
        ).fetchall()
    ]


def rebuilt_with(schema: str, table: str, extra: str) -> Callable[[Build], list[str]]:
    """The table written again under its name with its columns, types and rows, and `extra`
    added to its definition."""

    def statements(build: Build) -> list[str]:
        where = f"{quote(schema)}.{quote(table)}"
        defs = ", ".join(f"{quote(c)} {k}" for c, k in columns_of(build, schema, table))
        return [
            f"create table main.kept as select * from {where}",  # noqa: S608
            f"drop table {where}",
            f"create table {where} ({defs}, {extra})",
            f"insert into {where} by name select * from main.kept",  # noqa: S608
            "drop table main.kept",
        ]

    return statements


def generated(schema: str, table: str, column: str, value: str) -> Callable[[Build], list[str]]:
    """The table written again under its name with its columns, types and rows, the column stored
    as a column generated always as the constant `value`."""

    def statements(build: Build) -> list[str]:
        where = f"{quote(schema)}.{quote(table)}"
        defs = ", ".join(
            f"{quote(c)} {k}" + (f" generated always as ({literal(value)})" if c == column else "")
            for c, k in columns_of(build, schema, table)
        )
        return [
            f"create table main.kept as select * exclude ({quote(column)}) from {where}",  # noqa: S608
            f"drop table {where}",
            f"create table {where} ({defs})",
            f"insert into {where} by name select * from main.kept",  # noqa: S608
            "drop table main.kept",
        ]

    return statements


def as_view(schema: str, table: str) -> Callable[[Build], list[str]]:
    """The table replaced by a view of one row of nulls under its name, its columns and types
    kept."""

    def statements(build: Build) -> list[str]:
        where = f"{quote(schema)}.{quote(table)}"
        held = columns_of(build, schema, table)
        nulls = ", ".join(f"cast(null as {k})" for _, k in held)
        named = ", ".join(quote(c) for c, _ in held)
        return [
            f"drop table {where}",
            f"create view {where} as select * from (values ({nulls})) as v({named})",  # noqa: S608
        ]

    return statements


def as_enum(schema: str, table: str, column: str) -> Callable[[Build], list[str]]:
    """The column's type changed to an inline ENUM of its values and one more."""

    def statements(build: Build) -> list[str]:
        where = f"{quote(schema)}.{quote(table)}"
        values = [
            str(v)
            for (v,) in build.execute(
                f"select distinct {quote(column)} from {where} where {quote(column)} is not null"  # noqa: S608
            ).fetchall()
        ]
        labels = ", ".join(literal(v) for v in sorted({*values, "invented"}))
        return [f"alter table {where} alter column {quote(column)} type enum({labels})"]

    return statements


def hidden_by(function: str, where: str) -> str:
    """A stored macro under a catalogue function's name that leaves out what `where` matches."""
    return (
        f"create macro main.{function}() as table select * from system.main.{function}()"  # noqa: S608
        f" where not ({where})"
    )


# Changes made to the built database, with the source attached as the build attaches it: the
# persona, the statements, and the breaches the check must show, in the order it shows them.
ALTERED: dict[str, tuple[str, Statements, tuple[str, ...]]] = {
    "copy-a-product-table-under-another-name": (
        "group-controller",
        [f"create table main.ledger as select * from {SOURCE}.main.payments"],  # noqa: S608
        ("main.ledger, which no purpose",),
    ),
    "copy-a-product-table-under-another-schema": (
        "group-controller",
        [
            "create schema spare",
            f"create table spare.ledger as select * from {SOURCE}.main.payments",  # noqa: S608
        ],
        ("spare.ledger, which no purpose", "holds the schema spare"),
    ),
    "add-a-view": (
        "group-controller",
        ["create view main.close as select * from group_close.payments"],
        ("main.close, which no purpose",),
    ),
    "hold-an-aggregate-group-twice": (
        # Every group the metric owner's payments aggregate keeps, written a second time.
        "metric-owner",
        ["insert into main.payments_by_period select * from main.payments_by_period"],
        ("main.payments_by_period as no aggregate: a group held twice",),
    ),
    "add-a-column-under-a-new-name": (
        "group-controller",
        [
            "alter table group_close.payments add column payee varchar",
            "update group_close.payments set payee = 'invented'",
        ],
        ("reads payee in",),
    ),
    "drop-a-purpose-table": (
        "group-controller",
        ["drop table group_close.group_totals_left_out"],
        ("cannot read group_close.group_totals_left_out, which group-close gives it",),
    ),
    "drop-an-aggregate-table": (
        "metric-owner",
        ["drop table main.payments_by_period"],
        ("cannot read main.payments_by_period, which its aggregate grant",),
    ),
    "leave-an-aggregate-group-s-payees-null": (
        "metric-owner",
        [
            "update main.payments_by_period set payees = null"
            " where rowid = (select min(rowid) from main.payments_by_period)"
        ],
        ("main.payments_by_period as no aggregate: a group under the minimum",),
    ),
    "store-a-macro": (
        "group-controller",
        [
            f"create macro main.leak() as table select document_number from {SOURCE}.main.payments"  # noqa: S608
        ],
        ("holds the macro main.leak",),
    ),
    "store-a-type": (
        "group-controller",
        [
            "create type main.leak as enum (select distinct document_number"  # noqa: S608
            f" from {SOURCE}.main.payments where document_number is not null)"  # noqa: S608
        ],
        ("holds the type main.leak",),
    ),
    "store-a-sequence": (
        "group-controller",
        ["create sequence main.leak start 4242"],
        ("holds the sequence main.leak",),
    ),
    "store-a-comment-on-a-column": (
        "group-controller",
        ["comment on column group_close.payments.amount is 'invented'"],
        ("holds a comment on group_close.payments.amount",),
    ),
    "store-a-comment-on-a-table": (
        "group-controller",
        ["comment on table group_close.payments is 'invented'"],
        ("holds a comment on group_close.payments",),
    ),
    "store-a-comment-on-a-view": (
        "group-controller",
        [
            "create view main.close as select * from group_close.payments",
            "comment on view main.close is 'invented'",
        ],
        ("main.close, which no purpose", "holds a comment on main.close"),
    ),
    "store-a-column-default": (
        "group-controller",
        ["alter table group_close.payments alter column amount set default 4242"],
        ("holds a default on group_close.payments.amount",),
    ),
    "store-a-purpose-column-as-a-generated-column": (
        # DuckDB reports a generated column's expression as the column's default.
        "group-controller",
        generated("group_close", "payments", "source_table", "invented"),
        ("holds a default on group_close.payments.source_table",),
    ),
    "store-a-constraint": (
        "group-controller",
        rebuilt_with("group_close", "payments", "check (vendor_organisation_name <> 'invented')"),
        ("holds a CHECK constraint on group_close.payments",),
    ),
    "store-an-index": (
        "group-controller",
        [
            "create index leak on group_close.payments"
            " ((coalesce(vendor_organisation_name, '') = 'invented'))"
        ],
        ("holds the index group_close.leak",),
    ),
    "store-a-schema": (
        "group-controller",
        ["create schema leak"],
        ("holds the schema leak, which no purpose",),
    ),
    "hold-a-purpose-column-as-an-enum": (
        "group-controller",
        as_enum("group_close", "payments", "source_table"),
        ("holds in group_close.payments source_table as ENUM(",),
    ),
    "hold-an-aggregate-column-in-another-type": (
        "metric-owner",
        ["alter table main.payments_by_period alter column payees type double"],
        ("main.payments_by_period as no aggregate: payees as DOUBLE, not BIGINT",),
    ),
    "store-a-purpose-table-as-a-view": (
        "group-controller",
        as_view("group_close", "group_totals_left_out"),
        ("holds group_close.group_totals_left_out as a view",),
    ),
    "store-an-aggregate-table-as-a-view": (
        "metric-owner",
        as_view("main", "payments_by_period"),
        ("main.payments_by_period as no aggregate: a view",),
    ),
    # Each catalogue function the check reads, shadowed by a stored macro of its name that hides
    # one object: the check sees the object only while it reads the function by qualified name.
    "hide-a-table-behind-a-macro-named-like-the-catalogue": (
        "group-controller",
        [
            f"create table main.ledger as select * from {SOURCE}.main.payments",  # noqa: S608
            hidden_by("duckdb_tables", "table_name = 'ledger'"),
        ],
        ("main.ledger, which no purpose", "holds the macro main.duckdb_tables"),
    ),
    "hide-a-view-behind-a-macro-named-like-the-catalogue": (
        "group-controller",
        [
            "create view main.close as select * from group_close.payments",
            hidden_by("duckdb_views", "view_name = 'close'"),
        ],
        ("main.close, which no purpose", "holds the macro main.duckdb_views"),
    ),
    "hide-a-column-behind-a-macro-named-like-the-catalogue": (
        "group-controller",
        [
            "alter table group_close.payments add column payee varchar",
            hidden_by("duckdb_columns", "column_name = 'payee'"),
        ],
        ("reads payee in", "holds the macro main.duckdb_columns"),
    ),
    "hide-a-macro-behind-a-macro-named-like-the-catalogue": (
        "group-controller",
        [
            f"create macro main.leak() as table select document_number from {SOURCE}.main.payments",  # noqa: S608
            hidden_by("duckdb_functions", "function_name in ('leak', 'duckdb_functions')"),
        ],
        ("holds the macro main.duckdb_functions", "holds the macro main.leak"),
    ),
    "hide-a-type-behind-a-macro-named-like-the-catalogue": (
        "group-controller",
        [
            "create type main.leak as enum ('invented')",
            hidden_by("duckdb_types", "type_name = 'leak'"),
        ],
        ("holds the macro main.duckdb_types", "holds the type main.leak"),
    ),
    "hide-a-sequence-behind-a-macro-named-like-the-catalogue": (
        "group-controller",
        [
            "create sequence main.leak start 4242",
            hidden_by("duckdb_sequences", "sequence_name = 'leak'"),
        ],
        ("holds the macro main.duckdb_sequences", "holds the sequence main.leak"),
    ),
    "hide-a-constraint-behind-a-macro-named-like-the-catalogue": (
        "group-controller",
        lambda build: [
            *rebuilt_with("group_close", "payments", "check (amount <> 4242)")(build),
            hidden_by("duckdb_constraints", "table_name = 'payments'"),
        ],
        ("holds the macro main.duckdb_constraints", "holds a CHECK constraint on group_close"),
    ),
    "hide-an-index-behind-a-macro-named-like-the-catalogue": (
        "group-controller",
        [
            "create index leak on group_close.payments ((amount = 4242))",
            hidden_by("duckdb_indexes", "index_name = 'leak'"),
        ],
        ("holds the macro main.duckdb_indexes", "holds the index group_close.leak"),
    ),
    "hide-a-schema-behind-a-macro-named-like-the-catalogue": (
        "group-controller",
        ["create schema leak", hidden_by("duckdb_schemas", "schema_name = 'leak'")],
        ("holds the macro main.duckdb_schemas", "holds the schema leak"),
    ),
    "forge-the-catalog-name-with-a-macro-named-like-the-catalogue": (
        "group-controller",
        ["create macro main.current_database() as 'forged'"],
        ("holds the macro main.current_database",),
    ),
}


def broken(
    name: str, plans: Callable[[str], Plan], kinds: dict[str, dict[str, str]]
) -> tuple[str, Plan, tuple[str, ...]]:
    """The persona whose plan a change breaks, the broken plan, and the breaches it must show."""
    if name in ALTERED:
        persona, _, shown = ALTERED[name]
        return persona, plans(persona), shown
    persona, plan, breach = broken_plan(name, plans, kinds)
    return persona, plan, breach if isinstance(breach, tuple) else (breach,)


def broken_plan(
    name: str, plans: Callable[[str], Plan], kinds: dict[str, dict[str, str]]
) -> tuple[str, Plan, str | tuple[str, ...]]:
    """The persona whose plan a change to the plan breaks, the broken plan, and its breaches."""
    if name == "leave-the-base-table-in":
        plan = plans("accountant-chicago")
        whole = dataclasses.replace(payments_of(plan), purpose="main")
        return plan.persona, Plan(plan.persona, (*plan.tables, whole)), "main.payments, which no"
    if name == "leave-a-narrowed-base-table-in":
        plan = plans("group-controller")
        narrow = dataclasses.replace(only(payments_of(plan), "amount"), purpose="main")
        return plan.persona, Plan(plan.persona, (*plan.tables, narrow)), "main.payments, which no"
    if name == "give-another-purposes-table":
        plan = plans("accountant-chicago")
        other = dataclasses.replace(
            payments_of(plans("group-controller")), entities=frozenset({"chicago"})
        )
        return (
            plan.persona,
            Plan(plan.persona, (*plan.tables, other)),
            ("group_close.payments, which no", "holds the schema group_close"),
        )
    if name == "give-one-column-of-another-purposes-table":
        plan = plans("group-controller")
        # Audit's payments, holding only the document number: none of group close's columns.
        other = dataclasses.replace(payments_of(plan), purpose="audit")
        other = dataclasses.replace(other, columns=(Column("document_number", "VARCHAR", "clear"),))
        shown = ("audit.payments, which no", "holds the schema audit")
        return plan.persona, Plan(plan.persona, (*plan.tables, other)), shown
    if name == "give-a-table-its-purpose-does-not-list":
        plan = plans("metric-owner")
        (totals,) = [t for t in plan.tables if isinstance(t, Rows)]
        extra = Rows(
            "group_totals_left_out", None, (Column("figures", "BIGINT", "clear"),), totals.purpose
        )
        return (
            plan.persona,
            Plan(plan.persona, (*plan.tables, extra)),
            "group_totals_left_out, which no",
        )
    if name == "store-under-a-schema-that-is-no-purpose":
        plan = plans("group-controller")
        spare = dataclasses.replace(only(payments_of(plan), "amount"), purpose="spare")
        shown = ("spare.payments, which no", "holds the schema spare")
        return plan.persona, Plan(plan.persona, (*plan.tables, spare)), shown
    if name == "add-an-omitted-column":
        plan = plans("group-controller")
        rows = payments_of(plan)
        wider = dataclasses.replace(
            rows, columns=(*rows.columns, Column("contract_number", "VARCHAR", "clear"))
        )
        tables = tuple(wider if t is rows else t for t in plan.tables)
        return plan.persona, Plan(plan.persona, tables), "reads contract_number in"
    if name == "store-an-aggregate-table-with-other-columns":
        # The accountant's group totals in aggregate, stored as the product's rows by that name.
        totals = kinds["group_totals"]
        columns = tuple(Column(c, k, "clear") for c, k in totals.items())
        plan = swap_aggregate(plans("accountant-chicago"), "group_totals", columns)
        return plan.persona, plan, "main.group_totals as no aggregate: other columns"
    if name == "store-an-aggregate-table-ungrouped":
        # The aggregate's own columns, but the source's rows, one payment span each.
        totals = kinds["group_totals"]
        shown = (
            "period_start",
            "period_end",
            "reporting_currency",
            "constant_rates_on",
            "figures",
            "amount_at_constant_rates",
            "payees",
        )
        columns = tuple(Column(c, totals[c], "clear") for c in shown)
        plan = swap_aggregate(plans("accountant-chicago"), "group_totals", columns)
        return plan.persona, plan, "a group under the minimum"
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
        "store-an-aggregate-table-with-other-columns",
        "store-an-aggregate-table-ungrouped",
        *ALTERED,
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

    kinds = {table: dict(cols) for table, cols in schema.items()}
    persona, plan, shown = broken(change, plans, kinds)
    path = materialise(plan, warehouse, tmp_path)
    if change in ALTERED:
        with duckdb.connect(str(path)) as build:
            build.execute(f"attach {literal(str(warehouse))} as {SOURCE} (read_only)")
            altered = ALTERED[change][1]
            for statement in altered if isinstance(altered, list) else altered(build):
                build.execute(statement)
            build.execute(f"detach {SOURCE}")
    want = expected(access, aggregates, purposes, persona, warehouse)
    db = connect(path)
    try:
        found = breaches(db, persona, want)
    finally:
        db.close()
    assert len(found) == len(shown), found
    assert all(want in got for want, got in zip(shown, found, strict=True)), found


# The columns the purposes leave out of each persona's purpose tables, worked out from the source's
# columns: before purposes, a persona with a row grant on a product read every column of every
# table of it.
ABSENT_COLUMNS = {
    "group-controller": {
        "payments": [
            "payment_key",
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


def test_the_group_controller_s_organisation_name_is_empty_exactly_on_payments_to_a_person(
    as_persona: AsPersona, warehouse: Path
) -> None:
    """Group close holds neither `vendor_is_hashed` nor the row key, and holds the organisation
    paid: over the sample, the payments with no organisation name are exactly those to a person,
    compared as a multiset of entity, date, amount and that flag."""
    controller = as_persona("group-controller")
    for absent in ("vendor_is_hashed", "payment_key"):
        with pytest.raises(duckdb.BinderException):
            controller.execute(f"select {absent} from group_close.payments")  # noqa: S608
    shape = "select entity_id, payment_date, amount, {} from {}"
    kept = Counter(
        controller.execute(
            shape.format("vendor_organisation_name is null", "group_close.payments")
        ).fetchall()
    )
    with duckdb.connect(str(warehouse), read_only=True) as source:
        truth = Counter(source.execute(shape.format("vendor_is_hashed", "payments")).fetchall())
    flags = {row[-1] for row in truth}
    assert flags == {True, False}, "the sample pays persons and others"
    assert kept == truth


def test_payment_review_and_audit_are_the_purposes_that_list_every_column_of_their_tables(
    access: Doc, purposes: Doc, warehouse: Path
) -> None:
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
        "group_close.payments": (9, {**CHICAGO, **UK}),
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
    schema = source_schema(landed_warehouse)
    read: dict[str, dict[str, tuple[int, dict[str, int]]]] = {}
    found: list[str] = []
    for persona in (p["id"] for p in access["personas"]):
        directory = tmp_path / persona
        directory.mkdir()
        plan = compile_persona(access, columns, aggregates, purposes, persona, schema)
        db = connect(materialise(plan, landed_warehouse, directory))
        try:
            want = expected(access, aggregates, purposes, persona, landed_warehouse)
            found += breaches(db, persona, want)
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
