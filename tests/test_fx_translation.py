"""Currency translation through the ECB's euro rates passes its rules on the local engine.

The rules (a weekend takes Friday's rate, a run of closed days the last business day's, a cross
equals its two euro legs, constant currency uses one fixed rate set, exact rounding, a null
amount stays null) are dbt data and unit tests in `models/`; these tests run them over the
committed sample of real rates and check each ran and passed. They also check what a passing
build cannot show: the macro refuses a call it cannot honour, a query stops on an amount no rate
translates, and a build without landed rates fails. The landed test runs the rules over the whole
landed rates file.
"""

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import pytest
from conftest import landed_file

from tests.models.dbt_build import dbt

RULES = [
    "a_weekend_takes_fridays_rate",
    "closed_days_over_easter_take_the_last_business_day",
    "a_cross_rate_equals_its_two_euro_legs",
    "constant_currency_uses_one_fixed_rate_set",
    "a_currency_to_itself_keeps_the_amount",
    "a_null_amount_translates_to_null",
    "translations_round_exactly_to_cents",
    "every_covered_day_has_one_rate_per_currency",
    "published_rates_keep_the_published_figure",
    "a_rate_is_carried_at_most_four_days",
]
SELECTION = ["--select", "ecb_rates_sample", "fx_published_rates+"]


def environment(tmp_path: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k != "LEDGER_LAB_GCP_PROJECT"}
    return env | {
        "LEDGER_LAB_DUCKDB_PATH": str(tmp_path / "ledger_lab.duckdb"),
        "DBT_TARGET_PATH": str(tmp_path / "target"),
        "DBT_LOG_PATH": str(tmp_path / "logs"),
        "DBT_SEND_ANONYMOUS_USAGE_STATS": "false",
    }


def build_fx(tmp_path: Path, rates: str) -> dict[str, str]:
    """Build the rates models and run every rule over the `rates` set; each result by name."""
    env = environment(tmp_path)
    result = dbt(env, tmp_path, "build", *SELECTION, "--vars", json.dumps({"source_rows": rates}))
    assert result.returncode == 0, result.stdout + result.stderr
    run = json.loads((tmp_path / "target" / "run_results.json").read_text())
    return {r["unique_id"].split(".")[-1]: r["status"] for r in run["results"]}


@dataclass(frozen=True)
class Sample:
    results: dict[str, str]
    path: Path


@pytest.fixture(scope="module")
def sample(tmp_path_factory: pytest.TempPathFactory) -> Sample:
    path = tmp_path_factory.mktemp("fx")
    return Sample(build_fx(path, "sample"), path)


@pytest.mark.parametrize("rule", RULES)
def test_each_translation_rule_ran_and_passed(sample: Sample, rule: str) -> None:
    assert sample.results.get(rule) == "pass"


def compiled(sample: Sample, arguments: str) -> str:
    """The macro's select over a relation named `probe`, compiled against the sample build."""
    call = f"translate_currency('probe', 'amount', 'from_currency', 'to_currency', {arguments})"
    result = dbt(
        environment(sample.path),
        sample.path,
        "compile",
        "--inline",
        f"{{{{ {call} }}}}",
        "--vars",
        json.dumps({"source_rows": "sample"}),
        "--output",
        "json",
        "--quiet",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)["compiled"]


@pytest.fixture(scope="module")
def translate(sample: Sample) -> Callable[[str, str, str, str], list[tuple[Any, ...]]]:
    """`translate(day, from, to, amount)` runs the macro over one posting on the sample's rates,
    each argument a SQL literal."""
    query = compiled(sample, "rate_date='posted_on'")

    def run(day: str, from_currency: str, to_currency: str, amount: str) -> list[tuple[Any, ...]]:
        probe = (
            f"with probe as (select cast({day} as date) as posted_on, {from_currency} as"
            f" from_currency, {to_currency} as to_currency, cast({amount} as decimal(28, 6))"
            " as amount) "
        )
        with duckdb.connect(str(sample.path / "ledger_lab.duckdb"), read_only=True) as db:
            return db.execute(probe + query).fetchall()

    return run


def test_a_posting_with_rates_translates(translate: Callable[..., list[tuple[Any, ...]]]) -> None:
    rows = translate("'2024-04-06'", "'USD'", "'GBP'", "542.05")
    assert [row[-2:] for row in rows] == [(Decimal("428.87"), date(2024, 4, 5))]


