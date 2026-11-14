"""The visibility matrix acted out: every persona's own connection, every cell observed.

`access/visibility.yaml` says what each persona must see. This file opens each persona's
connection on an engine and makes, for every cell, the observation that proves it, against the
source read with full rights; the matrix test beside it checks the matrix against the grants, this
one against what the engine returns. The observations, per kind of cell:

- product `clear`, and `edit`, which reads as clear: each of the product's tables is there, and its
  rows of the entity (every row, for a group product) are as many as the source's and equal them
  row for row on every untagged column;
- product `denied`, and `draft` and `post`, which read nothing in the warehouse: no table of the
  product holds a row of the entity, a group product has no table, and no aggregate of it either;
- product `aggregate`: no row table of the product; each of its aggregate tables in
  `access/aggregates.yaml` there with exactly its grain, sums and units as columns; its groups (of
  the entity, for an entity-scoped product) equal to the groups worked out here from the source,
  in Python, apart from the compiler; none below the minimum group;
- tag `clear`: each column the tag marks holds the stored value on every row the persona reads;
- tag `masked`: it holds the tag's masking rule applied to the stored value, worked out here, and no
  column of any table the persona holds has a stored value of the tag that no untagged column has;
- tag `denied`: no table the persona holds has a column the tag marks, nor such a value anywhere;
- beyond the cells, the persona holds no table that no product lists.

A cell is proven on a build when the source holds something there for it to show or hide, its
support. The products the models build no table of yet (the ledger, the metric definitions, the
audit log), the bank tags, which mark no column until the ERP's tables exist, an entity with no
rows and an aggregate that keeps no group are acted out on invented tables, labelled as such, in a
copy of the build, and are reported as proven on invented tables only.

The engine is a parameter: an engine opens a persona's connection and the source with full rights,
and every observation is SQL over the persona's own tables and the source's, so the BigQuery twin
swaps the engine and keeps the cells. Failure messages name persona, place, the expected cell and
what was observed, in counts and names of tables and columns, never a data value.
"""

import shutil
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any, Protocol

import duckdb
import pytest
from gcp_project import lab_project_or_none

from access.compile import (
    ENTITY,
    SOURCE,
    Rows,
    compile_persona,
    connect,
    literal,
    masked,
    materialise,
    quote,
    source_schema,
)
from tests.access.test_compile import LANDED_SOURCES, sha256_rendered
from tests.conftest import landed_file, load_access

Doc = dict[str, Any]
Row = tuple[Any, ...]
Columns = Mapping[str, Mapping[str, str]]
# The cells that read rows; every other product cell reads none.
READS = frozenset({"clear", "edit"})
PERSONAS = [p["id"] for p in load_access()["personas"]]


def select(what: str, table: str, where: str = "") -> str:
    """A select from a table named as `Reader.tables` names it: `<schema>.<table>` outside the
    main schema. No catalogue name holds a dot."""
    named = ".".join(quote(part) for part in table.split(".", 1))
    return f"select {what} from {named}{where}"  # noqa: S608


def of_entities(entities: set[str] | None) -> str:
    """The filter to the entities' rows; none for every row. Entity ids are schema-checked."""
    if entities is None:
        return ""
    return (
        f" where {quote(ENTITY)} in ({', '.join(literal(e) for e in sorted(entities)) or 'null'})"
    )


@dataclass(frozen=True)
class Catalogues:
    access: Doc
    columns: Doc
    aggregates: Doc


class Reader(Protocol):
    """A connection: the source read with full rights, or one persona's own."""

    def rows(self, sql: str) -> list[Row]: ...

    def tables(self) -> dict[str, dict[str, str]]:
        """Each table and view the connection's database holds, in any schema, with its columns
        and their types in order; one outside the main schema is named `<schema>.<table>`."""
        ...

    def views(self) -> set[str]:
        """The names of `tables` that are views."""
        ...

    def row_hash(self, columns: Sequence[str]) -> str:
        """SQL for one number per row over the named columns, equal for equal values."""
        ...

    def is_string(self, kind: str) -> bool: ...


class Engine(Protocol):
    catalogues: Catalogues

    def source(self) -> Reader: ...

    def persona(self, persona: str) -> Reader: ...

    def invented(self) -> "Engine":
        """The same engine over a copy of its build holding the invented tables below."""
        ...

    def rebuilt(self, catalogues: Catalogues) -> "Engine":
        """The same engine and build with the personas' rules made from other catalogues."""
        ...


# The local engine.


class LocalReader:
    def __init__(self, db: duckdb.DuckDBPyConnection, remember: bool = False) -> None:
        self.db = db
        self.remembered: dict[str, list[Row]] | None = {} if remember else None

    def rows(self, sql: str) -> list[Row]:
        if self.remembered is None:
            return self.db.execute(sql).fetchall()
        if sql not in self.remembered:
            self.remembered[sql] = self.db.execute(sql).fetchall()
        return self.remembered[sql]

    def tables(self) -> dict[str, dict[str, str]]:
        found: dict[str, dict[str, str]] = {}
        for table, column, kind in self.db.execute(
            "select case when schema_name = 'main' then table_name"
            " else schema_name || '.' || table_name end, column_name, data_type"
            " from duckdb_columns() where database_name = system.main.current_database()"
            " order by schema_name, table_name, column_index"
        ).fetchall():
            found.setdefault(table, {})[column] = kind
        return found

    def views(self) -> set[str]:
        return {
            name
            for (name,) in self.db.execute(
                "select case when schema_name = 'main' then view_name"
                " else schema_name || '.' || view_name end from duckdb_views()"
                " where database_name = system.main.current_database() and not internal"
            ).fetchall()
        }

    def row_hash(self, columns: Sequence[str]) -> str:
        return f"hash({', '.join(quote(c) for c in columns)})"

    def is_string(self, kind: str) -> bool:
        return kind == "VARCHAR"


