"""The group totals sum each entity's spend by span, translated into the reporting currency at
actual and at constant rates, with every figure left out named by its rule.

The rules (a figure counted once or left out once, the group total of a span equal to the sum
of its entity totals translated again from the entity marts, each entity's own-currency total
unchanged, only the rates moving the actual figures) are dbt data and unit tests in `models/`;
the sample build checks each ran and passed. The committed sample of ECB rates covers a
fortnight, no whole period, so over the samples every figure of a covered year is left out for
want of rates. The landed test builds over the real landing, translates every counted figure
again here with exact decimal arithmetic, and pins the totals and what each rule leaves out.
"""

import bisect
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

import duckdb
import httpx
import pytest
from conftest import landed_file

from ingest.fetch import CLIENT_NAME, landing_path
from tests.models.dbt_build import Build, built, query

RULES = [
    "only_the_rates_change_between_two_periods",
    "a_figure_with_a_year_and_no_day_takes_the_years_average_rate",
    "each_rule_leaves_its_figures_out_and_untranslated",
    "a_span_sums_its_counted_figures_only",
    "every_figure_is_in_the_totals_or_left_out_once",
    "the_group_total_of_a_span_is_the_sum_of_its_translated_entity_totals",
    "translation_leaves_each_entitys_own_currency_total_unchanged",
    "unique_group_totals_group_total_key",
    "unique_group_totals_figures_figure_key",
]
CENT = Decimal("0.01")
SIX_PLACES = Decimal("0.000001")
# How many days a closed run carries a rate over, as the rates models carry it.
CARRIED = 4
# A currency's units per euro on a day, and over a span; None where the rates do not reach.
RateOn = Callable[[str, date], Decimal | None]
RateOver = Callable[[str, date, date], Decimal | None]


@pytest.mark.parametrize("rule", RULES)
def test_each_group_totals_rule_ran_and_passed(build: Build, rule: str) -> None:
    assert build.results.get(rule) == "pass"


def test_a_span_the_sample_rates_do_not_cover_is_left_out_whole(build: Build) -> None:
    """The sample's fortnight of rates covers no period or year, so no figure of a covered year
    counts and nothing is half-counted."""
    assert query(build, "select count(*) from group_totals") == [(0,)]
    rules = query(
        build,
        "select distinct coalesce(left_out_by, 'counted') from group_totals_figures"
        " where fiscal_year is not null and coalesce(left_out_by, '') not in"
        " ('repeat', 'no-outturn')",
    )
    assert rules == [("no-rate",)]


def rate_finder(rates: list[tuple[Any, ...]]) -> tuple[RateOn, RateOver]:
    """The units per euro of a currency on a day: the latest publication on or before it, at most
    `CARRIED` days before; None when there is none."""
    by_currency: dict[str, tuple[list[date], list[Decimal]]] = defaultdict(lambda: ([], []))
    for day, currency, units in sorted(rates):
        by_currency[currency][0].append(day)
        by_currency[currency][1].append(units)

    def rate(currency: str, day: date) -> Decimal | None:
        days, units = by_currency[currency]
        at = bisect.bisect_right(days, day) - 1
        if at < 0 or day - days[at] > timedelta(days=CARRIED):
            return None
        return units[at]

    def span(currency: str, start: date, end: date) -> Decimal | None:
        """The mean of the span's publications to six places, when they cover the span."""
        days, units = by_currency[currency]
        low, high = bisect.bisect_left(days, start), bisect.bisect_right(days, end)
        if low == high:
            return None
        if days[low] - start > timedelta(days=CARRIED) or end - days[high - 1] > timedelta(
            days=CARRIED
        ):
            return None
        return (sum(units[low:high], Decimal(0)) / (high - low)).quantize(SIX_PLACES, ROUND_HALF_UP)

    return rate, span


@dataclass
class Counted:
    amount: Decimal = Decimal(0)
    actual: Decimal = Decimal(0)
    constant: Decimal = Decimal(0)
    figures: int = 0


@dataclass
class LeftOut:
    amount: Decimal = Decimal(0)
    figures: int = 0


