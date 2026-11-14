"""Compile the access catalogues into one database per persona on the local engine.

DuckDB has no users, grants, row policies or masking, so the local engine cannot filter what a
connection reads: anything a connection can open, it reads whole. A persona's database therefore
holds only what the persona may see, materialised from the catalogues by the account that builds
it, and everything else is simply absent:

- the row rule: of an entity-scoped product's tables, only the rows of the entities the persona's
  `read` or `edit` grants name (`access/personas.yaml`); of a group product, every row;
- the column rules: a column tagged in `access/columns.yaml` holds the stored value for the tag's
  clear readers, the tag's masking rule applied to the stored value for its masked readers, and
  is left out for a persona that is neither, so no query can name it;
- the minimum group: an `aggregate` grant gives the product's aggregate tables of
  `access/aggregates.yaml` and never its rows, each table holding only the groups of at least the
  minimum, so no filter or grain the persona writes can reach a smaller one;
- the entities apart: a table of figures across all entities keeps a group only when it holds at
  least the minimum entities among those whose rows the persona reads in no product, and their
  rows alone hold the minimum group, so neither the group nor the group less what the persona
  reads in clear is one entity's figure or a small group.

Masked values are stored masked, so a filter, a join or a count on a masked column sees the masked
value, as on BigQuery, where predicates read the masked column. The persona's connection
(`connect`) is read-only, reaches no file, loads no extension and cannot change a setting.

The compile is a pure function of the catalogues and the source tables' columns, so the same
inputs give the same statements, and it refuses what it cannot express (an unknown access, a tag
with no masking rule it knows, a column or table the source lacks, an aggregate over a tagged
column) instead of leaving anything open. Persona, product and table ids are schema-checked
slugs; every identifier and literal is quoted all the same.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb

from access.identities import ReservedPersonaError, identity

# Every access a grant may give, and what it compiles to on the local engine: `read` and `edit`
# the product's rows, `aggregate` its aggregate tables; `draft` and `post` act through the
# proposal and approval workflow, which carries the entry, so they read nothing.
ACCESSES = frozenset({"read", "edit", "aggregate", "draft", "post"})
AGGREGATE = "aggregate"
# The column every entity-scoped product's tables hold their entity in: the payments contract's
# row boundary.
ENTITY = "entity_id"
# The catalog name the build attaches the source warehouse under.
SOURCE = "warehouse"
MASKING_RULES = frozenset({"nullify", "hash", "last-four"})
# BigQuery's hash and last-four rules apply to strings only; nullify to any type.
STRING_ONLY = frozenset({"hash", "last-four"})

# A table's columns as the source holds them: name and DuckDB type, in table order.
Schema = Mapping[str, Sequence[tuple[str, str]]]


class CompileError(ValueError):
    """The catalogues ask for something the compiler cannot express, so nothing is compiled."""


@dataclass(frozen=True)
class Column:
    name: str
    type: str
    # `clear`, or the masking rule applied to the stored value.
    rule: str


@dataclass(frozen=True)
class Rows:
    """A product table copied with the persona's rows and columns."""

    table: str
    entities: frozenset[str] | None  # None: every row (a group product)
    columns: tuple[Column, ...]


@dataclass(frozen=True)
class Aggregate:
    """An aggregate table: the source's rows of the persona's entities grouped at one grain."""

    name: str
    source: str
    entities: frozenset[str] | None
    group_by: tuple[str, ...]
    sums: tuple[str, ...]
    units: str
    # `count_distinct` or `max`, over the column `units_of`.
    count: str
    units_of: str
    minimum: int
    # For figures across all entities: the entities whose rows the persona reads in some product,
    # whose share of a group it can work out and take away, and the fewest other entities a kept
    # group must hold. `known` is None for a table that keeps each entity apart.
    known: frozenset[str] | None = None
    minimum_entities: int = 0


@dataclass(frozen=True)
class Plan:
    persona: str
    tables: tuple[Rows | Aggregate, ...]


def quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def source_schema(database: Path) -> dict[str, list[tuple[str, str]]]:
    """The columns of every table in the source warehouse's main schema, in table order."""
    with duckdb.connect(str(database), read_only=True) as db:
        rows = db.execute(
            "select table_name, column_name, data_type from duckdb_columns()"
            " where schema_name = 'main' order by table_name, column_index"
        ).fetchall()
    schema: dict[str, list[tuple[str, str]]] = {}
    for table, column, kind in rows:
        schema.setdefault(table, []).append((column, kind))
    return schema


def column_rules(columns: Mapping[str, Any], persona: str) -> dict[str, dict[str, str]]:
    """Per data product, each tagged `<table>.<column>` with what the persona gets: `clear`, the
    tag's masking rule, or `denied`."""
    tags: dict[str, str] = {}
    for tag in columns["tags"]:
        if tag["masking"] not in MASKING_RULES:
            raise CompileError(f"tag {tag['id']}: no masking rule {tag['masking']!r}")
        clear, masked = persona in tag["clear"], persona in tag["masked"]
        if clear and masked:
            raise CompileError(f"{persona} reads tag {tag['id']} both in clear and masked")
        tags[tag["id"]] = "clear" if clear else tag["masking"] if masked else "denied"
    rules: dict[str, dict[str, str]] = {}
    for product, entry in columns["products"].items():
        for column, tag in entry.get("columns", {}).items():
            if tag not in tags:
                raise CompileError(f"{product} {column}: no tag {tag!r}")
            rules.setdefault(product, {})[column] = tags[tag]
    return rules


def _entities(catalogue: Mapping[str, Any], persona: str, product: str) -> frozenset[str] | None:
    scope = {p["id"]: p["scope"] for p in catalogue["data_products"]}[product]
    if scope != "entity":
        return None
    return frozenset(
        g["entity"]
        for g in catalogue["grants"]
        if g["persona"] == persona and g["product"] == product and g["access"] == AGGREGATE
    )


def _columns_of(schema: Schema, table: str) -> dict[str, str]:
    if table not in schema:
        raise CompileError(f"the source has no table {table}")
    return dict(schema[table])


