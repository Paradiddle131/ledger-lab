"""The group totals sum each entity's spend by span and basis, translated into the reporting
currency at actual and at constant rates, with every figure left out counted by its rule.

The rules (a figure counted once or left out once, the group total of a span equal to the sum
of its entity totals translated again from the entity marts, each entity's own-currency total
unchanged, only the rates moving the actual figures, a span beyond the rates left out whole)
are dbt data and unit tests in `models/`; the sample build checks each ran and passed. The
committed ECB sample holds the whole of March 2026, so over the samples the UK's and Chicago's
March payments count and every other figure of a covered year is left out for want of rates.
Both builds are worked out again here with exact decimal arithmetic from the entity marts and
must equal the mart to the cent; the landed test also pins the totals and what is left out.
"""

import bisect
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

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
    "a_payment_beyond_the_rates_leaves_its_span_out_whole",
    "a_span_sums_its_counted_figures_only",
    "every_figure_is_in_the_totals_or_left_out_once",
    "the_group_total_of_a_span_is_the_sum_of_its_translated_entity_totals",
    "translation_leaves_each_entitys_own_currency_total_unchanged",
    "unique_group_totals_group_total_key",
    "unique_int_group_totals__figures_figure_key",
]
CENT = Decimal("0.01")
SIX_PLACES = Decimal("0.000001")
# How many days a closed run carries a rate over, as the rates models carry it.
CARRIED = 4

Total = tuple[str, int, int | None, str]  # entity, fiscal year, period (None: a year), basis
Gap = tuple[str, int | None, str, str]  # entity, fiscal year (None: no calendar), basis, rule


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


@dataclass
class Rates:
    """The published rates of each currency, in date order."""

    days: dict[str, list[date]] = field(default_factory=lambda: defaultdict(list))
    units: dict[str, list[Decimal]] = field(default_factory=lambda: defaultdict(list))

    def on(self, currency: str, day: date) -> Decimal | None:
        """The latest publication on or before the day, at most `CARRIED` days before."""
        days = self.days[currency]
        at = bisect.bisect_right(days, day) - 1
        if at < 0 or day - days[at] > timedelta(days=CARRIED):
            return None
        return self.units[currency][at]

    def over(self, currency: str, start: date, end: date) -> Decimal | None:
        """The mean of the span's publications to six places, when they reach within `CARRIED`
        days of both its ends."""
        days = self.days[currency]
        low, high = bisect.bisect_left(days, start), bisect.bisect_right(days, end)
        reach = timedelta(days=CARRIED)
        if low == high or days[low] - start > reach or end - days[high - 1] > reach:
            return None
        mean = sum(self.units[currency][low:high], Decimal(0)) / (high - low)
        return mean.quantize(SIX_PLACES, ROUND_HALF_UP)

    def published(self, currency: str, day: date) -> bool:
        days = self.days[currency]
        return bool(days) and days[0] <= day <= days[-1]


def translate(amount: Decimal, from_units: Decimal, to_units: Decimal) -> Decimal:
    return (amount * to_units / from_units).quantize(CENT, ROUND_HALF_UP)


