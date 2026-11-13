"""What the City's tables share at landing: the served CSV, checked against the columns its step
knows, every payee who may be a person hashed, and written as text Parquet, blanks as nulls, without
the rows ever touching disk; and the one-number queries their profiles run over the landed file.

A payee is hashed when the pseudonymiser finds no evidence of an organisation, and also when the
table's identifier for payees (a vendor id) ever appears beside a hashed name: the id would
otherwise link a hash to a clear name, so every name an id carries is hashed together, and the id
with them (`id:`, a keyed hash, still a join key). On a hashed row the cells that describe or
locate the payee are blanked. A free-text cell on any row, hashed or not, is blanked when a run
of its words is a hashed payee's name, since a name repeated in another row's description undoes
its hash.
"""

import csv
import io
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import duckdb
import fsspec

from ingest.individuals import (
    hash_key,
    identifier_hasher,
    is_organisation,
    normalise,
    person_hasher,
)

MEMORY = fsspec.filesystem("memory")
LETTER = re.compile(r"[^\W\d_]")


class TableError(ValueError):
    """The served file is not the table its step knows, or a profile query found no answer."""


@dataclass(frozen=True)
class Table:
    columns: tuple[str, ...]
    # The payee's name, and the publisher's identifier for payees, if the table has one.
    name: str
    identifier: str | None = None
    # Blanked on a hashed row: what describes or locates the payee.
    personal: tuple[str, ...] = ()
    # Blanked on any row where a run of its words is a hashed name.
    free_text: tuple[str, ...] = ()
    # Raises on a row the step must not land; it names the column, never the value.
    check: Callable[[dict[str, str]], None] = field(default=lambda row: None)


def words_inside(cell: str, longest: int) -> set[str]:
    """Every run of up to `longest` consecutive words of a cell that holds a letter, each normalised
    as a name is; a run of figures alone spells no one's name."""
    words = cell.split()
    runs = (
        normalise(" ".join(words[i : i + n]))
        for n in range(1, longest + 1)
        for i in range(len(words) - n + 1)
    )
    return {run for run in runs if LETTER.search(run)}


def hashed_names(rows: list[dict[str, str]], table: Table) -> set[str]:
    """The raw names the step hashes: no evidence of an organisation, or carried by an identifier
    that also carries such a name."""
    names = {row[table.name] for row in rows if normalise(row[table.name])}
    hashed = {name for name in names if not is_organisation(name)}
    if table.identifier is None:
        return hashed
    pairs = {(row[table.name], row[table.identifier]) for row in rows if row[table.identifier]}
    while True:
        ids = {ident for name, ident in pairs if name in hashed}
        joined = {name for name, ident in pairs if ident in ids and normalise(name)} - hashed
        if not joined:
            return hashed
        hashed |= joined


def scrub(rows: list[dict[str, str]], table: Table, env: Mapping[str, str] | None) -> None:
    """Every row changed in place: hashed payees, their identifiers and personal cells, and any
    free-text cell holding a hashed name."""
    as_person, as_id = person_hasher(env), identifier_hasher(env)
    hashed = hashed_names(rows, table)
    keys = {normalise(name) for name in hashed}
    longest = max((len(key.split()) for key in keys), default=0)
    for row in rows:
        if row[table.name] in hashed:
            row[table.name] = as_person(row[table.name])
            if table.identifier and row[table.identifier]:
                row[table.identifier] = as_id(row[table.identifier])
            for column in table.personal:
                row[column] = ""
        for column in table.free_text:
            if row[column] and words_inside(row[column], longest) & keys:
                row[column] = ""


def land(raw: bytes, table: Table, env: Mapping[str, str] | None = None) -> bytes:
    """The verified CSV as text Parquet with every payee who may be a person hashed."""
    hash_key(env)  # refuses before any row is read
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8"), newline=""))
    if tuple(reader.fieldnames or ()) != table.columns:
        raise TableError("the served columns differ from the table's")
    rows = list(reader)
    for row in rows:
        table.check(row)
    scrub(rows, table, env)
    out = io.StringIO()
    writer = csv.DictWriter(out, table.columns)
    writer.writeheader()
    writer.writerows(rows)
    work = f"/{uuid.uuid4().hex}"
    try:
        MEMORY.pipe(f"{work}/rows.csv", out.getvalue().encode())
        con = duckdb.connect()
        con.register_filesystem(MEMORY)
        con.read_csv(f"memory://{work}/rows.csv", header=True, all_varchar=True).write_parquet(
            f"memory://{work}/rows.parquet"
        )
        return MEMORY.cat(f"{work}/rows.parquet")
    finally:
        if MEMORY.exists(work):
            MEMORY.rm(work, recursive=True)


def hashed_names_inside(
    path: Path, env: Mapping[str, str] | None = None, longest: int = 10
) -> dict[str, int]:
    """The leak check over a landed file, across every row: for each column, how many cells in
    clear hold a run of up to `longest` words whose person hash is one the file holds. Needs the
    key the file was landed with."""
    as_person = person_hasher(env)
    con, _ = counter(path)
    columns = [c for (c,) in con.execute("SELECT name FROM pragma_table_info('t')").fetchall()]
    hashes = set()
    for column in columns:
        hashes |= {
            v
            for (v,) in con.execute(
                f'SELECT DISTINCT "{column}" FROM t WHERE "{column}" LIKE \'person:%\''  # noqa: S608
            ).fetchall()
        }
    found = {}
    for column in columns:
        cells = con.execute(
            f'SELECT "{column}", count(*) FROM t WHERE "{column}" IS NOT NULL'  # noqa: S608
            f" AND \"{column}\" NOT LIKE 'person:%' AND \"{column}\" NOT LIKE 'id:%'"
            f' GROUP BY "{column}"'
        ).fetchall()
        found[column] = sum(
            n for cell, n in cells if {as_person(w) for w in words_inside(cell, longest)} & hashes
        )
    return found


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