def compile_persona(
    catalogue: Mapping[str, Any],
    columns: Mapping[str, Any],
    aggregates: Mapping[str, Any],
    persona: str,
    schema: Schema,
) -> Plan:
    """What the persona's database holds, from the three catalogues and the source's columns."""
    who = identity(catalogue, persona)
    mine = [g for g in catalogue["grants"] if g["persona"] == persona]
    for grant in mine:
        if grant["access"] not in ACCESSES:
            raise CompileError(f"{persona}: no rule for the access {grant['access']!r}")
    minimum = aggregates["minimum_group"]
    if not isinstance(minimum, int) or isinstance(minimum, bool) or minimum < 2:
        raise CompileError(f"the minimum group must be a whole number of two or more: {minimum!r}")
    apart = aggregates["minimum_entities"]
    if not isinstance(apart, int) or isinstance(apart, bool) or apart < 1:
        raise CompileError(f"the minimum entities must be a whole number of one or more: {apart!r}")
    known = frozenset(e for entities in who.reads.values() if entities for e in entities)
    tables_of = {p["id"]: list(p.get("tables", [])) for p in catalogue["data_products"]}
    rules = column_rules(columns, persona)
    for product, tagged in rules.items():
        for column in tagged:
            table, _, name = column.partition(".")
            if not tables_of.get(product):
                continue  # the models build no table of it yet: the tag awaits one
            if table not in tables_of[product]:
                raise CompileError(f"{product} {column}: {table} is not a table of {product}")
            if name not in _columns_of(schema, table):
                raise CompileError(f"{product}: the source table {table} has no {name}")
    plan: list[Rows | Aggregate] = []
    for product in sorted(who.reads):
        entities = who.reads[product]
        for table in tables_of[product]:
            source = _columns_of(schema, table)
            if entities is not None and ENTITY not in source:
                raise CompileError(f"{product} is entity-scoped but {table} has no {ENTITY}")
            tagged = rules.get(product, {})
            kept = tuple(
                Column(name, kind, rule)
                for name, kind in source.items()
                if (rule := tagged.get(f"{table}.{name}", "clear")) != "denied"
            )
            for column in kept:
                if column.rule in STRING_ONLY and column.type != "VARCHAR":
                    raise CompileError(f"{table}.{column.name}: {column.rule} masks strings only")
            plan.append(Rows(table, entities, kept))
    for product in sorted({g["product"] for g in mine if g["access"] == AGGREGATE}):
        if product in who.reads:
            raise CompileError(f"{persona} reads {product} both by rows and in aggregate")
        if not tables_of[product]:
            continue  # the models build no table of it yet: nothing to aggregate
        entry = aggregates["products"].get(product)
        if not entry or not entry["tables"]:
            raise CompileError(f"{product}: an aggregate grant with no aggregate table")
        if not isinstance(entry["entity_rows"], bool):
            raise CompileError(f"{product}: entity_rows must be true or false")
        for view in entry["tables"]:
            if view["from"] not in tables_of[product]:
                raise CompileError(f"{view['name']}: {view['from']} is not a table of {product}")
            source = _columns_of(schema, view["from"])
            units = view["units"]
            count = "count_distinct" if "count_distinct" in units else "max"
            if count not in units or set(units) != {"name", count}:
                raise CompileError(f"{view['name']}: units count no column it knows: {units}")
            group_by = list(view["group_by"])
            sums = list(view["sum"])
            if entry["entity_rows"]:
                group_by += view["entity_group_by"]
                sums += view.get("entity_sum", [])
            elif ENTITY in group_by:
                raise CompileError(f"{view['name']}: all-entity figures grouped by {ENTITY}")
            shown = [*group_by, *sums]
            if units["name"] in shown or len(shown) != len(set(shown)):
                raise CompileError(f"{view['name']}: a column is shown twice: {shown}")
            for name in [*shown, units[count]]:
                if name not in source:
                    raise CompileError(f"{view['name']}: {view['from']} has no column {name}")
            personal = {
                name
                for table, _, name in (c.partition(".") for c in rules.get(product, {}))
                if table == view["from"]
            }
            if personal & set(shown):
                raise CompileError(f"{view['name']} shows a tagged column: {sorted(personal)}")
            entities = _entities(catalogue, persona, product)
            if entities is not None and ENTITY not in source:
                raise CompileError(f"{product} is entity-scoped but {view['from']} has no {ENTITY}")
            if not entry["entity_rows"] and ENTITY not in source:
                raise CompileError(f"{view['name']}: {view['from']} has no {ENTITY} to keep apart")
            plan.append(
                Aggregate(
                    view["name"],
                    view["from"],
                    entities,
                    tuple(group_by),
                    tuple(sums),
                    units["name"],
                    count,
                    units[count],
                    minimum,
                    None if entry["entity_rows"] else known,
                    0 if entry["entity_rows"] else apart,
                )
            )
    names = [t.table if isinstance(t, Rows) else t.name for t in plan]
    if len(names) != len(set(names)):
        raise CompileError(f"{persona}: two tables share a name: {sorted(names)}")
    return Plan(persona, tuple(plan))


def masked(column: Column) -> str:
    """The SQL that gives the column as the persona reads it, as BigQuery's rule renders it.

    The hash rule is SHA-256 of the stored string's UTF-8 bytes, encoded in Base64, so the masked
    value is a string again and equal values still join and count. BigQuery's documentation says
    the rule preserves the type (a STRING column masks to a STRING); that the bytes are rendered in
    Base64 is read from its examples' 44-character values, not from a sentence that says so, so
    the rendering is kept here alone. Last four keeps the last four characters after `XXXXX`, and
    hashes a value of four characters or fewer, as BigQuery's rule does; nullify reads null.
    """
    name = quote(column.name)
    digest = f"to_base64(unhex(sha256({name})))"
    if column.rule == "clear":
        return name
    if column.rule == "hash":
        return digest
    if column.rule == "last-four":
        return f"case when length({name}) > 4 then 'XXXXX' || right({name}, 4) else {digest} end"
    if column.rule == "nullify":
        return f"cast(null as {column.type})"
    raise CompileError(f"{column.name}: no masking rule {column.rule!r}")