def worked_out(build: Build) -> tuple[dict[Total, Counted], dict[Gap, LeftOut]]:
    """Every figure of the entity marts, its span, basis and rule, and every counted one
    translated here, summed by the mart's grain and by the left-out model's."""
    ((reporting, constant_day),) = query(
        build,
        "select distinct reporting_currency, constant_rates_on from int_group_totals__figures",
    )
    rates = Rates()
    for day, currency, units in sorted(
        query(build, "select published_on, currency, units_per_euro from fx_published_rates")
    ):
        rates.days[currency].append(day)
        rates.units[currency].append(units)
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

    figures = []
    for entity, currency, amount, day, year, is_repeat, contract in query(
        build,
        "select entity_id, currency, amount, payment_date, fiscal_year, is_repeat,"
        " contract_number from payments union all select entity_id, currency, outturn_eur,"
        " null, fiscal_year, false, null from budget_against_outturn where kind = 'U'",
    ):
        if entity == "nl-government":
            basis = "cash-outturn"
        elif entity == "uk-transport":
            basis = "payments-over-25k"
        elif day is None:
            basis = "payments-year-rollup"
        else:
            basis = "contract-payments" if contract else "direct-vouchers"
        found: tuple[int, int | None, date, date] | None = None
        if day is not None and (entity, day) in period_of:
            found = period_of[entity, day]
        elif day is None and (entity, year) in year_span:
            found = (year, None, *year_span[entity, year])
        figures.append((entity, currency, amount, day, is_repeat, basis, found))
    beyond = {
        (entity, found[2])
        for entity, currency, _, day, is_repeat, _, found in figures
        if day is not None
        and found is not None
        and not is_repeat
        and not (rates.published(currency, day) and rates.published(reporting, day))
    }

    totals: dict[Total, Counted] = defaultdict(Counted)
    left_out: dict[Gap, LeftOut] = defaultdict(LeftOut)
    for entity, currency, amount, day, is_repeat, basis, found in figures:
        if is_repeat:
            rule = "repeat"
        elif found is None:
            rule = "outside-calendars"
        elif amount is None:
            rule = "no-outturn"
        elif (
            rates.over(currency, *found[2:]) is None
            or rates.over(reporting, *found[2:]) is None
            or (entity, found[2]) in beyond
        ):
            rule = "no-rate"
        else:
            rule = None
        if rule is not None or found is None or amount is None:
            kept = left_out[entity, found[0] if found else None, basis, rule or ""]
            kept.amount += amount or 0
            kept.figures += 1
            continue
        if day is None:
            from_actual = rates.over(currency, *found[2:])
            to_actual = rates.over(reporting, *found[2:])
        else:
            from_actual, to_actual = rates.on(currency, day), rates.on(reporting, day)
        from_constant = rates.on(currency, constant_day)
        to_constant = rates.on(reporting, constant_day)
        assert from_actual and to_actual and from_constant and to_constant
        total = totals[entity, found[0], found[1], basis]
        total.amount += amount
        total.actual += translate(amount, from_actual, to_actual)
        total.constant += translate(amount, from_constant, to_constant)
        total.figures += 1
    return dict(totals), dict(left_out)


def assert_the_mart_is_worked_out(build: Build) -> tuple[dict[Total, Counted], dict[Gap, LeftOut]]:
    totals, left_out = worked_out(build)
    mart = {
        (entity, year, period, basis): Counted(amount, actual, constant, count)
        for entity, year, period, basis, amount, actual, constant, count in query(
            build,
            "select entity_id, fiscal_year, fiscal_period, basis, amount, amount_at_actual_rates,"
            " amount_at_constant_rates, figures from group_totals",
        )
    }
    assert mart == totals
    gaps = {
        (entity, year, basis, rule): LeftOut(amount or Decimal(0), count)
        for entity, year, basis, rule, amount, count in query(
            build,
            "select entity_id, fiscal_year, basis, left_out_by, amount, figures"
            " from group_totals_left_out",
        )
    }
    assert gaps == left_out
    return totals, left_out


@pytest.mark.parametrize("rule", RULES)
def test_each_group_totals_rule_ran_and_passed(build: Build, rule: str) -> None:
    assert build.results.get(rule) == "pass"


def test_the_sample_mart_is_worked_out_to_the_cent(build: Build) -> None:
    """Over the samples only March 2026 has rates for the whole span: its UK payments and
    Chicago's direct vouchers and contract payments count, to the cent of the arithmetic here,
    and every other figure of a covered year is left out."""
    totals, left_out = assert_the_mart_is_worked_out(build)
    assert sorted(totals) == [
        ("chicago", 2026, 3, "contract-payments"),
        ("chicago", 2026, 3, "direct-vouchers"),
        ("uk-transport", 2025, 12, "payments-over-25k"),
    ]
    assert {rule for (_, year, _, rule) in left_out if year is not None} == {
        "repeat",
        "no-outturn",
        "no-rate",
    }


@pytest.mark.landed
def test_the_real_landing_builds_the_group_totals(tmp_path: Path) -> None:
    """Over the real landing: every group-totals rule passes, every counted figure translated
    again here to the cent gives the mart's totals exactly, and today's totals and gaps."""
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
    totals, left_out = assert_the_mart_is_worked_out(build)

    by_year: dict[tuple[str, int, str], Counted] = defaultdict(Counted)
    for (entity, year, _, basis), total in totals.items():
        row = by_year[entity, year, basis]
        row.amount += total.amount
        row.actual += total.actual
        row.constant += total.constant
        row.figures += total.figures
    cents = {
        key: (row.figures, f"{row.amount:.2f}", f"{row.actual:.2f}", f"{row.constant:.2f}")
        for key, row in by_year.items()
    }
    gaps = {key: (kept.figures, f"{kept.amount:.2f}") for key, kept in left_out.items()}
    print("totals by entity, fiscal year and basis:", cents)
    print("left out:", gaps)
    assert cents == TOTALS
    assert gaps == LEFT_OUT


