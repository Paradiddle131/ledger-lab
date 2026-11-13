"""The Department for Transport's spending over £25,000: the step that lands a fiscal year's twelve
monthly files as one set, and its profile.

The Department publishes one CSV a month on GOV.UK, in Windows-1252 with CRLF line ends, one row
per payment line: the department family, the paying body (`Entity`), the date as `dd/mm/yyyy`,
the expense type and area, the supplier, the transaction number, a free-text description
(`Item Text`), the amount in pounds, and the supplier's postal code. The header is spelt three
ways in fiscal year 2025 (`LAYOUTS`), always in the same order; any other header, encoding, date
or amount shape is refused, naming the file and row, never a value.

`step` receives every verified month by its URL and lands one Parquet set: each row carries the
file name it was served in (`source_file`), its position there (`source_row`, from 1), and the
served cells as text under one set of names (`COLUMNS`). An amount is the publisher's digits:
surrounding spaces, the pound sign and the thousands commas are dropped, and nothing else, so
`" £25,018.57 "` lands as `25018.57`; it never passes through a float. Each file's dates must lie
in one calendar month, and no two files may share one.

A supplier who is or may be a person (the pseudonymiser finds no evidence of an organisation; the
Department's own bodies, named in its `Department Family` and `Entity` columns, are known ones)
leaves only as a keyed hash. Their row, and a row the Department itself redacted (supplier
`REDACTED`), also keeps no description and no postal code, and its transaction number, which finds
the name again in the published file, leaves as a keyed hash of its own (`ref:`). On every row of
the set, a description, expense type or expense area is blanked when it holds a hashed supplier's
name, compared on letters and digits only, in any word order or run together; a description is
also blanked when it ends in one cut short (the Department cuts some descriptions short), and when
it names someone by title or for the attention of (`Mr`, `Dr`, `FAO`), whoever they are. The
Department's own marker for a name it removed from a description (`NAME REMOVED`) names nobody and
stays. Not caught: a hashed name reduced to a surname or initials elsewhere; and a hashed row's
date, amount and paying body still find it in the published file, which is public.

The rows are held in memory throughout: the Parquet is written to an in-memory filesystem, and
DuckDB is given no directory to spill to, so nothing of the served files touches disk.
"""

import csv
import hmac
import io
import re
import unicodedata
import uuid
from collections.abc import Callable, Mapping
from datetime import date
from pathlib import Path
from typing import TypedDict
from urllib.parse import urlsplit

import duckdb
import fsspec

from ingest.individuals import hash_key, normalise, pseudonymiser

# The set's name in the fetch kit: fiscal year 2025, April 2025 to March 2026.
SET = "uk-transport-spend-2025"

SERVED: tuple[str, ...] = (
    "department_family",
    "entity",
    "date",
    "expense_type",
    "expense_area",
    "supplier",
    "transaction_number",
    "item_text",
    "amount",
    "postal_code",
)
COLUMNS: tuple[str, ...] = ("source_file", "source_row", *SERVED)
STANDARD = (
    "Department Family",
    "Entity",
    "Date",
    "Expense Type",
    "Expense Area",
    "Supplier",
    "Transaction No",
    "Item Text",
    " £ ",
    "Postal Code",
)
# Each header the Department serves, by position onto `SERVED`: the standard one; July 2025's,
# which names the first column `Departmental Family`; December 2025's, with a space after
# `Expense Area`.
LAYOUTS: tuple[tuple[str, ...], ...] = (
    STANDARD,
    ("Departmental Family", *STANDARD[1:]),
    (*STANDARD[:4], "Expense Area ", *STANDARD[5:]),
)
FREE_TEXT = ("expense_type", "expense_area", "item_text")
# Blanked on a hashed or redacted row: what describes or locates the supplier.
PERSONAL = ("item_text", "postal_code")
REDACTED = "redacted"
# Words that name someone in a description when another word follows them.
TITLES = frozenset(
    {"mr", "mrs", "ms", "miss", "mx", "dr", "prof", "sir", "dame", "lady", "lord", "attn", "fao"}
)

