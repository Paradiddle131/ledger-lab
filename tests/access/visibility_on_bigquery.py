"""The visibility matrix's BigQuery engine: each persona's connection is a session on the lab's
BigQuery carrying a token minted for the persona's own service account (`access/bigquery.py`), so
the warehouse's applied rules decide every cell, and `tests/access/test_visibility.py` makes the
same observations through it as through the local engine. Run on the owner's machine
(`make bigquery-visibility`), off the cloud session's proxy, which replaces minted tokens.

What each observation reads on BigQuery:
- a persona's tables: every dataset `platform/gcp` declares, listed as the persona (`tables.list`
  and `tables.get`, free metadata reads); a dataset it may not list (403) or cannot see (404) it
  holds nothing in. Every table a catalogue names outside the datasets it listed (the governed
  copies, the group products' tables, the marts the aggregate views read, the other purposes'
  views and the aggregate views) is also read for its metadata as the persona, and one it is
  shown counts as held. A table-level grant on a table no catalogue names is not looked for;
  `platform/gcp` declares none.
- the names: a view of its purposes' datasets (`access/purpose_views.py`) is held as
  `<purpose dataset>.<table>`, the copy its tag treatment gives it (the clear copy's view or
  `<table>_masked`'s); the other copy's view, which its dataset grant also shows it, is counted
  as the persona and left out only when it returns no row or is refused (403), and is held under
  its own name, which no purpose lists, when it returns any. An aggregate view
  (`access/thresholds.py`) is held under its table's name, and read as any table: each is grouped
  at its grain, its units a column of it. Anything else is held as
  `<dataset>.<table>`, which no purpose lists. "No views" is held as: every view held is one the
  modules generate, with its generated query.
- the source with full rights, the reference each cell is read against, never filtered by the
  rules of the persona under test: the payments mart in `intermediate`, under no row access policy
  and no policy tag, as the publisher reads it (the source the governed copies are published from
  and the aggregate views read, and the local engine's own source); the group totals, which carry
  no row policy or tag, through the audit purpose's views as the auditor reads them, which list
  every column, and through the group close purpose's views as the group controller reads them
  when the auditor is the persona under test.
- rows: a clear cell's rows, and a tag's values with their row key, are compared as an order-free
  digest of the rows computed on each side (the count, and the sum and the exclusive or of each
  row's FARM_FINGERPRINT), since a page of an answer holds 10 MB and a product's rows are more:
  equal digests are evidence, not proof, and a differing one tells that rows differ, not how
  many. A masking rule is applied in the reference's query, by this module's SQL, not the models'
  macro; that SQL is held to `test_visibility.mask` on invented values before any cell is read.
- distinct values (the stored values a masked or denied reader must not hold anywhere) are read in
  parts by a bucket of each value's fingerprint, each part one answer, so every value is brought
  back and compared in Python as on the local engine.
- an aggregate cell's groups: the source's sums and distinct payees per group are counted in
  BigQuery, per entity, which combine only for a grain that keeps each entity apart, the only kind
  `access/thresholds.py` writes.
- every query is capped (`access/bigquery.py`'s `MAX_BYTES_BILLED`), and one refused as over the
  cap is compared in parts of its columns (`in_parts`), each keeping the row key.

Not here: the invented tables and the rebuilt catalogues, since a test cannot change BigQuery's
applied rules or tables; their cells are left to the local engine, counted as such, never as
proven.
"""

import math
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

import httpx

from access import purpose_views, thresholds
from access.bigquery import PersonaQueryError, PersonaSession, chain, mint, service_account
from access.purpose_views import same_query
from access.row_policies import ENTITY, PUBLISHER, clear_readers, path
from tests.access.test_visibility import Catalogues, Row, mask

NAME = re.compile(r"^[a-z][a-z0-9_]*$")
ENTITY_ID = re.compile(r"^[a-z][a-z0-9-]*$")
# The dataset of the marts the publisher reads.
MARTS = "intermediate"
# What one answer of distinct values may hold, inside a page's 10 MB, counting each value at the
# longest one's bytes and a row's framing beside it; an answer that still runs past one page is
# refused (`access/bigquery.py`) and fails the run, never cut short.
ANSWER_BYTES = 6_000_000
ROW_FRAMING = 32
# The persona that reads the group totals in its stead when the auditor is the one under test.
STAND_IN = {"auditor": "group-controller"}
REFERENCE = "auditor"
FIXED = (
    "BigQuery's rules and tables are the applied ones: a test can neither invent tables there nor"
    " rebuild the rules from other catalogues"
)
# Invented values the masking SQL is held to the Python rules on, never data.
MASK_PROBES = ("invented value", "abc", "")