@pytest.mark.parametrize(
    ("day", "from_currency", "to_currency", "named"),
    [
        ("'2024-04-04'", "'JPY'", "'GBP'", "from JPY to GBP on 2024-04-04"),
        ("'2024-04-04'", "'USD'", "'JPY'", "from USD to JPY on 2024-04-04"),
        ("'2024-04-04'", "'usd'", "'GBP'", "from usd to GBP on 2024-04-04"),
        ("'2024-04-04'", "null", "'GBP'", "from no currency to GBP on 2024-04-04"),
        ("'2024-03-24'", "'USD'", "'GBP'", "from USD to GBP on 2024-03-24"),
        ("'2024-04-09'", "'USD'", "'GBP'", "from USD to GBP on 2024-04-09"),
        ("'2024-04-09'", "'EUR'", "'EUR'", "from EUR to EUR on 2024-04-09"),
        ("null", "'USD'", "'GBP'", "from USD to GBP on no date"),
    ],
    ids=[
        "unknown-from",
        "unknown-to",
        "lower-case",
        "no-currency",
        "before-first",
        "after-last",
        "euro-after-last",
        "no-date",
    ],
)
def test_an_amount_no_rate_translates_stops_the_query(
    translate: Callable[..., list[tuple[Any, ...]]],
    day: str,
    from_currency: str,
    to_currency: str,
    named: str,
) -> None:
    """Never a null a sum would skip, and never a far-away rate: the query fails, naming the row."""
    with pytest.raises(duckdb.Error, match=f"no ECB rate {named}"):
        translate(day, from_currency, to_currency, "100.00")


def test_a_constant_rate_set_the_rates_lack_stops_the_query(sample: Sample) -> None:
    query = compiled(sample, "constant_rates_on='2024-04-09'")
    probe = (
        "with probe as (select cast('2024-04-04' as date) as posted_on, 'USD' as from_currency,"
        " 'GBP' as to_currency, cast(1 as decimal(28, 6)) as amount) "
    )
    with (
        duckdb.connect(str(sample.path / "ledger_lab.duckdb"), read_only=True) as db,
        pytest.raises(duckdb.Error, match="no ECB rate from USD to GBP on 2024-04-09"),
    ):
        db.execute(probe + query).fetchall()


def test_a_build_without_landed_rates_fails_on_the_missing_file(tmp_path: Path) -> None:
    """The default rate set is the landed file: with none landed, the build stops, never falling
    back to the test sample."""
    env = environment(tmp_path) | {"LEDGER_LAB_LANDING": str(tmp_path / "empty")}
    result = dbt(env, tmp_path, "build", "--select", "fx_published_rates")
    assert result.returncode != 0
    assert str(tmp_path / "empty" / "ecb-rates") in result.stdout + result.stderr


@pytest.mark.parametrize(
    ("arguments", "refusal"),
    [
        ("rate_date='posted_on', constant_rates_on='2024-03-27'", "either rate_date or"),
        ("", "either rate_date or"),
        ("constant_rates_on='27/03/2024'", "is an ISO date"),
    ],
    ids=["both", "neither", "not-iso"],
)
def test_the_macro_refuses_a_call_without_exactly_one_rate_date(
    tmp_path: Path, arguments: str, refusal: str
) -> None:
    probe = "(select cast('2024-03-27' as date) as posted_on, 1.00 as amount)"
    call = f"translate_currency({probe!r}, 'amount', \"'USD'\", \"'GBP'\", {arguments})"
    result = dbt(environment(tmp_path), tmp_path, "compile", "--inline", f"{{{{ {call} }}}}")
    assert result.returncode != 0
    assert refusal in result.stdout + result.stderr


@pytest.mark.landed
def test_the_rules_hold_over_every_landed_rate(tmp_path: Path) -> None:
    """Over the whole landed file: every rule passes, and every day from the first rate to the
    last has one rate per currency, carried at most four days."""
    rates = landed_file("ecb-rates")
    results = build_fx(tmp_path, "landed")
    assert {rule: results.get(rule) for rule in RULES} == dict.fromkeys(RULES, "pass")
    with duckdb.connect(str(tmp_path / "ledger_lab.duckdb"), read_only=True) as db:
        days = db.execute(
            "select currency, count(*), min(rate_on), max(rate_on), max(rate_on - published_on),"
            " count(*) filter (where rate_on != published_on) from fx_rates group by currency"
            " order by currency"
        ).fetchall()
        landed = db.execute("select count(*) from read_parquet(?)", [str(rates)]).fetchall()[0][0]
    for currency, count, first, last, carried, filled in days:
        assert count == (last - first) // timedelta(days=1) + 1
        assert carried <= 4
        print(f"{currency}: {count} days, {first} to {last}, {filled} filled, at most {carried}")
    assert [c for c, *_ in days] == ["EUR", "GBP", "USD"]
    print(f"landed rates: {landed} rows")
