"""The Dutch central government's budget statements: the step that lands them, and their profile.

The Ministry of Finance publishes the Rijksbegrotingsstaten as one semicolon-separated file of
every year since 2013. A served row is one article (`Artikelnummer`) of one budget chapter
(`HoofdstukRomeins`) in one year (`Begrotingsjaar`), for one kind of amount (`VUO`: `V`
commitments, `U` cash expenditure, `O` receipts), with a column per budget moment: `StandOWB`
the draft budget, `StandO1` the first supplementary budget, `StandSBS` the September
supplementary budget, `StandO2` the second supplementary budget, and `Realisatie` the outturn as
the final act settles it.

The file states no unit; the publisher's budget documents on rijksfinancien.nl show the same
figures under "bedragen x € 1.000", so every amount is in thousands of euros. For one: the 2024
annual report for chapter I (`/jaarverslag/2024/I`, table 1) shows the outturn of article 1 as
12.043, and the file serves `12043`; the landed test checks that row.

`step` lands every served year as Parquet with one row per served row and moment: the served
row's position among the served rows (`source_row`, an integer from 1), its key columns, the
moment as the publisher's column name, the amount as published, and the unit. Every column but
the position is the publisher's text, a blank as the empty text it was served as; typing, and
treating a zero as a moment not yet reached, are the models' job. A year that is not four digits
is refused, so no row is dropped or misfiled quietly.

`profile` counts, per year and moment, the rows and those with a non-zero amount, and the mess:
units, amounts that are not whole numbers, and articles the publisher serves on several rows,
which each carry some of the article's moments and can repeat the others.
"""

import csv
import io
import re
import tempfile
from pathlib import Path
from typing import TypedDict

import duckdb

KEYS: tuple[str, ...] = (
    "Begrotingsjaar",
    "VUO",
    "HoofdstukRomeins",
    "Hoofdstuknaam",
    "Artikelnummer",
    "Artikelnaam",
)
MOMENTS: tuple[str, ...] = ("StandOWB", "StandO1", "StandSBS", "StandO2", "Realisatie")
COLUMNS: tuple[str, ...] = (*KEYS, *MOMENTS)
LANDED: dict[str, str] = {
    "source_row": "BIGINT",
    **{k: "VARCHAR" for k in (*KEYS, "moment", "amount", "unit")},
}
UNIT = "EUR thousand"
YEAR = re.compile(r"[0-9]{4}")


class Profile(TypedDict):
    rows: int
    articles_on_several_rows: int


class BudgetError(ValueError):
    """The served file is not the budget statements this step knows."""


def step(raw: bytes) -> bytes:
    """The verified file as Parquet, one row per served row and moment."""
    reader = csv.reader(io.StringIO(raw.decode("utf-8"), newline=""), delimiter=";")
    if tuple(next(reader, ())) != COLUMNS:
        raise BudgetError("the served columns differ from the budget statements'")
    with tempfile.TemporaryDirectory() as work:
        long, parquet = Path(work, "budget.csv"), Path(work, "budget.parquet")
        with long.open("w", newline="") as out:
            writer = csv.writer(out, quoting=csv.QUOTE_ALL)
            writer.writerow(LANDED)
            for position, served in enumerate(reader, 1):
                if len(served) != len(COLUMNS):
                    raise BudgetError(f"served row {position} has {len(served)} fields")
                row = dict(zip(COLUMNS, served, strict=True))
                if not YEAR.fullmatch(row["Begrotingsjaar"]):
                    raise BudgetError(f"served row {position} has a year that is not four digits")
                keys = [row[k] for k in KEYS]
                for moment in MOMENTS:
                    writer.writerow([position, *keys, moment, row[moment], UNIT])
        # Every field is quoted and quoted fields are never null, so a blank stays empty text.
        duckdb.read_csv(
            str(long),
            header=True,
            sep=",",
            quotechar='"',
            escapechar='"',
            dtype=LANDED,
            allow_quoted_nulls=False,
        ).write_parquet(str(parquet))
        return parquet.read_bytes()


def profile(path: Path) -> Profile:
    """The landed file's rows and the articles it serves on several rows, counted."""
    con = duckdb.connect()
    con.read_parquet(str(path)).create_view("b")

    def first_row(sql: str) -> tuple[object, ...]:
        row = con.execute(sql).fetchone()
        if row is None:
            raise BudgetError(f"no answer from {sql!r}")
        return row

    def one(sql: str) -> int:
        return int(str(first_row(sql)[0]))

    return Profile(
        rows=one("SELECT count(*) FROM b"),
        articles_on_several_rows=one(
            "SELECT count(*) FROM (SELECT 1 FROM b WHERE moment = 'StandOWB'"
            " GROUP BY Begrotingsjaar, VUO, HoofdstukRomeins, Artikelnummer HAVING count(*) > 1)"
        ),
    )
