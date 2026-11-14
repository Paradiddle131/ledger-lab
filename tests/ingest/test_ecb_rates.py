"""The ECB's euro reference rates land one row per date and currency, with closing days left out.

The rows below are real reference rates around Easter 2024 (Source: European Central Bank), as the
data API serves them; the ECB publishes them for information purposes only.
"""

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import duckdb
import pytest
import yaml
from conftest import REPO, landed_file

from ingest import fetch
from ingest.ecb.rates import COLUMNS, RatesError, step

SOURCE = "ecb-rates"
HEADER = ",".join(COLUMNS)
# Easter 2024: Good Friday (29 March) and Easter Monday (1 April) are TARGET closing days.
SERVED_ROWS = [
    "EXR.D.GBP.EUR.SP00.A,D,GBP,EUR,SP00,A,2024-03-27,0.85768",
    "EXR.D.GBP.EUR.SP00.A,D,GBP,EUR,SP00,A,2024-03-28,0.8551",
    "EXR.D.GBP.EUR.SP00.A,D,GBP,EUR,SP00,A,2024-04-02,0.8551",
    "EXR.D.USD.EUR.SP00.A,D,USD,EUR,SP00,A,2024-03-27,1.0816",
    "EXR.D.USD.EUR.SP00.A,D,USD,EUR,SP00,A,2024-03-28,1.0811",
    "EXR.D.USD.EUR.SP00.A,D,USD,EUR,SP00,A,2024-04-02,1.0749",
]


def served(*lines: str, header: str = HEADER) -> bytes:
    return ("\r\n".join([header, *lines]) + "\r\n").encode()


def rows(path: Path) -> list[tuple[str, str, str]]:
    return [
        (str(d), str(c), str(r))
        for d, c, r in duckdb.read_parquet(str(path))
        .order("currency, date")
        .select("date, currency, rate")
        .fetchall()
    ]


def landed(tmp_path: Path, raw: bytes) -> Path:
    path = tmp_path / SOURCE
    path.write_bytes(step(raw))
    return path