@dataclass
class LocalEngine:
    """DuckDB: a persona's connection is its compiled database (`access/compile.py`). `mutate`
    runs statements on a persona's database once built, with the source attached as the compiler
    attaches it, so a test can break what the compiler wrote."""

    warehouse: Path
    catalogues: Catalogues
    directory: Path
    mutate: Mapping[str, Sequence[str]] = field(default_factory=dict)
    opened: list[duckdb.DuckDBPyConnection] = field(default_factory=list)
    children: list["LocalEngine"] = field(default_factory=list)
    reader: LocalReader | None = None

    def source(self) -> LocalReader:
        if self.reader is None:
            self.reader = LocalReader(duckdb.connect(str(self.warehouse), read_only=True), True)
            self.opened.append(self.reader.db)
        return self.reader

    def persona(self, persona: str) -> LocalReader:
        c = self.catalogues
        plan = compile_persona(
            c.access, c.columns, c.aggregates, persona, source_schema(self.warehouse)
        )
        directory = self.directory / f"persona-{len(self.opened)}"
        directory.mkdir(parents=True)
        database = materialise(plan, self.warehouse, directory)
        if persona in self.mutate:
            with duckdb.connect(str(database)) as db:
                db.execute(f"attach {literal(str(self.warehouse))} as {SOURCE} (read_only)")
                for statement in self.mutate[persona]:
                    db.execute(statement)
                db.execute(f"detach {SOURCE}")
        self.opened.append(connect(database))
        return LocalReader(self.opened[-1])

    def invented(self) -> "LocalEngine":
        copy = self.directory / f"invented-{len(self.children)}" / "warehouse.duckdb"
        copy.parent.mkdir(parents=True)
        shutil.copyfile(self.warehouse, copy)
        with duckdb.connect(str(copy)) as db:
            for statement in invent(self.catalogues.access):
                db.execute(statement)
        child = LocalEngine(copy, invented_catalogues(self.catalogues), copy.parent, self.mutate)
        self.children.append(child)
        return child

    def rebuilt(self, catalogues: Catalogues) -> "LocalEngine":
        directory = self.directory / f"rebuilt-{len(self.children)}"
        child = LocalEngine(self.warehouse, catalogues, directory, self.mutate)
        self.children.append(child)
        return child

    def close(self) -> None:
        """Close every connection and delete every database built, derived data with hashed names
        in it."""
        for child in self.children:
            child.close()
            shutil.rmtree(child.directory, ignore_errors=True)
        for db in self.opened:
            db.close()
        self.opened.clear()
        for built in self.directory.glob("persona-*"):
            shutil.rmtree(built)


# The invented tables, every value labelled `invented`, never data: a ledger with rows of every
# entity, the metric definitions and the audit log as tables of their products; the ERP's bank
# details as columns of payments, the product whose readers the matrix states the bank tags for;
# twelve payments of an invented year to twelve payees of each entity, half of them people, so
# every entity has rows and a group of payments kept; and a span and year every entity brings
# figures of more payees than the minimum to, kept for a reader of one entity's rows.
INVENTED_TABLES = {
    "ledger": "ledger",
    "metric-definitions": "metric_definitions",
    "audit-log": "audit_log",
}
INVENTED_BANK = {
    "payments.vendor_bank_account_number": "bank-account-number",
    "payments.vendor_bank_account_holder": "bank-account-holder",
}
INVENTED_YEAR = 1999


def invent(access: Doc) -> list[str]:
    """Statements that add the invented tables and rows to a copy of a build."""
    sql = [
        "create table ledger (entity_id varchar, entry_key varchar, account varchar, amount int)",
        "create table metric_definitions as select 'invented metric' as metric",
        "create table audit_log as select 'invented answer' as answer",
        "alter table payments add column vendor_bank_account_number varchar",
        "alter table payments add column vendor_bank_account_holder varchar",
        "update payments set vendor_bank_account_number = 'INVENTED-' || payment_key,"
        " vendor_bank_account_holder = 'invented holder ' || payment_key",
    ]
    for entity, currency in (
        (literal(e["id"]), literal(e["currency"])) for e in access["entities"]
    ):
        # Invented values only; the entity ids and currencies are the catalogue's, quoted.
        sql += [
            f"insert into ledger select {entity}, 'invented-' || i, 'invented', i"  # noqa: S608
            " from range(2) r(i)",
            "insert into payments by name select"  # noqa: S608
            f" 'invented-' || {entity} || '-' || i as payment_key, {entity} as entity_id,"
            f" {currency} as currency, {INVENTED_YEAR} as fiscal_year, 1 as fiscal_period,"
            " cast(i as decimal(28, 6)) as amount, 'invented payee ' || i as vendor_name,"
            " i % 2 = 0 as vendor_is_hashed, 'invented-doc-' || i as document_number,"
            " false as is_repeat,"
            " case when i % 2 = 1 then 'invented payee ' || i end as vendor_organisation_name,"
            " case when i % 2 = 0 then 'invented payee ' || i end as vendor_person_key,"
            " 'INVENTED-' || i as vendor_bank_account_number,"
            " 'invented holder ' || i as vendor_bank_account_holder"
            " from range(12) as r(i)",
            "insert into group_totals by name select"  # noqa: S608
            f" 'invented-' || {entity} as group_total_key, {entity} as entity_id,"
            f" {INVENTED_YEAR} as fiscal_year, 1 as fiscal_period,"
            f" date '{INVENTED_YEAR}-01-01' as period_start,"
            f" date '{INVENTED_YEAR}-01-31' as period_end, 'invented' as basis, 20 as figures,"
            f" 20 as payees, {currency} as currency, 1 as amount,"
            " (select any_value(reporting_currency) from group_totals) as reporting_currency,"
            " 1 as amount_at_actual_rates, 1 as amount_at_constant_rates,"
            " (select any_value(constant_rates_on) from group_totals) as constant_rates_on",
            "insert into group_totals_left_out by name select"
            f" {entity} as entity_id, {INVENTED_YEAR} as fiscal_year, 'invented' as basis,"
            f" 'invented' as left_out_by, 20 as figures, 20 as payees, {currency} as currency,"
            " 1 as amount",
        ]
    return sql


def invented_catalogues(catalogues: Catalogues) -> Catalogues:
    """The catalogues naming the invented tables as their products' and tagging the invented bank
    columns; the ledger then awaits no tag."""
    access = {**catalogues.access, "data_products": []}
    for product in catalogues.access["data_products"]:
        tables = product.get("tables") or [INVENTED_TABLES[product["id"]]]
        access["data_products"].append({**product, "tables": tables})
    products = {
        product: {"columns": dict(entry.get("columns", {}))}
        for product, entry in catalogues.columns["products"].items()
    }
    products["payments"]["columns"].update(INVENTED_BANK)
    return Catalogues(access, {**catalogues.columns, "products": products}, catalogues.aggregates)


# What a persona is observed to see.


@dataclass
class Seen:
    persona: str
    place: str  # "<product> <entity>", "<product>", "tag <tag>", or "tables"
    cell: str
    support: int = 0
    failures: list[str] = field(default_factory=list)

    def fail(self, observed: str) -> None:
        self.failures.append(f"{self.persona} {self.place}: expected {self.cell}, {observed}")


def cells_of(row: Doc) -> Iterator[tuple[str, str | None, str]]:
    for product, cells in row["products"].items():
        if isinstance(cells, str):
            yield product, None, cells
        else:
            for entity, cell in cells.items():
                yield product, entity, cell