def translate(amount: Decimal, from_units: Decimal, to_units: Decimal) -> Decimal:
    return (amount * to_units / from_units).quantize(CENT, ROUND_HALF_UP)


@pytest.mark.landed
def test_the_real_landing_builds_the_group_totals(tmp_path: Path) -> None:
    """Over the real landing: every group-totals rule passes, every counted figure translated
    again here to the cent gives the mart's totals exactly, and today's totals and exclusions."""
    landing = landing_path().resolve()
    for name in (
        "chicago-contracts",
        "chicago-payments",
        "gleif-legal-entities",
        "uk-transport-spend-2025",
        "ecb-rates",
        "nl-government-budget",
    ):
        if not (landing / name).exists():
            pytest.skip(f"{name} is not landed")
    build = built(tmp_path, "landed", landing)
    for rule in RULES:
        assert build.results.get(rule) == "pass", rule

    ((reporting, constant_day),) = query(
        build, "select distinct reporting_currency, constant_rates_on from group_totals"
    )
    assert (reporting, constant_day) == ("EUR", date(2023, 12, 29))
    rate, span = rate_finder(
        query(build, "select published_on, currency, units_per_euro from fx_published_rates")
    )
    periods = query(
        build,
        "select entity_id, fiscal_year, fiscal_period, period_start, period_end"
        " from fiscal_periods",
    )
    period_of = {
        (entity, start + timedelta(days=n)): (year, period, start, end)
        for entity, year, period, start, end in periods
        for n in range((end - start).days + 1)
    }
    year_span: dict[tuple[str, int], tuple[date, date]] = {}
    for entity, year, _, start, end in periods:
        first, last = year_span.get((entity, year), (start, end))
        year_span[entity, year] = (min(first, start), max(last, end))

    # Every figure, its span and the rule leaving it out, worked out from the entity marts.
    totals: dict[tuple[str, int, int | None], Counted] = defaultdict(Counted)
    left_out: dict[tuple[str, int | None, str], LeftOut] = defaultdict(LeftOut)
    figures = query(
        build,
        "select entity_id, currency, amount, payment_date, fiscal_year, is_repeat from payments"
        " union all select entity_id, currency, outturn_eur, null, fiscal_year, false"
        " from budget_against_outturn where kind = 'U'",
    )
    for entity, currency, amount, day, year, is_repeat in figures:
        found: tuple[int, int | None, date, date] | None = None
        if day is not None and (entity, day) in period_of:
            found = period_of[entity, day]
        elif day is None and (entity, year) in year_span:
            found = (year, None, *year_span[entity, year])
        if is_repeat:
            rule = "repeat"
        elif found is None:
            rule = "outside-calendars"
        elif amount is None:
            rule = "no-outturn"
        elif span(currency, *found[2:]) is None or span(reporting, *found[2:]) is None:
            rule = "no-rate"
        else:
            rule = None
        if rule is not None or found is None or amount is None:
            kept = left_out[entity, found[0] if found else None, rule or ""]
            kept.amount += amount or 0
            kept.figures += 1
            continue
        if day is None:
            actual = (span(currency, *found[2:]), span(reporting, *found[2:]))
        else:
            actual = (rate(currency, day), rate(reporting, day))
        constant = (rate(currency, constant_day), rate(reporting, constant_day))
        (from_actual, to_actual), (from_constant, to_constant) = actual, constant
        assert from_actual and to_actual and from_constant and to_constant
        total = totals[entity, found[0], found[1]]
        total.amount += amount
        total.actual += translate(amount, from_actual, to_actual)
        total.constant += translate(amount, from_constant, to_constant)
        total.figures += 1

    mart = {
        (entity, year, period): Counted(amount, actual, constant, count)
        for entity, year, period, amount, actual, constant, count in query(
            build,
            "select entity_id, fiscal_year, fiscal_period, amount, amount_at_actual_rates,"
            " amount_at_constant_rates, figures from group_totals",
        )
    }
    assert mart == dict(totals)
    in_model = {
        (entity, year, rule): LeftOut(amount or Decimal(0), count)
        for entity, year, rule, amount, count in query(
            build,
            "select entity_id, fiscal_year, left_out_by, sum(amount), count(*)"
            " from group_totals_figures where left_out_by is not null group by all",
        )
    }
    assert in_model == dict(left_out)

    by_year: dict[tuple[str, int], Counted] = defaultdict(Counted)
    for (entity, year, _), total in totals.items():
        row = by_year[entity, year]
        row.amount += total.amount
        row.actual += total.actual
        row.constant += total.constant
    cents = {
        key: (f"{row.amount:.2f}", f"{row.actual:.2f}", f"{row.constant:.2f}")
        for key, row in by_year.items()
    }
    print("totals by entity and fiscal year (own, actual, constant):", cents)
    print("left out by rule:", dict(left_out))
    assert cents == TOTALS
    assert {key: (f"{kept.amount:.2f}", kept.figures) for key, kept in left_out.items()} == LEFT_OUT