# The ECB's own yearly averages of its daily reference rates, from its data API.
ECB_ANNUAL = (
    "https://data-api.ecb.europa.eu/service/data/EXR/A.USD+GBP.EUR.SP00.A"
    "?startPeriod=2023&endPeriod=2025&format=csvdata"
)


@pytest.mark.landed
@pytest.mark.pages
def test_a_years_mean_rate_is_the_ecbs_own_yearly_average_to_six_places() -> None:
    """The rate a figure with a year and no day takes, the mean of the year's published rates to
    six places, is the ECB's own unrounded yearly average rounded to six places, for every full
    landed year."""
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


# Today's totals over the landed files by entity, fiscal year and basis: figures, own currency,
# then euros at actual and at constant rates.
TOTALS: dict[tuple[str, int, str], tuple[int, str, str, str]] = {
    ("chicago", 2026, "direct-vouchers"): (
        37067,
        "3317974115.85",
        "2846757085.25",
        "3002691507.70",
    ),
    ("chicago", 2024, "direct-vouchers"): (
        40785,
        "4063960114.91",
        "3783673243.19",
        "3677791963.17",
    ),
    ("chicago", 2025, "direct-vouchers"): (
        45413,
        "4644991092.20",
        "4166273461.00",
        "4203611850.32",
    ),
    ("chicago", 2023, "direct-vouchers"): (
        57188,
        "4152815650.75",
        "3825384358.35",
        "3758204201.58",
    ),
    ("chicago", 2026, "contract-payments"): (
        52717,
        "4677272617.96",
        "4042876561.56",
        "4232825898.65",
    ),
    ("chicago", 2025, "contract-payments"): (
        66448,
        "5380389217.47",
        "4780948452.46",
        "4869130511.49",
    ),
    ("chicago", 2023, "payments-year-rollup"): (
        4308,
        "4716466272.69",
        "4361973082.64",
        "4268295269.02",
    ),
    ("chicago", 2024, "payments-year-rollup"): (
        3787,
        "5449625941.03",
        "5034854618.05",
        "4931788181.71",
    ),
    ("chicago", 2023, "contract-payments"): (62, "5084332.16", "4711997.18", "4601205.57"),
    ("chicago", 2024, "contract-payments"): (59, "12361950.59", "11360576.63", "11187285.63"),
    ("uk-transport", 2025, "payments-over-25k"): (
        31103,
        "44712766076.80",
        "51712398841.27",
        "51450165212.22",
    ),
    ("nl-government", 2023, "cash-outturn"): (
        186,
        "377263672000.00",
        "377263672000.00",
        "377263672000.00",
    ),
    ("nl-government", 2024, "cash-outturn"): (
        186,
        "394204901000.00",
        "394204901000.00",
        "394204901000.00",
    ),
    ("nl-government", 2025, "cash-outturn"): (
        207,
        "386527257000.00",
        "386527257000.00",
        "386527257000.00",
    ),
}
# Today's figures each rule leaves out, by entity, fiscal year, basis and rule: count and
# own-currency amount.
LEFT_OUT: dict[Gap, tuple[int, str]] = {
    ("chicago", None, "direct-vouchers", "outside-calendars"): (51624, "3338248535.74"),
    ("chicago", 2024, "direct-vouchers", "repeat"): (348, "505485344.53"),
    ("chicago", 2025, "direct-vouchers", "repeat"): (156, "299417686.25"),
    ("chicago", 2023, "direct-vouchers", "repeat"): (53, "199235127.84"),
    ("chicago", None, "payments-year-rollup", "outside-calendars"): (102308, "72704949160.72"),
    ("chicago", None, "payments-year-rollup", "repeat"): (4994, "3648809392.76"),
    ("chicago", 2024, "payments-year-rollup", "repeat"): (256, "296895815.44"),
    ("chicago", 2023, "payments-year-rollup", "repeat"): (882, "779906786.98"),
    ("chicago", 2025, "contract-payments", "repeat"): (3527, "328846091.44"),
    ("chicago", 2026, "contract-payments", "repeat"): (3499, "327800778.54"),
    ("chicago", None, "direct-vouchers", "repeat"): (42, "249741351.94"),
    ("chicago", 2026, "direct-vouchers", "repeat"): (86, "397620345.50"),
    ("nl-government", 2026, "cash-outturn", "no-outturn"): (206, "0.00"),
}