def read_entities(row: Doc, product: str) -> set[str] | None:
    """The entities whose rows of the product the persona reads; None for all of a group product
    it reads; an empty set when it reads none."""
    cells = row["products"][product]
    if isinstance(cells, str):
        return None if cells in READS else set()
    return {e for e, cell in cells.items() if cell in READS}


def known_entities(row: Doc, aggregates: Doc) -> set[str]:
    """The entities whose share of a group the persona can work out: those whose rows it reads in
    some entity-scoped product, and those it reads per entity in an aggregate table."""
    apart = {p for p, e in aggregates["products"].items() if e["entity_rows"]}
    return {
        e
        for p, e, cell in cells_of(row)
        if e is not None and (cell in READS or cell == "aggregate" and p in apart)
    }


def tagged_columns(columns: Doc) -> dict[str, dict[str, str]]:
    """Per table, each tagged column with its tag."""
    found: dict[str, dict[str, str]] = {}
    for entry in columns["products"].values():
        for column, tag in entry.get("columns", {}).items():
            table, _, name = column.partition(".")
            found.setdefault(table, {})[name] = tag
    return found


def mask(rule: str, value: str | None) -> str | None:
    """A masking rule of access/columns.yaml on a stored value, as BigQuery's renders it."""
    if value is None or rule == "nullify":
        return None
    if rule == "last-four" and len(value) > 4:
        return "XXXXX" + value[-4:]
    return sha256_rendered(value)


def spec_columns(spec: Doc, entry: Doc) -> tuple[list[str], list[str], str, str, str]:
    """An aggregate table's grain, sums, units column, units count and the column it counts."""
    rows = entry["entity_rows"]
    grain = [*spec["group_by"], *(spec["entity_group_by"] if rows else [])]
    sums = [*spec["sum"], *(spec.get("entity_sum", []) if rows else [])]
    kind = "count_distinct" if "count_distinct" in spec["units"] else "max"
    return grain, sums, spec["units"]["name"], kind, spec["units"][kind]


@dataclass
class Group:
    """One group of an aggregate table's grain, with each entity's share of it."""

    kind: str  # how the units count: `count_distinct` or `max`
    sums: dict[str, list[Any]] = field(default_factory=dict)  # per entity, its sums
    units: dict[str, Any] = field(default_factory=dict)  # per entity, its payees or largest count

    def total(self, entities: Sequence[str] | None = None) -> list[Any]:
        shares = [s for e, s in self.sums.items() if entities is None or e in entities]
        return [_sum(column) for column in zip(*shares, strict=True)]

    def count(self, entities: Sequence[str] | None = None) -> int | None:
        each = [n for e, n in self.units.items() if entities is None or e in entities]
        if self.kind == "count_distinct":
            return len(set().union(*each))
        present = [n for n in each if n is not None]
        return max(present) if present else None


def _sum(values: Sequence[Any]) -> Any:
    present = [v for v in values if v is not None]
    return sum(present[1:], present[0]) if present else None


def groups_of(source: Reader, spec: Doc, entry: Doc, scope: set[str] | None) -> dict[Row, Group]:
    """The aggregate table's groups over the source's rows of the entities in scope (all, for
    None), each entity's sums and units summed in the source and combined here in Python."""
    grain, sums, _, kind, of = spec_columns(spec, entry)
    keys = list(dict.fromkeys([*grain, ENTITY]))
    listed = ", ".join(quote(k) for k in keys)
    rows = of_entities(scope)
    totals = ", ".join([*(f"sum({quote(s)})" for s in sums), f"max({quote(of)})"])
    payees: dict[Row, set[Any]] = {}
    if kind == "count_distinct":
        for *key, unit in source.rows(
            select(f"distinct {listed}, {quote(of)}", spec["from"], rows)
        ):
            if unit is not None:
                payees.setdefault(tuple(key), set()).add(unit)
    groups: dict[Row, Group] = {}
    for values in source.rows(
        select(f"{listed}, {totals}", spec["from"], f"{rows} group by {listed}")
    ):
        key, summed, largest = values[: len(keys)], values[len(keys) : -1], values[-1]
        group = groups.setdefault(tuple(key[keys.index(c)] for c in grain), Group(kind))
        entity = key[keys.index(ENTITY)]
        group.sums[entity] = list(summed)
        group.units[entity] = payees.get(tuple(key), set()) if kind == "count_distinct" else largest
    return groups


def kept(group: Group, entry: Doc, aggregates: Doc, known: set[str]) -> bool:
    """Whether the group is shown: at least the minimum group and, for figures across all
    entities, the entities the persona reads no rows of numbering the minimum entities, their rows
    alone holding the minimum group."""
    minimum = aggregates["minimum_group"]
    everyone = group.count()
    if everyone is None or everyone < minimum:
        return False
    if entry["entity_rows"]:
        return True
    others = [e for e in group.units if e not in known]
    rest = group.count(others)
    return len(others) >= aggregates["minimum_entities"] and rest is not None and rest >= minimum


def expected_groups(
    source: Reader,
    spec: Doc,
    entry: Doc,
    aggregates: Doc,
    scope: set[str] | None,
    known: set[str],
) -> list[Row]:
    """The rows an aggregate table must hold: its kept groups, each with its sums and units."""
    return [
        (*key, *group.total(), group.count())
        for key, group in groups_of(source, spec, entry, scope).items()
        if kept(group, entry, aggregates, known)
    ]


@dataclass
class Sight:
    """One persona's connection and the source, with their tables."""

    persona: str
    row: Doc
    me: Reader
    source: Reader
    held: dict[str, dict[str, str]]
    built: dict[str, dict[str, str]]
    catalogues: Catalogues

    def count(self, reader: Reader, table: str, entities: set[str] | None) -> int:
        ((n,),) = reader.rows(select("count(*)", table, of_entities(entities)))
        return n


def observe(engine: Engine, matrix: Doc, persona: str) -> list[Seen]:
    """Every cell of the persona's row, observed through its own connection."""
    me, source = engine.persona(persona), engine.source()
    cat = engine.catalogues
    sight = Sight(
        persona, matrix["personas"][persona], me, source, me.tables(), source.tables(), cat
    )
    tables_of = {p["id"]: list(p.get("tables", [])) for p in cat.access["data_products"]}
    seen: list[Seen] = []
    for product, entity, cell in cells_of(sight.row):
        place = Seen(persona, f"{product} {entity}" if entity else product, cell)
        seen.append(place)
        if tables_of[product]:  # else nothing is built to act on: the invented copy acts it out
            observe_product(sight, place, product, entity, tables_of[product])
    seen += observe_tags(sight)
    listed = {
        *(t for tables in tables_of.values() for t in tables),
        *(s["name"] for e in cat.aggregates["products"].values() for s in e["tables"]),
    }
    inventory = Seen(persona, "tables", "only tables its products list", len(sight.held))
    if unlisted := sorted(set(sight.held) - listed):
        inventory.fail(f"observed {len(unlisted)} more: {', '.join(unlisted)}")
    if views := sorted(me.views()):
        inventory.fail(f"observed {len(views)} views: {', '.join(views)}")
    return [*seen, inventory]