class Unreadable(RuntimeError):
    """A read the BigQuery engine does not make: the local engine's own SQL."""


def checked(*names: str) -> None:
    for name in names:
        if not NAME.fullmatch(name):
            raise PersonaQueryError(f"{name!r} is not a column name")


def where(entities: set[str] | None) -> str:
    if entities is None:
        return ""
    for entity in entities:
        if not ENTITY_ID.fullmatch(entity):
            raise PersonaQueryError(f"{entity!r} is not an entity id")
    listed = ", ".join(f"'{e}'" for e in sorted(entities)) or "NULL"
    return f" WHERE {ENTITY} IN ({listed})"


def masked_sql(rule: str, value: str) -> str:
    """A masking rule of access/columns.yaml on a value, as `test_visibility.mask` works it out."""
    digest = f"TO_BASE64(SHA256({value}))"
    if rule == "nullify":
        return "CAST(NULL AS STRING)"
    if rule == "last-four":
        last = f"CONCAT('XXXXX', SUBSTR({value}, -4))"
        return f"CASE WHEN CHAR_LENGTH({value}) > 4 THEN {last} ELSE {digest} END"
    if rule == "hash":
        return digest
    raise PersonaQueryError(f"no masking rule {rule!r}")


def typed(kind: str, value: Any) -> Any:
    """A REST answer's cell, given as text, as the type its schema names."""
    if value is None:
        return None
    if kind in ("INTEGER", "INT64"):
        return int(value)
    if kind in ("NUMERIC", "BIGNUMERIC"):
        return Decimal(value)
    if kind in ("FLOAT", "FLOAT64"):
        return float(value)
    if kind in ("BOOLEAN", "BOOL"):
        return value == "true"
    if kind == "DATE":
        return date.fromisoformat(value)
    return value


@dataclass(frozen=True)
class Place:
    """Where a table a reader names sits on BigQuery, and the session that reads it."""

    session: PersonaSession
    dataset: str
    table: str


def fingerprint(columns: Sequence[str], apart: Mapping[str, str | None]) -> str:
    fields = ", ".join(
        f"{masked_sql(str(apart[c]), c)} AS {c}" if apart.get(c) else c for c in columns
    )
    return f"FARM_FINGERPRINT(TO_JSON_STRING(STRUCT({fields})))"