# The ECB's own yearly averages of its daily reference rates, from its data API.
ECB_ANNUAL = (
    "https://data-api.ecb.europa.eu/service/data/EXR/A.USD+GBP.EUR.SP00.A"
    "?startPeriod=2023&endPeriod=2025&format=csvdata"
)


@pytest.mark.landed
@pytest.mark.pages
def test_a_years_mean_rate_is_the_ecbs_own_yearly_average() -> None:
    """The rate a figure with a year and no day takes, the mean of the year's published rates to
    six places, is the ECB's own yearly average to six places, for every full landed year."""
    rates = landed_file("ecb-rates")
    means = {
        (currency, int(year)): Decimal(total) / count
        for currency, year, total, count in duckdb.connect()
        .execute(
            "select currency, left(date, 4), sum(cast(rate as decimal(18, 6))), count(*)"
            " from read_parquet(?) where date < '2026' group by all",
            [str(rates)],
        )
        .fetchall()
    }
    served = httpx.get(ECB_ANNUAL, headers={"User-Agent": CLIENT_NAME}, timeout=60).text
    header, *lines = served.strip().splitlines()
    columns = header.split(",")
    published = {
        (row[columns.index("CURRENCY")], int(row[columns.index("TIME_PERIOD")])): Decimal(
            row[columns.index("OBS_VALUE")]
        )
        for row in (line.split(",") for line in lines)
    }
    assert set(published) == set(means)
    for key, mean in means.items():
        assert mean.quantize(SIX_PLACES, ROUND_HALF_UP) == published[key].quantize(
            SIX_PLACES, ROUND_HALF_UP
        ), key


# Today's totals over the landed files: own currency, then euros at actual and constant rates.
TOTALS: dict[tuple[str, int], tuple[str, str, str]] = {
    ("chicago", 2023): ("9154716980.48", "8451348789.38", "8284811739.43"),
    ("chicago", 2024): ("9724819418.42", "9013623735.88", "8800741558.93"),
    ("chicago", 2025): ("10192385144.67", "9091882327.95", "9223877958.98"),
    ("chicago", 2026): ("8227079104.06", "7090495598.91", "7445320456.51"),
    ("uk-transport", 2025): ("44712766076.80", "51712398841.27", "51450165212.22"),
    ("nl-government", 2023): ("377263672000.00", "377263672000.00", "377263672000.00"),
    ("nl-government", 2024): ("394204901000.00", "394204901000.00", "394204901000.00"),
    ("nl-government", 2025): ("386527257000.00", "386527257000.00", "386527257000.00"),
}
# Today's figures each rule leaves out: own-currency amount and count, by entity and fiscal year.
LEFT_OUT: dict[tuple[str, int | None, str], tuple[str, int]] = {
    ("chicago", 2023, "repeat"): ("698791189.94", 718),
    ("chicago", 2024, "repeat"): ("603509748.08", 491),
    ("chicago", 2025, "repeat"): ("461258942.69", 3153),
    ("chicago", 2026, "repeat"): ("493588753.79", 2671),
    ("chicago", None, "repeat"): ("1742080814.48", 2831),
    ("chicago", None, "outside-calendars"): ("78199667626.68", 156137),
    ("nl-government", 2026, "no-outturn"): ("0.00", 206),
}
