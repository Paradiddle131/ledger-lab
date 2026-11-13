"""The UK Department for Transport's spend modelled into the payments shape: typed in staging,
every landed version of a month kept, and the latest version of each month as payments on the UK's
April to March periods.

The fixture build lands the committed real rows and invented people through the landing step
(`uk_transport_sample.py`); the landed tests build the same models over the real landing.
"""

import calendar
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import duckdb
import httpx
import pytest

from ingest import fetch
from ingest.entities import REGISTER
from ingest.uk_transport.spend import SET
from tests.models.dbt_build import PROJECT, Build, built, query
from tests.models.uk_transport_sample import PEOPLE, sample_rows

CONTENT_API = "https://www.gov.uk/api/content/government/publications/dft-spending-over-25000-"
FILES = PROJECT / "seeds" / "staging" / "uk_transport" / "uk_transport_spend_files.csv"
# The payments shape every entity's intermediate model shares, in order.
SHAPE = [
    "payment_key",
    "entity_id",
    "currency",
    "payment_date",
    "fiscal_year",
    "fiscal_period",
    "amount",
    "vendor_name",
    "vendor_is_hashed",
    "document_number",
    "contract_number",
    "award_after_revisions",
    "source_table",
]


def landed_sum(path: Path) -> tuple[int, Decimal]:
    """The landed set's rows and the sum of its amounts, read with Python's own decimal parsing of
    the publisher's digits, not the models' cast."""
    landed = duckdb.connect().execute("select amount from read_parquet(?)", [str(path)])
    amounts = [str(a) for (a,) in landed.fetchall()]
    return len(amounts), sum((Decimal(a) for a in amounts), Decimal(0))


def columns(build: Build, model: str) -> list[str]:
    return [
        str(c)
        for (c,) in query(
            build,
            "select column_name from information_schema.columns where table_name = ?"
            " order by ordinal_position",
            model,
        )
    ]


def test_the_payments_model_has_the_shared_shape_in_order(build: Build) -> None:
    assert columns(build, "int_uk_transport__payments") == SHAPE


def test_every_landed_row_is_a_payment_and_the_sums_agree(build: Build) -> None:
    landing = Path(build.env["LEDGER_LAB_LANDING"]) / SET
    rows, total = landed_sum(landing)
    assert rows == len(sample_rows())
    for model in ("stg_uk_transport__spend", "int_uk_transport__payment_versions"):
        assert query(build, f"select count(*), sum(amount) from {model}") == [(rows, total)]  # noqa: S608
    assert query(build, "select count(*), sum(amount) from int_uk_transport__payments") == [
        (rows, total)
    ]


def test_an_amount_is_a_decimal_holding_the_publishers_figure(build: Build) -> None:
    types = dict(
        query(
            build,
            "select column_name, data_type from information_schema.columns"
            " where table_name = 'int_uk_transport__payments'",
        )
    )
    assert types["amount"].startswith("DECIMAL")
    assert query(
        build,
        "select amount from int_uk_transport__payments where document_number = '1032887'",
    ) == [(Decimal("18508935.10"),)]


def test_a_payment_falls_in_the_uk_period_of_its_date(build: Build) -> None:
    rows = query(
        build,
        "select payment_date, fiscal_year, fiscal_period from int_uk_transport__payments"
        " where payment_date in ('2025-04-23', '2025-12-15', '2026-03-31')"
        " order by payment_date",
    )
    assert rows == [
        (date(2025, 4, 23), 2025, 1),
        (date(2025, 12, 15), 2025, 9),
        (date(2026, 3, 31), 2025, 12),
    ]


def test_the_payment_key_is_the_entity_file_and_row_and_unique(build: Build) -> None:
    keys = [str(k) for (k,) in query(build, "select payment_key from int_uk_transport__payments")]
    assert len(set(keys)) == len(keys)
    assert "uk-transport:dft-spending-over-25000-april-2025.csv:1" in keys
    shared = query(
        build,
        "select count(*), count(distinct payment_key) from int_uk_transport__payments"
        " where document_number = '#'",
    )
    assert shared[0][0] > 1 and shared[0][0] == shared[0][1]


def test_a_person_stays_hashed_with_a_hashed_document_number(build: Build) -> None:
    rows = query(
        build,
        "select vendor_name, document_number from int_uk_transport__payments"
        " where vendor_is_hashed order by payment_date",
    )
    people = [p for p in PEOPLE if p[3] != "REDACTED"]
    assert len(rows) == len(people)
    assert all(n.startswith("person:") and d.startswith("id:") for n, d in rows)
    assert rows[0] == rows[-1], "one person under one number hashes the same in two files"
    redacted = query(
        build,
        "select vendor_is_hashed, document_number like 'id:%' from int_uk_transport__payments"
        " where vendor_name = 'REDACTED'",
    )
    assert redacted == [(False, True)]