def _where(entities: frozenset[str] | None) -> str:
    if entities is None:
        return ""
    # An empty set of entities still compiles, to no rows.
    listed = ", ".join(literal(e) for e in sorted(entities)) or "null"
    return f" where {quote(ENTITY)} in ({listed})"


def _kept(table: Aggregate) -> str:
    """The condition a group of the aggregate table meets to be written."""
    of = quote(table.units_of)
    count = "count(distinct {})" if table.count == "count_distinct" else "max({})"
    if table.known is None:
        return f"{count.format(of)} >= {table.minimum}"
    # Only the rows of entities the persona reads in no product count, written as CASE since
    # BigQuery has no FILTER clause; with no entity known, every row is another entity's.
    entity = quote(ENTITY)
    if table.known:
        other = f"{entity} not in ({', '.join(literal(e) for e in sorted(table.known))})"
        of, entity = (f"case when {other} then {c} end" for c in (of, entity))
    return (
        f"{count.format(of)} >= {table.minimum}"
        f" and count(distinct {entity}) >= {table.minimum_entities}"
    )


def statements(plan: Plan) -> list[str]:
    """The SQL that builds the persona's database, with the source attached as SOURCE."""
    sql: list[str] = []
    for table in plan.tables:
        if isinstance(table, Rows):
            listed = ", ".join(f"{masked(c)} as {quote(c.name)}" for c in table.columns)
            source = f"{SOURCE}.main.{quote(table.table)}"
            sql.append(
                f"create table {quote(table.table)} as select {listed}"  # noqa: S608
                f" from {source}{_where(table.entities)}"
            )
        else:
            of = quote(table.units_of)
            units = f"count(distinct {of})" if table.count == "count_distinct" else f"max({of})"
            keys = ", ".join(quote(c) for c in table.group_by)
            sums = "".join(f", sum({quote(c)}) as {quote(c)}" for c in table.sums)
            source = f"{SOURCE}.main.{quote(table.source)}"
            sql.append(
                f"create table {quote(table.name)} as select {keys}{sums},"  # noqa: S608
                f" {units} as {quote(table.units)} from {source}{_where(table.entities)}"
                f" group by {keys} having {_kept(table)}"
            )
    return sql


def materialise(plan: Plan, warehouse: Path, directory: Path) -> Path:
    """Build `<persona>.duckdb` in `directory` from `warehouse`; every table is written once, from
    its final select, so no row or value the persona may not see is ever written to the file."""
    database = directory / f"{plan.persona}.duckdb"
    if database.exists():
        raise FileExistsError(database)
    with duckdb.connect(str(database)) as build:
        # DuckDB renames a file whose stem it reserves (`system` opens as `system_db`), and the
        # build attaches the source as SOURCE: either way the catalog could not be the persona.
        (catalog,) = build.execute("select system.main.current_database()").fetchone() or ("",)
        if catalog != plan.persona or plan.persona == SOURCE:
            raise ReservedPersonaError(
                f"{plan.persona} cannot name a DuckDB catalog: it is reserved or used by the build"
            )
        build.execute(f"attach {literal(str(warehouse))} as {SOURCE} (read_only)")
        for statement in statements(plan):
            build.execute(statement)
        build.execute(f"detach {SOURCE}")
        build.execute("checkpoint")
    return database


def connect(database: Path) -> duckdb.DuckDBPyConnection:
    """The persona's connection: read-only, no file beyond its database, no extension to load,
    and every setting locked, so it can lift none of these."""
    return duckdb.connect(
        str(database),
        read_only=True,
        config={
            "enable_external_access": False,
            "autoload_known_extensions": False,
            "autoinstall_known_extensions": False,
            "lock_configuration": True,
        },
    )


def open_persona(
    catalogue: Mapping[str, Any],
    columns: Mapping[str, Any],
    aggregates: Mapping[str, Any],
    persona: str,
    warehouse: Path,
    directory: Path,
) -> duckdb.DuckDBPyConnection:
    """Compile the persona's rules over `warehouse`, build its database and connect as it."""
    plan = compile_persona(catalogue, columns, aggregates, persona, source_schema(warehouse))
    return connect(materialise(plan, warehouse, directory))