@dataclass
class BigQueryReader:
    """One persona's connection, or the source: each table it names, where that table sits, and
    what it holds."""

    project: str
    places: dict[str, Place]
    held: dict[str, dict[str, str]]
    foreign: set[str] = field(default_factory=set)
    remembered: dict[tuple[str, str], list[Row]] = field(default_factory=dict)

    def _run(self, table: str, sql: str) -> list[Row]:
        if (table, sql) not in self.remembered:
            answer = self.places[table].session.query(sql)
            kinds = answer.types or ("",) * (len(answer.rows[0]) if answer.rows else 0)
            self.remembered[(table, sql)] = [
                tuple(typed(k, v) for k, v in zip(kinds, row, strict=True)) for row in answer.rows
            ]
        return self.remembered[(table, sql)]

    def _path(self, table: str) -> str:
        place = self.places[table]
        return path(self.project, place.dataset, place.table)

    def tables(self) -> dict[str, dict[str, str]]:
        return self.held

    def views(self) -> set[str]:
        return set(self.foreign)

    def is_string(self, kind: str) -> bool:
        return kind == "STRING"

    def read(self, table: str, columns: Sequence[str]) -> list[Row]:
        checked(*columns)
        return self._run(table, f"SELECT {', '.join(columns)} FROM {self._path(table)}")  # noqa: S608

    def count(self, table: str, entities: set[str] | None, column: str | None = None) -> int:
        what = "COUNT(*)"
        if column is not None:
            checked(column)
            what = f"COUNT({column})"
        ((n,),) = self._run(table, f"SELECT {what} FROM {self._path(table)}{where(entities)}")  # noqa: S608
        return int(n)

    def digest(
        self,
        table: str,
        columns: Sequence[str],
        entities: set[str] | None,
        apart: Mapping[str, str | None] | None = None,
    ) -> tuple[Any, ...]:
        checked(*columns)
        printed = fingerprint(columns, apart or {})
        sql = (
            f"SELECT COUNT(*), CAST(SUM(CAST({printed} AS BIGNUMERIC)) AS STRING),"  # noqa: S608
            f" BIT_XOR({printed}) FROM {self._path(table)}{where(entities)}"
        )
        (row,) = self._run(table, sql)
        return tuple(row)

    def unique(self, table: str, column: str) -> bool:
        checked(column)
        ((same,),) = self._run(
            table,
            f"SELECT COUNT(*) = COUNT(DISTINCT {column}) FROM {self._path(table)}",  # noqa: S608
        )
        return bool(same)

    def distinct(self, table: str, column: str) -> set[Any]:
        checked(column)
        source = self._path(table)
        ((n, longest),) = self._run(
            table,
            f"SELECT COUNT(DISTINCT {column}), MAX(BYTE_LENGTH({column})) FROM {source}",  # noqa: S608
        )
        parts = max(1, math.ceil(int(n or 0) * (int(longest or 0) + ROW_FRAMING) / ANSWER_BYTES))
        found: set[Any] = set()
        for part in range(parts):
            bucket = (
                ""
                if parts == 1
                else f" WHERE ABS(MOD(FARM_FINGERPRINT({column}), {parts})) = {part}"
                + (f" OR {column} IS NULL" if part == 0 else "")
            )
            found |= {
                row[0]
                for row in self._run(table, f"SELECT DISTINCT {column} FROM {source}{bucket}")  # noqa: S608
            }
        return found

    def grouped(
        self,
        table: str,
        keys: Sequence[str],
        sums: Sequence[str],
        kind: str,
        of: str,
        entities: set[str] | None,
    ) -> list[tuple[Row, list[Any], Any]]:
        """Each group's payees counted in BigQuery: a group of a grain that keeps each entity
        apart is one entity's, so its count is the group's."""
        checked(*keys, *sums, of)
        units = f"COUNT(DISTINCT {of})" if kind == "count_distinct" else f"MAX({of})"
        totals = ", ".join([*(f"SUM({s})" for s in sums), units])
        listed = ", ".join(keys)
        sql = (
            f"SELECT {listed}, {totals} FROM {self._path(table)}{where(entities)}"  # noqa: S608
            f" GROUP BY {listed}"
        )
        return [
            (tuple(row[: len(keys)]), list(row[len(keys) : -1]), row[-1])
            for row in self._run(table, sql)
        ]

    def rows(self, sql: str) -> list[Row]:
        raise Unreadable("the local engine's SQL is not run on BigQuery")


def ours(view: purpose_views.View, columns: Mapping[str, Any], persona: str) -> bool:
    """Whether the persona's tag treatment gives it the view's source copy."""
    if view.audience == "all":
        return True
    clear = clear_readers(columns, view.product, view.table) or frozenset()
    return (view.audience == "clear") == (persona in clear)


def generated_query(view: thresholds.View, project: str) -> str:
    """An aggregate view's stored query: its statement after `as`."""
    sql = view.sql(project)
    return sql[sql.index(" as select ") + len(" as ") :]