def observe_product(
    sight: Sight, place: Seen, product: str, entity: str | None, tables: list[str]
) -> None:
    cat, held, me, source = sight.catalogues, sight.held, sight.me, sight.source
    entry = cat.aggregates["products"].get(product)
    specs = {s["name"]: s for s in entry["tables"]} if entry else {}
    shape: dict[str, list[str]] = {}
    for name, spec in specs.items():
        assert entry is not None
        grain, sums, units, *_ = spec_columns(spec, entry)
        shape[name] = [*grain, *sums, units]

    def is_rows(table: str) -> bool:
        # A group-totals aggregate table has its source table's name; the columns tell them apart.
        return table in held and list(held[table]) != shape.get(table)

    one = {entity} if entity else None
    tagged = tagged_columns(cat.columns)
    reads = any(c in READS for p, _, c in cells_of(sight.row) if p == product)
    for table in tables:
        total = sight.count(source, table, one)
        # What a cell shows or hides; an aggregate cell is proven by the groups it is given.
        place.support += 0 if place.cell == "aggregate" else total
        if place.cell in READS:
            if not is_rows(table):
                place.fail(f"observed no table {table}")
                continue
            if (n := sight.count(me, table, one)) != total:
                place.fail(f"observed {n} rows in {table} where the source holds {total}")
            plain = [c for c in sight.built[table] if c not in tagged.get(table, {})]
            if missing := [c for c in plain if c not in held[table]]:
                place.fail(f"observed {table} without {len(missing)} of its untagged columns")
                continue
            digest = select(me.row_hash(plain), table, of_entities(one))
            if differ := Counter(source.rows(digest)) - Counter(me.rows(digest)):
                place.fail(f"observed {sum(differ.values())} rows of {table} unlike the source's")
        elif is_rows(table):
            # A product the persona reads no rows of has no row table at all, not an empty one.
            if not reads or entity is None or ENTITY not in held[table]:
                place.fail(f"observed a row table {table}")
            elif n := sight.count(me, table, one):
                place.fail(f"observed {n} rows of the entity in {table}")
    for name, spec in specs.items():
        assert entry is not None
        grain = shape[name][: len(spec_columns(spec, entry)[0])]
        present = name in held and list(held[name]) == shape[name]
        apart = entity is not None and ENTITY in grain
        if place.cell != "aggregate":
            if present and not apart:
                place.fail(f"observed an aggregate table {name}")
            elif present and (n := sight.count(me, name, one)):
                place.fail(f"observed {n} groups of the entity in {name}")
            continue
        if not present:
            place.fail(f"observed no aggregate table {name} of its grain")
            continue
        scope = {e for p, e, c in cells_of(sight.row) if p == product and c == "aggregate" and e}
        assert entry is not None
        want = expected_groups(
            source,
            spec,
            entry,
            cat.aggregates,
            scope if entity else None,
            known_entities(sight.row, cat.aggregates),
        )
        got = me.rows(select(", ".join(quote(c) for c in shape[name]), name))
        if apart:
            at = grain.index(ENTITY)
            want, got = ([g for g in groups if g[at] == entity] for groups in (want, got))
        place.support += len(want)
        if Counter(want) != Counter(got):
            extra = sum((Counter(got) - Counter(want)).values())
            place.fail(
                f"observed {len(got)} groups in {name} where {len(want)} are kept, {extra} not"
            )
        if small := [g for g in got if g[-1] is None or g[-1] < cat.aggregates["minimum_group"]]:
            place.fail(f"observed {len(small)} groups in {name} below the minimum group")


def row_key(sight: Sight, table: str) -> list[str]:
    """The columns that tell the table's rows apart for a tagged value: the first untagged column
    unique in the source, else every untagged column, so a tag's cell reads its own column."""
    tagged = tagged_columns(sight.catalogues.columns).get(table, {})
    plain = [c for c in sight.built[table] if c not in tagged]
    for column in plain:
        ((rows, distinct),) = sight.source.rows(
            select(f"count(*), count(distinct {quote(column)})", table)
        )
        if rows == distinct:
            return [column]
    return plain


def strings_held(sight: Sight) -> dict[str, set[Any]]:
    """Every distinct string value the persona can read, per `<table>.<column>`."""
    return {
        f"{table}.{column}": {
            v for (v,) in sight.me.rows(select(f"distinct {quote(column)}", table))
        }
        for table, columns in sight.held.items()
        for column, kind in columns.items()
        if sight.me.is_string(kind)
    }


def secrets(sight: Sight, marks: list[tuple[str, str]]) -> set[Any]:
    """The tag's stored values that no untagged column of their table holds: those a masked or
    denied reader must never read in any column."""
    tagged, source = tagged_columns(sight.catalogues.columns), sight.source

    def values(table: str, column: str) -> set[Any]:
        return {v for (v,) in source.rows(select(f"distinct {quote(column)}", table))}

    found: set[Any] = set()
    for table, column in marks:
        stored = values(table, column) - {None}
        for other, kind in sight.built[table].items():
            if other not in tagged.get(table, {}) and source.is_string(kind):
                stored -= values(table, other)
        found |= stored
    return found


def observe_tags(sight: Sight) -> list[Seen]:
    cat = sight.catalogues
    tagged = tagged_columns(cat.columns)
    rules = {t["id"]: t["masking"] for t in cat.columns["tags"]}
    product_of = {
        column: product
        for product, entry in cat.columns["products"].items()
        for column in entry.get("columns", {})
    }
    seen: list[Seen] = []
    held_values: dict[str, set[Any]] | None = None
    for tag, cell in sight.row["columns"].items():
        place = Seen(sight.persona, f"tag {tag}", cell)
        seen.append(place)
        marks = [
            (table, column)
            for table, columns in tagged.items()
            for column, mark in columns.items()
            if mark == tag and table in sight.built
        ]
        if not marks:
            continue  # it marks no column of the build: the invented copy acts it out
        if cell in ("clear", "masked"):
            for table, column in marks:
                entities = read_entities(sight.row, product_of[f"{table}.{column}"])
                if entities == set():
                    continue  # the persona reads no rows of this product
                if table not in sight.held or column not in sight.held[table]:
                    place.fail(f"observed no column {table}.{column}")
                    continue
                key = sight.me.row_hash(row_key(sight, table))
                pairs = select(f"{key}, {quote(column)}", table, of_entities(entities))
                stored = sight.source.rows(pairs)
                place.support += sum(1 for _, v in stored if v is not None)
                rule = "stored value" if cell == "clear" else f"{rules[tag]} masking"
                want = Counter(
                    (h, v if cell == "clear" else mask(rules[tag], v)) for h, v in stored
                )
                if differ := want - Counter(sight.me.rows(pairs)):
                    n = sum(differ.values())
                    place.fail(f"observed {n} values of {table}.{column} not its {rule}")
        if cell in ("masked", "denied"):
            hidden = secrets(sight, marks)
            if cell == "denied":
                place.support += len(hidden)
                for _, column in marks:
                    if named := sorted(t for t, cols in sight.held.items() if column in cols):
                        place.fail(f"observed a column {column} in {', '.join(named)}")
            if held_values is None:
                held_values = strings_held(sight)
            for at, values in held_values.items():
                if n := len(values & hidden):
                    place.fail(f"observed {n} of the tag's stored values in {at}")
    return seen


