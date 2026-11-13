"""The Dutch central government's budget statements: the step that lands them, and their profile.

The Ministry of Finance publishes the Rijksbegrotingsstaten as one semicolon-separated file of
every year since 2013. A served row is one article (`Artikelnummer`) of one budget chapter
(`HoofdstukRomeins`) in one year (`Begrotingsjaar`), for one kind of amount (`VUO`: `V`
commitments, `U` cash expenditure, `O` receipts), with a column per budget moment: `StandOWB`
the draft budget, `StandO1` the first supplementary budget, `StandSBS` the September
supplementary budget, `StandO2` the second supplementary budget, and `Realisatie` the outturn as
the final act settles it. The publisher shows the same figures in its budget documents as
"bedragen x € 1.000", so every amount is in thousands of euros.

`step` lands the years in `YEARS` as Parquet with one row per served row and moment: the served
row's key columns, its position among the served rows (`source_row`, from 1), the moment as the
publisher's column name, the amount as published, and the unit. Every column is the publisher's
text; typing, and treating a zero as a moment not yet reached, are the models' job.

`profile` counts, per year and moment, the rows and those with a non-zero amount, and the mess:
units, amounts that are not whole numbers, and articles the publisher serves on several rows,
which each carry some of the article's moments and can repeat the others.
"""

import csv
import io
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
LANDED: tuple[str, ...] = ("source_row", *KEYS, "moment", "amount", "unit")
# The years the fiscal calendars cover whose outturn is published.
YEARS: tuple[str, ...] = ("2023", "2024", "2025")
UNIT = "EUR thousand"


class MomentCount(TypedDict):
    rows: int
    non_zero: int


class Profile(TypedDict):
    rows: int
    units: list[str]
    moments: dict[str, dict[str, MomentCount]]
    non_integer_amounts: int
    articles_on_several_rows: int


class BudgetError(ValueError):
    """The served file is not the budget statements this step knows."""


def step(raw: bytes) -> bytes:
    """The verified file as Parquet, one row per served row and moment, for the chosen years."""
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
                if row["Begrotingsjaar"] not in YEARS:
                    continue
                for moment in MOMENTS:
                    keys = [row[k] for k in KEYS]
                    writer.writerow([position, *keys, moment, row[moment], UNIT])
        duckdb.read_csv(
            str(long), header=True, all_varchar=True, sep=",", quotechar='"'
        ).write_parquet(str(parquet))
        return parquet.read_bytes()


def profile(path: Path) -> Profile:
    """The landed file's moments per year, and its mess, counted."""
    con = duckdb.connect()
    con.read_parquet(str(path)).create_view("b")

    def one(sql: str) -> int:
        row = con.execute(sql).fetchone()
        if row is None:
            raise BudgetError(f"no answer from {sql!r}")
        return int(row[0])

    moments: dict[str, dict[str, MomentCount]] = {}
    for year, moment, n, non_zero in con.execute(
        "SELECT Begrotingsjaar, moment, count(*),"
        " count(*) FILTER (WHERE TRY_CAST(amount AS HUGEINT) <> 0) FROM b GROUP BY ALL"
    ).fetchall():
        moments.setdefault(str(year), {})[str(moment)] = MomentCount(
            rows=int(n), non_zero=int(non_zero)
        )
    return Profile(
        rows=one("SELECT count(*) FROM b"),
        units=[str(u) for (u,) in con.execute("SELECT DISTINCT unit FROM b ORDER BY 1").fetchall()],
        moments={
            year: {m: moments[year][m] for m in MOMENTS if m in moments[year]}
            for year in sorted(moments)
        },
        non_integer_amounts=one(
            "SELECT count(*) FROM b WHERE NOT regexp_full_match(coalesce(amount, ''), '-?[0-9]+')"
        ),
        articles_on_several_rows=one(
            "SELECT count(*) FROM (SELECT 1 FROM b WHERE moment = 'StandOWB'"
            " GROUP BY Begrotingsjaar, VUO, HoofdstukRomeins, Artikelnummer HAVING count(*) > 1)"
        ),
    )