@dataclass
class BigQueryEngine:
    """BigQuery in the lab's own project: `session(<persona>)` opens a persona's session
    (`as_persona_on_bigquery`), the publisher's is minted here."""

    catalogues: Catalogues
    project: str
    session: Callable[[str], PersonaSession]
    fixed: str | None = FIXED
    opened: dict[str, PersonaSession] = field(default_factory=dict)
    sources: dict[str, BigQueryReader] = field(default_factory=dict)
    client: httpx.Client | None = None
    masks_checked: bool = False

    def __post_init__(self) -> None:
        c = self.catalogues
        self.purpose_views = purpose_views.views(c.access, c.columns, c.purposes, c.aggregates)
        self.aggregate_views = thresholds.views(c.access, c.aggregates)
        self.datasets = sorted(
            purpose_views.declared_datasets(c.access, c.aggregates)
            | {v.dataset for v in self.purpose_views}
        )

    def _session(self, persona: str) -> PersonaSession:
        if persona not in self.opened:
            if persona == PUBLISHER:
                self.client = self.client or httpx.Client(timeout=60)
                accounts = chain(service_account(PUBLISHER, self.project), self.project, os.environ)
                self.opened[persona] = PersonaSession(self.project, mint(accounts), self.client)
            else:
                self.opened[persona] = self.session(persona)
        return self.opened[persona]

    def close(self) -> None:
        if self.client is not None:
            self.client.close()

    def check_masks(self) -> None:
        """The masking SQL against `test_visibility.mask` on invented values, read as the
        publisher; a literal reads no table and bills nothing."""
        rules = sorted({t["masking"] for t in self.catalogues.columns["tags"]})
        cells = [masked_sql(rule, f"'{value}'") for rule in rules for value in MASK_PROBES]
        answer = self._session(PUBLISHER).query(f"SELECT {', '.join(cells)}")
        want = tuple(mask(rule, value) for rule in rules for value in MASK_PROBES)
        if answer.rows != [want]:
            raise PersonaQueryError("the masking SQL renders other values than the masking rules")
        self.masks_checked = True

    def source(self, persona: str | None = None) -> BigQueryReader:
        if not self.masks_checked:
            self.check_masks()
        reference = STAND_IN.get(persona or "", REFERENCE)
        if reference not in self.sources:
            places: dict[str, Place] = {}
            held: dict[str, dict[str, str]] = {}
            for product in self.catalogues.access["data_products"]:
                for table in product.get("tables", []):
                    if product["scope"] == "entity":
                        place = Place(self._session(PUBLISHER), MARTS, table)
                    else:
                        (dataset,) = [
                            v.dataset
                            for v in self.purpose_views
                            if v.name == table and reference in self._purpose_personas(v)
                        ]
                        place = Place(self._session(reference), dataset, table)
                    _, columns, _ = place.session.table(place.dataset, place.table)
                    places[table], held[table] = place, dict(columns)
            self.sources[reference] = BigQueryReader(self.project, places, held)
        return self.sources[reference]

    def _purpose_personas(self, view: purpose_views.View) -> set[str]:
        return {
            p
            for u in self.catalogues.purposes["purposes"]
            if u["id"] == view.purpose
            for p in u["personas"]
        }

    def persona(self, persona: str) -> BigQueryReader:
        session = self._session(persona)
        reader = BigQueryReader(self.project, {}, {})
        purposes = {(v.dataset, v.name): v for v in self.purpose_views}
        aggregates = {(v.dataset, v.name): v for v in self.aggregate_views}
        listed: set[str] = set()
        for dataset in self.datasets:
            try:
                entries = session.tables(dataset)
            except PersonaQueryError as error:
                if error.status in (403, 404):
                    continue
                raise
            listed.add(dataset)
            for name, _ in entries:
                self._hold(reader, session, persona, dataset, name, purposes, aggregates)
        named = {
            *((v.dataset, v.name) for v in self.purpose_views),
            *((v.source_dataset, v.source) for v in self.purpose_views),
            *((v.dataset, v.name) for v in self.aggregate_views),
            *((v.source_dataset, v.source) for v in self.aggregate_views),
        }
        for dataset, name in sorted(named):
            if dataset in listed:
                continue
            try:
                session.table(dataset, name)
            except PersonaQueryError as error:
                if error.status in (403, 404):
                    continue
                raise
            self._hold(reader, session, persona, dataset, name, purposes, aggregates)
        return reader

    def _hold(
        self,
        reader: BigQueryReader,
        session: PersonaSession,
        persona: str,
        dataset: str,
        name: str,
        purposes: Mapping[tuple[str, str], purpose_views.View],
        aggregates: Mapping[tuple[str, str], thresholds.View],
    ) -> None:
        if not (NAME.fullmatch(dataset) and NAME.fullmatch(name)):
            raise PersonaQueryError("the persona holds a table whose name this engine cannot write")
        kind, columns, stored = session.table(dataset, name)
        own = f"{dataset}.{name}"
        logical, generated = own, None
        if (dataset, name) in purposes:
            view = purposes[(dataset, name)]
            generated = view.select(self.project)
            if ours(view, self.catalogues.columns, persona):
                logical = f"{dataset}.{view.table}"
            else:
                place = Place(session, dataset, name)
                probe = BigQueryReader(self.project, {own: place}, {own: dict(columns)})
                try:
                    rows = probe.count(own, None)
                except PersonaQueryError as error:
                    if error.status != 403:
                        raise
                    rows = 0  # refused whole, as a policy tag on its clear columns may refuse it
                if rows == 0:
                    return  # the other copy's view, of which the persona reads no row
        elif (dataset, name) in aggregates:
            view = aggregates[(dataset, name)]
            generated = generated_query(view, self.project)
            logical = name
        if logical in reader.held:
            logical = own
        reader.places[logical] = Place(session, dataset, name)
        reader.held[logical] = dict(columns)
        if kind == "VIEW" and (generated is None or not same_query(stored or "", generated)):
            reader.foreign.add(logical)

    def invented(self) -> "BigQueryEngine":
        raise RuntimeError(FIXED)

    def rebuilt(self, catalogues: Catalogues) -> "BigQueryEngine":
        raise RuntimeError(FIXED)