# Running the matrix.


def act_out(engine: Engine, matrix: Doc, personas: Sequence[str]) -> dict[str, list[Seen]]:
    """Every cell of each persona, on the build and on the invented copy of it."""
    invented = engine.invented()
    return {
        "build": [s for p in personas for s in observe(engine, matrix, p)],
        "invented": [s for p in personas for s in observe(invented, matrix, p)],
    }


def verdict(runs: Mapping[str, list[Seen]]) -> tuple[list[str], dict[str, Counter[str]]]:
    """The failures, and per persona the cells proven by kind: `<kind>` on the build, `<kind>
    invented` on invented tables only. A cell proven on neither is a failure."""
    failures = [f for seen in runs.values() for s in seen for f in s.failures]
    where: dict[tuple[str, str], str] = {}
    cells: dict[tuple[str, str], Seen] = {}
    for build in ("build", "invented"):
        for s in runs[build]:
            if s.place != "tables":
                cells.setdefault((s.persona, s.place), s)
                if s.support:
                    where.setdefault((s.persona, s.place), build)
    tally: dict[str, Counter[str]] = {}
    for key, s in cells.items():
        if key not in where:
            failures.append(f"{s.persona} {s.place}: expected {s.cell}, acted out on no row")
            continue
        kind = ("tag " if s.place.startswith("tag ") else "") + s.cell
        suffix = " invented" if where[key] == "invented" else ""
        tally.setdefault(s.persona, Counter())[kind + suffix] += 1
    return failures, tally


@pytest.fixture(params=["local", "bigquery"])
def engine(
    request: pytest.FixtureRequest,
    access: Doc,
    columns: Doc,
    aggregates: Doc,
    tmp_path: Path,
) -> Iterator[Engine]:
    """The engine a persona's connection is opened on: the local engine over the dbt build of
    the samples, or BigQuery in the lab's own project, skipped until it is written, before any
    build, so the controls test can see it skip quickly."""
    if request.param == "bigquery":
        if lab_project_or_none() is None:
            pytest.skip("LEDGER_LAB_GCP_PROJECT is unset: the lab has no BigQuery to act in")
        # Named, not run: opening a persona on BigQuery and reading the source there are written
        # with the warehouse's own rules, which this engine acts on; nothing here calls GCP.
        pytest.skip("the BigQuery engine is not written yet: it comes with the warehouse's rules")
    warehouse: Path = request.getfixturevalue("warehouse")
    local = LocalEngine(warehouse, Catalogues(access, columns, aggregates), tmp_path)
    yield local
    local.close()


def kinds(tally: Mapping[str, Counter[str]]) -> dict[str, int]:
    return dict(sorted(sum(tally.values(), Counter()).items()))


# The cells proven over the samples, by kind, across the personas: on the build, and on invented
# tables only. 120 cells; a change to the samples, the catalogues or an observation moves them.
SAMPLE_PROVEN = {
    "aggregate": 4,
    "aggregate invented": 3,
    "clear": 11,
    "clear invented": 14,
    "denied": 13,
    "denied invented": 42,
    "draft invented": 1,
    "edit invented": 1,
    "post invented": 1,
    "tag clear": 2,
    "tag clear invented": 4,
    "tag denied": 3,
    "tag denied invented": 6,
    "tag masked": 4,
    "tag masked invented": 11,
}


def test_every_persona_sees_its_matrix(engine: Engine, visibility: Doc) -> None:
    failures, tally = verdict(act_out(engine, visibility, PERSONAS))
    assert failures == []
    assert set(tally) == set(PERSONAS)
    assert kinds(tally) == SAMPLE_PROVEN


# The harness fails for every kind of cell: each change below breaks one persona's compiled
# database the way a faulty compiler would, on the samples' build, and the matrix goes red on
# exactly the cells it touches.
MUTATIONS = [
    "leak-another-entitys-rows",
    "change-a-value-in-a-clear-row",
    "store-a-clear-value-in-a-masked-column",
    "give-an-aggregate-reader-a-row-table",
    "replace-an-aggregate-with-its-rows",
    "drop-the-minimum",
    "drop-the-entities-apart",
    "add-a-tagged-column-to-a-denied-reader",
    "add-a-tagged-value-under-another-name",
    "hold-an-unlisted-table",
    "leak-rows-in-another-schema",
    "leak-clear-values-in-another-schema",
    "a-view-over-a-hidden-table",
    "an-empty-table-of-a-denied-product",
]
WAREHOUSE = f"{SOURCE}.main"


def regroup(spec: Doc, entry: Doc, having: str) -> str:
    """The aggregate table rebuilt from the source at its grain, with the given condition."""
    grain, sums, units, kind, of = spec_columns(spec, entry)
    keys = ", ".join(quote(c) for c in grain)
    count = f"count(distinct {quote(of)})" if kind == "count_distinct" else f"max({quote(of)})"
    summed = "".join(f", sum({quote(s)}) as {quote(s)}" for s in sums)
    return (
        f"create or replace table {quote(spec['name'])} as select {keys}{summed},"  # noqa: S608
        f" {count} as {quote(units)} from {WAREHOUSE}.{quote(spec['from'])}"
        f" group by {keys} {having}"
    )