DATE = re.compile(r"([0-9]{2})/([0-9]{2})/([0-9]{4})")
AMOUNT = re.compile(r" *£([0-9]{1,3}(?:,[0-9]{3})*\.[0-9]{2}) *")
WORD = re.compile(r"[^\W_]+")
MEMORY = fsspec.filesystem("memory")


class Profile(TypedDict):
    rows: int
    files: int
    months: dict[str, int]
    suppliers_hashed: int
    rows_hashed: int
    rows_redacted: int
    references_hashed: int


class SpendError(ValueError):
    """A served file is not a month of spend this step knows."""


def words(text: str) -> list[str]:
    """The letters-and-digits runs of a text, accents dropped and case folded."""
    folded = unicodedata.normalize("NFKD", text).casefold()
    return WORD.findall("".join(c for c in folded if not unicodedata.combining(c)))


class Names:
    """Hashed suppliers' names, found in a text however it spaces, orders or cuts them."""

    def __init__(self, names: set[str]) -> None:
        spelt = [w for w in map(words, names) if w]
        self.joined = {"".join(w) for w in spelt}
        self.unordered = {tuple(sorted(w)) for w in spelt}
        self.cut: dict[tuple[str, ...], set[str]] = {}
        for w in spelt:
            for k in range(1, len(w)):
                self.cut.setdefault(tuple(w[:k]), set()).add(w[k])
        self.longest = max(map(len, spelt), default=0) + 1

    def inside(self, text: str) -> bool:
        """A run of the text's words is a name, run together or in any order."""
        cell = words(text)
        for n in range(1, min(self.longest, len(cell)) + 1):
            for i in range(len(cell) - n + 1):
                run = cell[i : i + n]
                if "".join(run) in self.joined or tuple(sorted(run)) in self.unordered:
                    return True
        return False

    def cut_short(self, text: str) -> bool:
        """The text ends in a name cut short: its first words whole, the next one begun."""
        cell = words(text)
        for n in range(1, min(self.longest, len(cell))):
            head, last = tuple(cell[-n - 1 : -1]), cell[-1]
            if any(rest.startswith(last) for rest in self.cut.get(head, ())):
                return True
        return False


def names_by_title(text: str) -> bool:
    cell = words(text)
    return any(word in TITLES for word in cell[:-1])


def reference_hasher(env: Mapping[str, str] | None) -> Callable[[str], str]:
    """`ref:` and the keyed HMAC of a transaction number, labelled so it never equals a name's."""
    key = hash_key(env)
    return lambda ref: "ref:" + hmac.new(key, b"ref\0" + ref.encode(), "sha256").hexdigest()


def read_month(source: str, raw: bytes) -> tuple[str, list[dict[str, str]]]:
    """One served file's month (`YYYY-MM`) and rows under `SERVED`'s names."""
    try:
        text = raw.decode("cp1252")
    except UnicodeDecodeError:
        raise SpendError(f"{source}: not Windows-1252") from None
    reader = csv.reader(io.StringIO(text, newline=""))
    if tuple(next(reader, ())) not in LAYOUTS:
        raise SpendError(f"{source}: an unknown layout")
    rows, months = [], set()
    for position, cells in enumerate(reader, 1):
        where = f"{source} row {position}"
        if len(cells) != len(SERVED):
            raise SpendError(f"{where}: {len(cells)} fields")
        row = dict(zip(SERVED, cells, strict=True))
        day = DATE.fullmatch(row["date"])
        try:
            if day is None:
                raise ValueError
            date(int(day[3]), int(day[2]), int(day[1]))
        except ValueError:
            raise SpendError(f"{where}: the date is not a dd/mm/yyyy day") from None
        months.add(f"{day[3]}-{day[2]}")
        amount = AMOUNT.fullmatch(row["amount"])
        if amount is None:
            raise SpendError(f"{where}: the amount is not £ with thousands commas and pence")
        row["amount"] = amount[1].replace(",", "")
        row["source_file"], row["source_row"] = source, str(position)
        rows.append(row)
    if len(months) != 1:
        raise SpendError(f"{source}: dates from more than one month, or none")
    return months.pop(), rows


