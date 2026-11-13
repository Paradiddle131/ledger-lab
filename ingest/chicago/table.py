"""What the City's tables share at landing: the served CSV, checked against the columns its step
knows, every payee who may be a person hashed, and written as text Parquet, blanks as nulls, without
the rows ever touching disk; and the one-number queries their profiles run over the landed file.

A payee is hashed exactly when the pseudonymiser finds no evidence of an organisation, and the
table's identifier for payees (a vendor id) is hashed on that row with it (`id:`, a keyed hash that
still joins) and stays in clear on a row whose name is clear: an id carrying both a person-like and
a company name appears once in clear beside the company and once as a hash beside the person's
hash, and nothing links the two without the key. On a hashed row the cells that describe or locate
the payee are blanked. Columns no consumer needs and that hold contact people (street addresses)
are dropped from every row. A description on any row is blanked when a run of its words, in any
order and whatever punctuation sits between them, is a name this table hashes, or when it carries
a person marker in the form that introduces one (the estate of, a deceased, T/A). A description of an
organisation's row is otherwise landed as served: it is the known remaining place a person's name
could sit, including a name another table hashes, since each table's landing reads only its own
data.
"""

import csv
import io
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import duckdb

from ingest.individuals import identifier_hasher, introduces_a_person, pseudonymiser
from ingest.scrub import HashedNames, parquet_from_csv


class TableError(ValueError):
    """The served file is not the table its step knows, or a profile query found no answer."""


@dataclass(frozen=True)
class Table:
    # The served columns, in the publisher's order.
    columns: tuple[str, ...]
    # The payee's name, and the publisher's identifier for payees, if the table has one.
    name: str
    identifier: str | None = None
    # Blanked on a hashed row: what describes or locates the payee.
    personal: tuple[str, ...] = ()
    # Left out of the landed file on every row.
    dropped: tuple[str, ...] = ()
    # Blanked on any row where they hold a hashed name or a person marker.
    descriptions: tuple[str, ...] = ()
    # Raises on a row the step must not land; it names the column, never the value.
    check: Callable[[dict[str, str]], None] = field(default=lambda row: None)

    @property
    def landed(self) -> tuple[str, ...]:
        return tuple(c for c in self.columns if c not in self.dropped)


def scrub(rows: list[dict[str, str]], table: Table, env: Mapping[str, str] | None) -> None:
    """Every row changed in place: hashed payees, their identifiers and personal cells, dropped
    columns, and descriptions that name a hashed payee or a person."""
    hide, as_id = pseudonymiser(env), identifier_hasher(env)
    names = {row[table.name] for row in rows}
    hidden = {n: h for n in names if (h := hide(n or None)) not in (n, None)}
    found = HashedNames(hidden)
    for row in rows:
        for column in table.dropped:
            del row[column]
        if row[table.name] in hidden:
            row[table.name] = hidden[row[table.name]]
            if table.identifier and row[table.identifier]:
                row[table.identifier] = as_id(row[table.identifier])
            for column in table.personal:
                row[column] = ""
        for column in table.descriptions:
            cell = row[column]
            if cell and (introduces_a_person(cell) or found.in_any_order(cell)):
                row[column] = ""


def land(raw: bytes, table: Table, env: Mapping[str, str] | None = None) -> bytes:
    """The verified CSV as text Parquet with every payee who may be a person hashed."""
    pseudonymiser(env)  # refuses before any row is read
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8"), newline=""))
    if tuple(reader.fieldnames or ()) != table.columns:
        raise TableError("the served columns differ from the table's")
    rows = list(reader)
    for row in rows:
        table.check(row)
    scrub(rows, table, env)
    out = io.StringIO()
    writer = csv.DictWriter(out, table.landed)
    writer.writeheader()
    writer.writerows(rows)
    return parquet_from_csv(out.getvalue(), all_varchar=True)


def counter(path: Path) -> tuple[duckdb.DuckDBPyConnection, Callable[[str], int]]:
    """A connection with the landed file as the view `t`, and a function returning the one number
    a query answers."""
    con = duckdb.connect()
    con.read_parquet(str(path)).create_view("t")

    def one(sql: str) -> int:
        row = con.execute(sql).fetchone()
        if row is None or row[0] is None:
            raise TableError(f"no answer from {sql!r}")
        return int(row[0])

    return con, one