def mutation(
    name: str, cat: Catalogues, warehouse: Path, matrix: Doc
) -> tuple[str, list[str], set[str]]:
    """The persona a change breaks, the statements that break its database and the cells they
    touch."""
    tagged = tagged_columns(cat.columns)["payments"]
    plain = ", ".join(quote(c) for c, _ in source_schema(warehouse)["payments"] if c not in tagged)
    minimum = cat.aggregates["minimum_group"]
    (by_period,) = cat.aggregates["products"]["payments"]["tables"]
    payments, totals = (
        cat.aggregates["products"]["payments"],
        cat.aggregates["products"]["group-totals"],
    )
    stored = f"(select max(vendor_person_key) from {WAREHOUSE}.payments)"  # noqa: S608
    if name == "leak-another-entitys-rows":
        persona = "accountant-chicago"
        plan = compile_persona(
            cat.access, cat.columns, cat.aggregates, persona, source_schema(warehouse)
        )
        (rows,) = [t for t in plan.tables if isinstance(t, Rows) and t.table == "payments"]
        listed = ", ".join(masked(c) for c in rows.columns)
        return (
            persona,
            [
                f"insert into payments select {listed} from {WAREHOUSE}.payments"  # noqa: S608
                " where entity_id = 'uk-transport'"
            ],
            {"payments uk-transport"},
        )
    if name == "change-a-value-in-a-clear-row":
        return (
            "accountant-chicago",
            [
                "update payments set amount = amount + 1"
                " where payment_key = (select min(payment_key) from payments)"
            ],
            {"payments chicago"},
        )
    if name == "store-a-clear-value-in-a-masked-column":
        return (
            "accountant-chicago",
            [
                f"update payments set vendor_person_key = w.vendor_person_key"  # noqa: S608
                f" from {WAREHOUSE}.payments as w where w.payment_key = payments.payment_key"
            ],
            {"tag individual-id"},
        )
    if name == "give-an-aggregate-reader-a-row-table":
        return (
            "metric-owner",
            [f"create table payments as select {plain} from {WAREHOUSE}.payments"],  # noqa: S608
            {f"payments {e}" for e in matrix["personas"]["metric-owner"]["products"]["payments"]},
        )
    if name == "replace-an-aggregate-with-its-rows":
        return (
            "accountant-nl-government",
            [f"create or replace table group_totals as select * from {WAREHOUSE}.group_totals"],  # noqa: S608
            {"group-totals"},
        )
    if name == "drop-the-minimum":
        grain = [*by_period["group_by"], *by_period["entity_group_by"]]
        keys = ", ".join(quote(c) for c in grain)
        small = (
            duckdb.connect(str(warehouse), read_only=True)
            .execute(
                f"select distinct entity_id from (select {keys} from payments group by {keys}"  # noqa: S608
                f" having count(distinct vendor_name) < {minimum})"
            )
            .fetchall()
        )
        return (
            "metric-owner",
            [regroup(by_period, payments, "")],
            {f"payments {e}" for (e,) in small},
        )
    if name == "drop-the-entities-apart":
        left_out = totals["tables"][1]
        return (
            "accountant-uk-transport",
            [regroup(left_out, totals, f"having max(payees) >= {minimum}")],
            {"group-totals"},
        )
    if name == "add-a-tagged-column-to-a-denied-reader":
        return (
            "metric-owner",
            [
                "alter table group_totals add column vendor_person_key varchar",
                f"update group_totals set vendor_person_key = {stored}",  # noqa: S608
            ],
            {"tag individual-id"},
        )
    if name == "add-a-tagged-value-under-another-name":
        return (
            "metric-owner",
            [
                "alter table group_totals add column note varchar",
                f"update group_totals set note = {stored}",  # noqa: S608
            ],
            {"tag individual-id"},
        )
    plan = compile_persona(
        cat.access, cat.columns, cat.aggregates, "accountant-chicago", source_schema(warehouse)
    )
    (rows,) = [t for t in plan.tables if isinstance(t, Rows) and t.table == "payments"]
    uk = f"from {WAREHOUSE}.payments where entity_id = 'uk-transport'"
    as_masked = f"select {', '.join(masked(c) for c in rows.columns)} {uk}"  # noqa: S608
    if name == "leak-rows-in-another-schema":
        return (
            "accountant-chicago",
            ["create schema other", f"create table other.uk as {as_masked}"],
            {"tables"},
        )
    if name == "leak-clear-values-in-another-schema":
        return (
            "accountant-chicago",
            ["create schema other", f"create table other.uk as select * {uk}"],  # noqa: S608
            {"tables", "tag individual-id"},
        )
    if name == "a-view-over-a-hidden-table":
        return (
            "accountant-chicago",
            [
                "create schema hidden",
                f"create table hidden.uk as {as_masked}",
                "create view uk_payments as select * from hidden.uk",
            ],
            {"tables"},
        )
    if name == "an-empty-table-of-a-denied-product":
        return (
            "proposer",
            [f"create table payments as select {plain} from {WAREHOUSE}.payments where false"],  # noqa: S608
            {f"payments {e}" for e in matrix["personas"]["proposer"]["products"]["payments"]},
        )
    assert name == "hold-an-unlisted-table"
    return (
        "accountant-chicago",
        [f"create table totals_copy as select * from {WAREHOUSE}.group_totals"],  # noqa: S608
        {"tables"},
    )


@pytest.mark.parametrize("name", MUTATIONS)
def test_the_matrix_goes_red_on_exactly_the_cells_a_broken_database_touches(
    name: str,
    access: Doc,
    columns: Doc,
    aggregates: Doc,
    visibility: Doc,
    warehouse: Path,
    tmp_path: Path,
) -> None:
    cat = Catalogues(access, columns, aggregates)
    persona, statements, places = mutation(name, cat, warehouse, visibility)
    assert places, "the samples hold something for the change to touch"
    whole = LocalEngine(warehouse, cat, tmp_path / "whole")
    broken = LocalEngine(warehouse, cat, tmp_path / "broken", {persona: statements})
    try:
        assert [f for s in observe(whole, visibility, persona) for f in s.failures] == []
        seen = observe(broken, visibility, persona)
    finally:
        whole.close()
        broken.close()
    assert {s.place for s in seen if s.failures} == places
    messages = [f for s in seen for f in s.failures]
    cells = {s.place: s.cell for s in seen}
    for message in messages:
        place = next(p for p in places if message.startswith(f"{persona} {p}: "))
        assert f": expected {cells[place]}, observed " in message
    with duckdb.connect(str(warehouse), read_only=True) as db:
        values = {
            v
            for (column,) in db.execute(
                "select column_name from duckdb_columns()"
                " where table_name = 'payments' and data_type = 'VARCHAR'"
            ).fetchall()
            for (v,) in db.execute(f"select distinct {quote(column)} from payments").fetchall()  # noqa: S608
            if v and len(v) >= 8
        }
    # The catalogue's entity ids are words of the matrix, not data.
    values -= {e["id"] for e in access["entities"]}
    assert not [v for v in values for m in messages if v in m], "a message holds a data value"


# The small cell and the differencing pair.