def easter(year: int) -> date:
    """Easter Sunday in the Gregorian calendar (the anonymous algorithm, after Meeus)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    g = (b - (b + 8) // 25 + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    weekday = (32 + 2 * e + 2 * i - h - k) % 7
    month, day = divmod(h + weekday - 7 * ((a + 11 * h + 22 * weekday) // 451) + 114, 31)
    return date(year, month, day + 1)


def closing_days(year: int) -> set[date]:
    """The days TARGET, the euro settlement system, is closed on top of weekends, so the ECB
    publishes no reference rate: New Year's Day, Good Friday, Easter Monday, 1 May, 25 and 26
    December."""
    sunday = easter(year)
    return {
        date(year, 1, 1),
        sunday - timedelta(days=2),
        sunday + timedelta(days=1),
        date(year, 5, 1),
        date(year, 12, 25),
        date(year, 12, 26),
    }


def working_days(first: date, last: date) -> tuple[set[date], set[date]]:
    """The days in the period that carry a rate, and the weekdays that are closing days."""
    open_days: set[date] = set()
    gaps: set[date] = set()
    day = first
    while day <= last:
        if day.weekday() < 5:
            (gaps if day in closing_days(day.year) else open_days).add(day)
        day += timedelta(days=1)
    return open_days, gaps


def pinned_period() -> tuple[date, date]:
    query = parse_qs(urlsplit(fetch.load_sources(fetch.SOURCES)[SOURCE]["url"]).query)
    return date.fromisoformat(query["startPeriod"][0]), date.fromisoformat(query["endPeriod"][0])


def test_easter_and_the_closing_days_match_the_calendar() -> None:
    assert [easter(y) for y in (2023, 2024, 2025, 2026)] == [
        date(2023, 4, 9),
        date(2024, 3, 31),
        date(2025, 4, 20),
        date(2026, 4, 5),
    ]
    assert {date(2024, 3, 29), date(2024, 4, 1)} <= closing_days(2024)


def test_each_date_and_currency_lands_once_as_the_publishers_text(tmp_path: Path) -> None:
    path = landed(tmp_path, served(*SERVED_ROWS))
    assert rows(path) == [
        ("2024-03-27", "GBP", "0.85768"),
        ("2024-03-28", "GBP", "0.8551"),
        ("2024-04-02", "GBP", "0.8551"),
        ("2024-03-27", "USD", "1.0816"),
        ("2024-03-28", "USD", "1.0811"),
        ("2024-04-02", "USD", "1.0749"),
    ]
    types = duckdb.read_parquet(str(path)).types
    assert [str(t) for t in types] == ["VARCHAR"] * 3, "the rate stays the publisher's text"


def test_the_closing_days_have_no_row_rather_than_a_zero(tmp_path: Path) -> None:
    landed_rows = rows(landed(tmp_path, served(*SERVED_ROWS)))
    days = {date.fromisoformat(d) for d, _, _ in landed_rows}
    assert days.isdisjoint(closing_days(2024))
    assert all(Decimal(rate) > 0 for _, _, rate in landed_rows)


@pytest.mark.parametrize(
    "line",
    [
        "EXR.D.USD.EUR.SP00.A,D,USD,EUR,SP00,A,2024-03-29,0",
        "EXR.D.USD.EUR.SP00.A,D,USD,EUR,SP00,A,2024-03-29,-1.08",
        "EXR.D.USD.EUR.SP00.A,D,USD,EUR,SP00,A,2024-03-29,",
        "EXR.D.USD.EUR.SP00.A,D,USD,EUR,SP00,A,2024-03-29,NaN",
        "EXR.D.USD.EUR.SP00.A,D,USD,EUR,SP00,A,2024-03-29,1e3",
        "EXR.M.USD.EUR.SP00.A,M,USD,EUR,SP00,A,2024-03,1.0872",
        "EXR.D.USD.GBP.SP00.A,D,USD,GBP,SP00,A,2024-03-28,1.2643",
        "EXR.D.USD.EUR.SP00.A,D,USD,EUR,SP00,A,2024-02-30,1.0811",
        "EXR.D.USD.EUR.SP00.A,D,USD,EUR,SP00,A,2024-03-28,1.0811",
        "EXR.D.USD.EUR.SP00.A,D,USD,EUR,SP00,A,2024-03-28",
    ],
    ids=[
        "zero",
        "negative",
        "blank",
        "not-a-number",
        "exponent",
        "monthly",
        "not-per-euro",
        "no-such-date",
        "duplicate",
        "short-row",
    ],
)
def test_the_step_refuses_a_row_that_is_not_one_positive_daily_euro_rate(line: str) -> None:
    with pytest.raises(RatesError):
        step(served(*SERVED_ROWS, line))


def test_the_step_refuses_a_file_whose_columns_changed_or_that_holds_no_rate() -> None:
    with pytest.raises(RatesError):
        step(served(*SERVED_ROWS, header=HEADER + ",OBS_STATUS"))
    with pytest.raises(RatesError):
        step(served())


def test_the_rates_are_pinned_to_a_closed_period_over_the_covered_years() -> None:
    entry = fetch.load_sources(fetch.SOURCES)[SOURCE]
    assert fetch.STEPS[SOURCE] is step
    assert entry["names_people"] is False
    query = parse_qs(urlsplit(entry["url"]).query)
    assert query["format"] == ["csvdata"] and query["detail"] == ["dataonly"]
    years = yaml.safe_load((REPO / "models/dbt_project.yml").read_text())["vars"]["fiscal_years"]
    first, last = pinned_period()
    assert first == date(years["first"], 1, 1), "the rates start with the first covered year"
    assert first < last < date(years["last"] + 1, 4, 1), "the end is a fixed date"


@pytest.mark.landed
def test_the_landed_rates_cover_every_working_day_and_no_closing_day() -> None:
    """Which days have a rate, by the TARGET calendar; the rates' keys, values and counts are
    their quality suite's (`ingest/quality`)."""
    con = duckdb.connect()
    con.read_parquet(str(landed_file(SOURCE))).create_view("r")
    first, last = pinned_period()
    open_days, gaps = working_days(first, last)
    landed_rows = con.execute("SELECT date, currency, rate FROM r").fetchall()
    for currency in ("GBP", "USD"):
        days = {date.fromisoformat(d) for d, c, _ in landed_rows if c == currency}
        assert days == open_days, f"{currency}: a working day is missing or a closed day landed"
        print(
            f"{currency}: {len(days)} rates from {min(days)} to {max(days)},"
            f" {len(gaps)} weekday closing days absent: {sorted(map(str, gaps))}"
        )
