"""The European Central Bank's euro reference rates: the step that lands them.

`step` turns the verified CSV the ECB data API serves (dataflow `EXR`, daily reference rates,
data only) into Parquet with one row per date and currency: `date`, `currency` and `rate`, the
units of the currency one euro buys, each the publisher's text, so no figure passes through a
float. A day the ECB publishes no rate (a weekend, or a day the TARGET settlement system is
closed) has no row in the served file and gets none here. The step refuses a file that is not
this series: other columns, a row that is not a daily rate against the euro, a date that is no
date, a rate that is not a positive decimal, a second rate for one date and currency, or no rate.
"""

import csv
import io
import re
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path

import duckdb

COLUMNS: tuple[str, ...] = (
    "KEY",
    "FREQ",
    "CURRENCY",
    "CURRENCY_DENOM",
    "EXR_TYPE",
    "EXR_SUFFIX",
    "TIME_PERIOD",
    "OBS_VALUE",
)
# The series: daily rates (frequency D) against the euro, the ECB reference rate (type SP00 and
# suffix A).
SERIES = ("D", "EUR", "SP00", "A")
DECIMAL = re.compile(r"[0-9]+(?:\.[0-9]+)?")


class RatesError(ValueError):
    """The served file is not the daily euro reference rates this step knows."""


def _rate(row: list[str]) -> tuple[str, str, str]:
    """A served row as (date, currency, rate), or a refusal."""
    if len(row) != len(COLUMNS):
        raise RatesError("a row has a different number of fields than the columns")
    _key, freq, currency, denominator, kind, suffix, day, value = row
    if (freq, denominator, kind, suffix) != SERIES:
        raise RatesError("a row is not a daily ECB reference rate against the euro")
    try:
        date.fromisoformat(day)
    except ValueError:
        raise RatesError("a row's date is not a date") from None
    if not DECIMAL.fullmatch(value) or Decimal(value) <= 0:
        raise RatesError("a rate is not a positive decimal")
    return day, currency, value


def step(raw: bytes) -> bytes:
    """The verified CSV as Parquet, one row per date and currency, every field as served."""
    reader = csv.reader(io.StringIO(raw.decode("utf-8"), newline=""))
    if tuple(next(reader, ())) != COLUMNS:
        raise RatesError("the served columns differ from the reference rates'")
    rates: dict[tuple[str, str], str] = {}
    for row in reader:
        day, currency, value = _rate(row)
        if (day, currency) in rates:
            raise RatesError("a date and currency has more than one rate")
        rates[day, currency] = value
    if not rates:
        raise RatesError("the served file holds no rate")
    with tempfile.TemporaryDirectory() as work:
        parquet = Path(work, "rates.parquet")
        con = duckdb.connect()
        con.execute("CREATE TABLE rates (date VARCHAR, currency VARCHAR, rate VARCHAR)")
        con.executemany(
            "INSERT INTO rates VALUES (?, ?, ?)", [(d, c, v) for (d, c), v in rates.items()]
        )
        con.sql("SELECT * FROM rates ORDER BY currency, date").write_parquet(str(parquet))
        con.close()
        return parquet.read_bytes()