def test_the_revision_unit_tests_and_the_rules_ran_and_passed(build: Build) -> None:
    for name in (
        "a_revised_month_supersedes_its_first_file_and_both_are_kept",
        "payments_carry_only_the_latest_file_of_each_month",
        "every_landed_amount_casts",
        "every_landed_date_casts",
        "each_uk_file_publishes_one_month",
        "no_two_files_of_a_month_share_a_publication_time",
        "a_hashed_uk_supplier_has_a_hashed_document_number",
        "dates_in_fiscal_calendar_int_uk_transport__payments_payment_date",
        "unique_int_uk_transport__payments_payment_key",
        "not_null_int_uk_transport__payment_versions_published_at",
    ):
        assert build.results.get(name) == "pass", name


def pinned_files() -> dict[str, str]:
    """Each pinned UK month's file name, by its source name."""
    return {
        name: entry["url"].rsplit("/", 1)[1]
        for name, entry in fetch.load_sources(fetch.SOURCES).items()
        if name.startswith(f"{SET}-")
    }


def publications() -> dict[str, str]:
    lines = FILES.read_text().splitlines()
    assert lines[0] == "source_file,published_at"
    return dict(line.split(",") for line in lines[1:])


def test_the_publication_seed_lists_exactly_the_pinned_files() -> None:
    assert sorted(publications()) == sorted(pinned_files().values())


@pytest.mark.pages
def test_each_pinned_file_was_published_when_the_seed_says() -> None:
    """The seed's time for each file is its GOV.UK page's first publication, in UTC."""
    seed = publications()
    found = {}
    for name, file in pinned_files().items():
        month = list(calendar.month_abbr).index(name.rsplit("-", 1)[1].title())
        year = int(SET.rsplit("-", 1)[1]) + (month < 4)
        page = httpx.get(
            f"{CONTENT_API}{calendar.month_name[month].lower()}-{year}",
            headers={"User-Agent": fetch.CLIENT_NAME},
            timeout=60,
        ).json()["details"]
        first = next(c for c in page["change_history"] if c["note"] == "First published.")
        stamp = datetime.fromisoformat(first["public_timestamp"]).astimezone(UTC)
        found[file] = stamp.strftime("%Y-%m-%d %H:%M:%S")
    assert found == seed


@pytest.fixture(scope="module")
def real(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Build]:
    """The whole project built over the real landing."""
    landing = fetch.landing_path().resolve()
    for name in (SET, REGISTER, "chicago-contracts", "chicago-payments"):
        if not (landing / name).exists():
            pytest.skip(f"{name} is not landed")
    yield built(tmp_path_factory.mktemp("dbt"), landing)


@pytest.mark.landed
def test_the_real_year_lands_whole_in_payments(real: Build) -> None:
    """Every one of the 31,103 landed rows is a payment: no month has a second file, so none is
    superseded, and the sum ties to the landed file's own digits."""
    rows, total = landed_sum(Path(real.env["LEDGER_LAB_LANDING"]) / SET)
    print("landed rows and sum:", rows, total)
    assert (rows, total) == (31_103, Decimal("44712766076.80"))
    for model in (
        "stg_uk_transport__spend",
        "int_uk_transport__payment_versions",
        "int_uk_transport__payments",
    ):
        assert query(real, f"select count(*), sum(amount) from {model}") == [(rows, total)]  # noqa: S608
    assert query(
        real, "select count(*) from int_uk_transport__payment_versions where not is_current"
    ) == [(0,)]
    assert query(
        real,
        "select count(*), count(distinct payment_key), count(distinct document_number)"
        " from int_uk_transport__payments",
    ) == [(31_103, 31_103, 26_188)]
    assert (
        query(
            real,
            "select count(*), count(distinct vendor_name) from int_uk_transport__payments"
            " where vendor_is_hashed",
        )[0][0]
        == 11_578
    )


@pytest.mark.landed
def test_each_real_month_is_one_uk_period_of_fiscal_2025(real: Build) -> None:
    by_period = query(
        real,
        "select fiscal_year, fiscal_period, count(*), min(payment_date), max(payment_date)"
        " from int_uk_transport__payments group by all order by all",
    )
    print("payments by period:", [(p, n) for _, p, n, _, _ in by_period])
    assert [(y, p) for y, p, *_ in by_period] == [(2025, p) for p in range(1, 13)]
    assert [n for _, _, n, _, _ in by_period] == [
        2753, 2396, 2346, 2586, 2353, 2352, 2715, 2446, 2626, 2426, 2511, 3593
    ]  # fmt: skip
    assert all(lo.month == hi.month for _, _, _, lo, hi in by_period)