def scrub(rows: list[dict[str, str]], env: Mapping[str, str] | None) -> None:
    """Every row of the set changed in place: see the module's account."""
    register = {row[c] for row in rows for c in ("department_family", "entity")}
    hide, as_ref = pseudonymiser(env, register), reference_hasher(env)
    hashed: set[str] = set()
    for row in rows:
        redacted = normalise(row["supplier"]) == REDACTED
        hidden = None if redacted else hide(row["supplier"] or None)
        if redacted or (hidden is not None and hidden != row["supplier"]):
            if not redacted:
                hashed.add(row["supplier"])
                row["supplier"] = str(hidden)
            row["transaction_number"] = as_ref(row["transaction_number"])
            for column in PERSONAL:
                row[column] = ""
    names = Names(hashed)
    for row in rows:
        for column in FREE_TEXT:
            if row[column] and names.inside(row[column]):
                row[column] = ""
        if names.cut_short(row["item_text"]) or names_by_title(row["item_text"]):
            row["item_text"] = ""


def step(files: Mapping[str, bytes], env: Mapping[str, str] | None = None) -> bytes:
    """The verified months, by URL, as one text Parquet set with every possible person hashed."""
    hash_key(env)  # refuses before any row is read
    rows, seen = [], set()
    for url, raw in files.items():
        source = Path(urlsplit(url).path).name
        month, month_rows = read_month(source, raw)
        if month in seen:
            raise SpendError(f"{source}: a second file for {month}")
        seen.add(month)
        rows += month_rows
    scrub(rows, env)
    out = io.StringIO()
    writer = csv.DictWriter(out, COLUMNS, quoting=csv.QUOTE_ALL)
    writer.writeheader()
    writer.writerows(rows)
    work = f"/{uuid.uuid4().hex}"
    try:
        MEMORY.pipe(f"{work}/rows.csv", out.getvalue().encode())
        con = duckdb.connect(config={"temp_directory": ""})
        con.register_filesystem(MEMORY)
        con.read_csv(
            f"memory://{work}/rows.csv",
            header=True,
            dtype={c: "BIGINT" if c == "source_row" else "VARCHAR" for c in COLUMNS},
        ).write_parquet(f"memory://{work}/rows.parquet")
        return MEMORY.cat(f"{work}/rows.parquet")
    finally:
        if MEMORY.exists(work):
            MEMORY.rm(work, recursive=True)


def profile(path: Path) -> Profile:
    """The landed set's rows per month, and what its scrub hashed, counted."""
    con = duckdb.connect()
    con.read_parquet(str(path)).create_view("s")

    def one(sql: str) -> int:
        row = con.execute(sql).fetchone()
        if row is None:
            raise SpendError(f"no answer from {sql!r}")
        return int(row[0])

    months = con.execute(
        "SELECT substr(date, 7, 4) || '-' || substr(date, 4, 2) AS month, count(*)"
        " FROM s GROUP BY month ORDER BY month"
    ).fetchall()
    return Profile(
        rows=one("SELECT count(*) FROM s"),
        files=one("SELECT count(DISTINCT source_file) FROM s"),
        months={str(m): int(n) for m, n in months},
        suppliers_hashed=one(
            "SELECT count(DISTINCT supplier) FROM s WHERE supplier LIKE 'person:%'"
        ),
        rows_hashed=one("SELECT count(*) FROM s WHERE supplier LIKE 'person:%'"),
        rows_redacted=one("SELECT count(*) FROM s WHERE supplier = 'REDACTED'"),
        references_hashed=one("SELECT count(*) FROM s WHERE transaction_number LIKE 'ref:%'"),
    )