def sql_value(value: Any) -> str:
    """A grain value as a SQL literal: dates, numbers, flags and catalogue words, never a payee."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return literal(value)
    return f"date '{value.isoformat()}'"


def aggregate_cells(row: Doc, aggregates: Doc) -> Iterator[tuple[Doc, Doc, set[str] | None]]:
    """Each aggregate table the persona reads, with its product's entry and the entities in scope
    (None for a group product)."""
    for product, entry in aggregates["products"].items():
        cells = row["products"][product]
        if isinstance(cells, str):
            scope = None if cells == "aggregate" else set()
        else:
            scope = {e for e, c in cells.items() if c == "aggregate"}
        if scope != set():
            for spec in entry["tables"]:
                yield spec, entry, scope


def test_a_small_cell_query_returns_nothing(
    engine: Engine, visibility: Doc, aggregates: Doc
) -> None:
    """Each aggregate reader asks its aggregate tables for every group of the source of fewer
    payees than the minimum, by its grain, and is given none."""
    asked: Counter[str] = Counter()
    minimum = aggregates["minimum_group"]
    for persona in PERSONAS:
        row = visibility["personas"][persona]
        me = None
        for spec, entry, scope in aggregate_cells(row, aggregates):
            grain = spec_columns(spec, entry)[0]
            groups = groups_of(engine.source(), spec, entry, scope)
            for key, group in groups.items():
                if (group.count() or 0) >= minimum:
                    continue
                me = me or engine.persona(persona)
                match = " and ".join(
                    f"{quote(c)} is not distinct from {sql_value(v)}"
                    for c, v in zip(grain, key, strict=True)
                )
                assert me.rows(select("count(*)", spec["name"], f" where {match}")) == [(0,)]
                asked[persona] += 1
    assert set(asked) == {
        p
        for p in PERSONAS
        if any(True for _ in aggregate_cells(visibility["personas"][p], aggregates))
    }


def cents(value: Fraction) -> Decimal:
    """Rounded half away from zero to two decimal places, as the models translate a figure."""
    hundredths = abs(value) * 100
    whole = int(hundredths) + (1 if hundredths - int(hundredths) >= Fraction(1, 2) else 0)
    return Decimal(whole if value >= 0 else -whole) / 100


def own_shares(engine: Engine, persona: str, entities: set[str]) -> dict[Row, list[Any]]:
    """The figures and constant-rate amount the persona's own entities bring to each span of the
    group totals, worked out from the payments it reads in clear and two public tables: the
    entities' fiscal calendars and the ECB's rates on the day the constant rates are taken."""
    source = engine.source()
    ((reporting, day),) = source.rows(
        select("distinct reporting_currency, constant_rates_on", "group_totals")
    )
    rates = dict(
        source.rows(
            select("currency, units_per_euro", "fx_rates", f" where rate_on = date '{day}'")
        )
    )
    periods = source.rows(
        select("entity_id, fiscal_year, fiscal_period, period_start, period_end", "fiscal_periods")
    )
    span = {(e, y, p): (s, t) for e, y, p, s, t in periods}
    for e, y, _, s, t in periods:
        first, last = span.get((e, y, None), (s, t))
        span[(e, y, None)] = (min(first, s), max(last, t))
    shares: dict[Row, list[Any]] = {}
    for entity, year, period, currency, amount in engine.persona(persona).rows(
        "select entity_id, fiscal_year, fiscal_period, currency, amount from payments"
        " where not is_repeat"
    ):
        if entity not in entities or (entity, year, period) not in span or amount is None:
            continue
        key = (*span[(entity, year, period)], reporting, day)
        share = shares.setdefault(key, [0, Decimal(0)])
        share[0] += 1
        share[1] += cents(Fraction(amount) * Fraction(rates[reporting]) / Fraction(rates[currency]))
    return shares


def one_entitys_figures(engine: Engine, matrix: Doc, minimum: int) -> dict[str, Counter[str]]:
    """Per reader of figures across all entities, the figures that are one other entity's, alone
    (`alone`) or once the reader takes away its own entities' share (`less own`).

    With the entities apart at one, each such figure is given and, less the reader's own share,
    is exactly the other entity's figure; at the catalogue's two, none is given. The share is the
    one the persona works out from the payments it reads (`own_shares`) where that equals its
    entities' share; one it cannot work out from its rows (an entity whose figures are not
    payments) is taken from the source, as the public files give it, and counted apart."""
    cat = engine.catalogues
    at = {**cat.aggregates, "minimum_group": minimum}
    strict = engine.rebuilt(Catalogues(cat.access, cat.columns, at))
    loose = engine.rebuilt(Catalogues(cat.access, cat.columns, {**at, "minimum_entities": 1}))
    isolated: dict[str, Counter[str]] = {}
    for persona in PERSONAS:
        row = matrix["personas"][persona]
        known = known_entities(row, at)
        clear = read_entities(row, "payments") or set()
        for spec, entry, scope in aggregate_cells(row, at):
            if entry["entity_rows"] or scope is not None:
                continue
            grain, sums, *_ = spec_columns(spec, entry)
            pairs: dict[Row, tuple[Group, str, list[str]]] = {}
            for key, group in groups_of(engine.source(), spec, entry, None).items():
                others = [e for e in group.units if e not in known]
                if len(others) == 1 and (group.count(others) or 0) >= minimum:
                    pairs[key] = (group, others[0], [e for e in group.units if e in known])
            shown = ", ".join(quote(c) for c in [*grain, *sums])
            given = {
                tuple(r[: len(grain)]): list(r[len(grain) :])
                for r in loose.persona(persona).rows(select(shown, spec["name"]))
            }
            derivable = spec["from"] == "group_totals" and sums == [
                "figures",
                "amount_at_constant_rates",
            ]
            worked = own_shares(engine, persona, clear) if derivable and clear else {}
            for key, (group, other, own) in pairs.items():
                if not own:
                    taken, how = [None] * len(sums), "alone"
                elif derivable and worked.get(key, [0, Decimal(0)]) == group.total(own):
                    taken, how = worked[key], "less own"
                else:
                    # Not in the rows the persona reads (the Dutch figures are budget lines, not
                    # payments): the share comes from the source, as the public files give it.
                    taken, how = group.total(own), "less a share from the source"
                difference = [
                    None if a is None else a - (b or 0)
                    for a, b in zip(given[key], taken, strict=True)
                ]
                assert difference == group.sums[other], (persona, spec["name"], how)
                isolated.setdefault(persona, Counter())[how] += 1
            held = {tuple(r) for r in strict.persona(persona).rows(select(shown, spec["name"]))}
            assert not set(pairs) & {h[: len(grain)] for h in held}, persona
    return isolated


def test_two_aggregates_whose_difference_isolates_one_entity_are_suppressed(
    engine: Engine, visibility: Doc, aggregates: Doc
) -> None:
    # The samples' groups are small, so both lines run at a minimum group of two.
    assert aggregates["minimum_entities"] == 2
    isolated = one_entitys_figures(engine, visibility, 2)
    assert any(c["less own"] for c in isolated.values()), "no share worked out from own rows"


# Over the whole landed build.


@pytest.fixture(scope="module")
def landed_warehouse(tmp_path_factory: pytest.TempPathFactory) -> Path:
    from ingest.fetch import landing_path
    from tests.models.dbt_build import built

    for name in LANDED_SOURCES:
        landed_file(name)
    return built(tmp_path_factory.mktemp("dbt-landed"), "landed", landing_path().resolve()).database


# Each persona's cells proven over the landed build by kind, and on invented tables only. The
# readers of Chicago's payments (its accountant and the approver) are given no figure across all
# entities on the landed build (below), so their aggregate cell is proven on invented tables.
LANDED_PROVEN = {
    "accountant-chicago": {
        "aggregate invented": 1,
        "clear": 1,
        "clear invented": 1,
        "denied": 1,
        "denied invented": 5,
        "tag masked": 1,
        "tag masked invented": 2,
    },
    "accountant-uk-transport": {
        "aggregate": 1,
        "clear": 1,
        "clear invented": 1,
        "denied": 1,
        "denied invented": 5,
        "tag masked": 1,
        "tag masked invented": 2,
    },
    "accountant-nl-government": {
        "aggregate": 1,
        "clear invented": 2,
        "denied": 2,
        "denied invented": 4,
        "tag masked invented": 3,
    },
    "group-controller": {
        "clear": 3,
        "clear invented": 2,
        "denied invented": 4,
        "tag masked": 1,
        "tag masked invented": 2,
    },
    "payables-clerk": {
        "clear": 1,
        "clear invented": 1,
        "denied": 2,
        "denied invented": 5,
        "tag clear": 1,
        "tag clear invented": 2,
    },
    "approver": {
        "aggregate invented": 1,
        "clear": 1,
        "clear invented": 1,
        "denied": 1,
        "denied invented": 4,
        "post invented": 1,
        "tag masked": 1,
        "tag masked invented": 2,
    },
    "auditor": {
        "clear": 3,
        "clear invented": 4,
        "denied invented": 2,
        "tag clear": 1,
        "tag clear invented": 2,
    },
    "metric-owner": {
        "aggregate": 2,
        "aggregate invented": 1,
        "clear": 1,
        "denied invented": 4,
        "edit invented": 1,
        "tag denied": 1,
        "tag denied invented": 2,
    },
    "agent-operator": {
        "clear invented": 2,
        "denied": 3,
        "denied invented": 4,
        "tag denied": 1,
        "tag denied invented": 2,
    },
    "proposer": {
        "denied": 3,
        "denied invented": 5,
        "draft invented": 1,
        "tag denied": 1,
        "tag denied invented": 2,
    },
}
# Per reader of figures across all entities and table, the groups of at least the minimum over the
# landed build: (of its own entities only, of one other entity alone, of one other and its own,
# of two others or more). Before the entities apart every one of them was given; now only the last.
CHICAGO_READER = {"group_totals": (34, 1, 14, 0), "group_totals_left_out": (6, 1, 0, 0)}
LANDED_ISOLATING = {
    "accountant-chicago": CHICAGO_READER,
    "accountant-uk-transport": {
        "group_totals": (0, 35, 12, 2),
        "group_totals_left_out": (0, 7, 0, 0),
    },
    "accountant-nl-government": {
        "group_totals": (1, 34, 2, 12),
        "group_totals_left_out": (1, 6, 0, 0),
    },
    "approver": CHICAGO_READER,
}


@pytest.mark.landed
def test_every_persona_sees_its_matrix_over_the_landed_build(
    access: Doc,
    columns: Doc,
    aggregates: Doc,
    visibility: Doc,
    landed_warehouse: Path,
    tmp_path: Path,
) -> None:
    engine = LocalEngine(landed_warehouse, Catalogues(access, columns, aggregates), tmp_path)
    try:
        failures, tally = verdict(act_out(engine, visibility, PERSONAS))
        isolating = isolating_groups(engine, visibility, aggregates)
        isolated = one_entitys_figures(engine, visibility, aggregates["minimum_group"])
    finally:
        engine.close()
    print(f"\ncells proven, by kind ({sum(kinds(tally).values())} cells):")
    for persona, counted in tally.items():
        print(f"  {persona}: {dict(sorted(counted.items()))}")
    print(f"  all: {kinds(tally)}")
    assert failures == []
    assert {p: dict(sorted(t.items())) for p, t in tally.items()} == LANDED_PROVEN
    assert isolating == LANDED_ISOLATING
    # The figures of one other entity, alone or less the reader's own share worked out from its
    # own rows, each given at one entity apart and exactly the other's figure, none given at two.
    # The Dutch accountant's own share is budget lines, which it does not read.
    assert isolated == {
        "accountant-chicago": Counter({"alone": 2, "less own": 14}),
        "approver": Counter({"alone": 2, "less own": 14}),
        "accountant-uk-transport": Counter({"alone": 42, "less own": 12}),
        "accountant-nl-government": Counter({"alone": 40, "less a share from the source": 2}),
    }
    assert {p: sum(c.values()) for p, c in isolated.items()} == {
        p: sum(t[1] + t[2] for t in tables.values()) for p, tables in LANDED_ISOLATING.items()
    }


def isolating_groups(
    engine: Engine, matrix: Doc, aggregates: Doc
) -> dict[str, dict[str, tuple[int, int, int, int]]]:
    """Per reader of figures across all entities and per table, the groups of at least the
    minimum: of its own entities only, of one other entity alone, of one other and its own (the
    difference isolating the other), and of two others or more, the only ones it is given."""
    found: dict[str, dict[str, tuple[int, int, int, int]]] = {}
    for persona in PERSONAS:
        row = matrix["personas"][persona]
        known = known_entities(row, aggregates)
        for spec, entry, scope in aggregate_cells(row, aggregates):
            if entry["entity_rows"] or scope is not None:
                continue
            shapes = Counter[str]()
            for group in groups_of(engine.source(), spec, entry, None).values():
                if (group.count() or 0) < aggregates["minimum_group"]:
                    continue
                others = len([e for e in group.units if e not in known])
                mine = any(e in known for e in group.units)
                shapes[
                    "own" if not others else "two" if others > 1 else "less" if mine else "one"
                ] += 1
            found.setdefault(persona, {})[spec["name"]] = (
                shapes["own"],
                shapes["one"],
                shapes["less"],
                shapes["two"],
            )
    return found
