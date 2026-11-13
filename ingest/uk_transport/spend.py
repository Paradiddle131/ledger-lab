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
in one calendar month, no two files may share one, and the twelve must be April 2025 to March 2026.
The department family and paying body must be the Department's own (`FAMILY`, `BODIES`): a value
outside them is refused, not hashed, since those columns name the payer, and a new one is a change
in what the Department publishes that a re-pin must see.

A supplier who is or may be a person (the pseudonymiser's classifier finds no evidence of an
organisation; the Department's bodies in `BODIES` are known ones) leaves only as a keyed hash
(`person:`). Their row, and a row the Department itself redacted (supplier `REDACTED`), also keeps
no description and no postal code, and its transaction number leaves as a keyed identifier hash
(`id:`, the shared one): a reference carried only by such rows never appears in clear, while one
the Department also gives clear rows (a payment-run number, the placeholder `#`) stays clear there,
and the keyed hash does not tie that clear copy to the hashed row.

On every row of the set, a description, expense type or expense area is blanked when a run of its
words is, in any order, a hashed supplier's name or a person a description introduces by initials
(`ingest/scrub.py`, the rule Chicago's landing uses). A description is also blanked when:

- it ends in such a name cut short: the Department cuts descriptions that repeat a supplier's name
  (on the real files, 32 descriptions end in a hashed supplier's name cut short), so a hashed
  person's name in another row's description would be cut the same way;
- a word that introduces a person (`by`, `for`, `from`, `c/o`, `attn`, `fao`, `per`) comes before
  one or two capital initials, each with a dot or a space after it, and a word of letters with a
  capital first: the initials and name are then also looked for in every other cell of the set; a
  figure, `&` or a capital straight after an initial makes a code, not a person (`L&P`, `A14`,
  `HS2`), and three initials make a trading name (`J R B Plant`);
- a title (`Mr`, `Dr`, `Sir`, `FAO`, `Attn` and the like) comes before such a name, whoever they
  are; `Ms` counts only so spelt, since the Department writes `MS` for Microsoft (`MS Azure`), and
  a figure after a title (`Dr 4`) is no name;
- it introduces a person by a marker (`ingest/individuals.py`): the estate or executors of someone,
  a deceased, or a name that is no company trading as another (`Ltd T/A` is a company's brand).

On the real files these blank 108 descriptions on rows whose supplier stays clear: 74 holding a
hashed supplier's name, 32 ending in one cut short, and 2 naming the one person a description
introduces by initials (the second names them without the introducer); the title and marker rules
blank none. The Department's own marker for a name it removed from a description (`NAME REMOVED`)
names nobody and stays. A company whose name contains a hashed name (`EXAMPLE` hashed, `EXAMPLE
LIMITED` in clear) stays in clear, as the pseudonymiser allows.

The landed tests (`pytest -m landed`, left out of `make check`) download the files again and
assert that every month lands once with every served row; that no hashed name, squashed by their
own spelling to letters and digits in either word order, starts at a word in any clear cell other
than a supplier; that a hashed or redacted row keeps no description, postcode or clear reference;
and that no reference carried only by hashed rows sits in clear. They only count clear supplier
cells containing a hashed name, where an organisation's name may hold one by design, and
references shared by hashed and clear rows. A pages test (`pytest -m pages`) asserts that no pinned
month's GOV.UK page lists another file or a change note. Neither the step nor the tests see a
hashed name reduced to a surname or initials, split by other words, or written run together, a
person named by a surname alone or by initials without an introducer the year ever uses, or a name
under five letters inside a word; and a hashed row's date, amount and paying body still find it in
the published file, which is public.

The rows are held in memory throughout, written to Parquet by the shared in-memory writer with
DuckDB given no directory to spill to, so nothing of the served files touches disk.
"""

import csv
import io
import re
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import TypedDict
from urllib.parse import urlsplit

import duckdb

from ingest import StepError
from ingest.individuals import (
    hash_key,
    identifier_hasher,
    introduces_a_person,
    is_organisation,
    normalise,
    person_hasher,
)
from ingest.scrub import HashedNames, parquet_from_csv

# The fiscal year the set lands, labelled by the year it opens in: April 2025 to March 2026.
FISCAL_YEAR = 2025
SET = f"uk-transport-spend-{FISCAL_YEAR}"
MONTHS = [f"{FISCAL_YEAR}-{m:02d}" for m in range(4, 13)] + [
    f"{FISCAL_YEAR + 1}-{m:02d}" for m in range(1, 4)
]
# The Department's family and the paying bodies in its files, as it spells them: the only values
# the two columns may hold, and the names the classifier knows as organisations.
FAMILY = "Department for Transport"
BODIES = (
    "Department for Transport",
    "National Highways",
    "High Speed Two (HS2) Limited",
    "Driver & Vehicle Licensing Agency",
    "Maritime and Coastguard Agency",
    "DVSA",
    "EWR",
    "British Transport Police",
    "Active Travel England",
    "VCA",
    "Transport Focus",
)

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
# What looks like a person's name in a description: up to two capital initials, each with a dot or
# a space after it, then a word of letters with a capital first, ending at a word's end; a figure,
# `&` or another capital straight after an initial makes it a code (`L&P`, `A14`, `HS2`).
NAME = r"(?:[A-Z](?:\.\s*|\s+)){0,2}[A-Z][A-Za-z'-]*[A-Za-z](?![^\W_])"
# A title, or a word for the attention of, before such a name. `Ms` counts only so spelt: the
# Department writes `MS` for Microsoft (`MS Azure`).
TITLED = re.compile(
    r"(?<![^\W_])(?:(?i:mr|mrs|miss|mx|dr|prof|sir|dame|lady|lord|attn|fao)|Ms)\.?:?\s+" + NAME
)
# A word that introduces a person, before one or two initials and a name: the person found, whose
# initials and name are then looked for across the set like a hashed supplier's.
INITIALLED = re.compile(
    r"(?<![^\W_])(?i:by|for|from|c/o|attn|fao|per)\s+"
    r"(?P<name>(?:[A-Z](?:\.\s*|\s+)){1,2}[A-Z][A-Za-z'-]*[A-Za-z](?![^\W_]))"
)

DATE = re.compile(r"([0-9]{2})/([0-9]{2})/([0-9]{4})")
AMOUNT = re.compile(r" *£([0-9]{1,3}(?:,[0-9]{3})*\.[0-9]{2}) *")


class Profile(TypedDict):
    rows: int
    files: int
    months: dict[str, int]
    suppliers_hashed: int
    rows_hashed: int
    rows_redacted: int
    references_hashed: int


class SpendError(StepError):
    """A served file is not a month of spend this step knows."""


def names_someone(text: str) -> bool:
    """A description naming someone by title, for the attention of, or by initials."""
    return bool(TITLED.search(text) or INITIALLED.search(text))


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
        if row["department_family"] != FAMILY or row["entity"] not in BODIES:
            raise SpendError(f"{where}: a paying body the step does not know")
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
    as_person, as_id = person_hasher(env), identifier_hasher(env)
    introduced = {m["name"] for row in rows for m in INITIALLED.finditer(row["item_text"])}
    hashed: set[str] = set()
    for row in rows:
        supplier = normalise(row["supplier"])
        redacted = supplier == REDACTED
        if redacted or (supplier and not is_organisation(row["supplier"], BODIES)):
            if not redacted:
                hashed.add(row["supplier"])
                row["supplier"] = as_person(row["supplier"])
            row["transaction_number"] = as_id(row["transaction_number"])
            for column in PERSONAL:
                row[column] = ""
    found = HashedNames(hashed | introduced)
    for row in rows:
        for column in FREE_TEXT:
            if row[column] and found.in_any_order(row[column]):
                row[column] = ""
        item = row["item_text"]
        if item and (found.cut_short(item) or names_someone(item) or introduces_a_person(item)):
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
    if sorted(seen) != MONTHS:
        raise SpendError(f"{len(seen)} months, not the twelve of one April-to-March year")
    scrub(rows, env)
    out = io.StringIO()
    writer = csv.DictWriter(out, COLUMNS, quoting=csv.QUOTE_ALL)
    writer.writeheader()
    writer.writerows(rows)
    dtype = {c: "BIGINT" if c == "source_row" else "VARCHAR" for c in COLUMNS}
    return parquet_from_csv(out.getvalue(), dtype=dtype)


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
        references_hashed=one("SELECT count(*) FROM s WHERE transaction_number LIKE 'id:%'"),
    )
