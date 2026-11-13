"""Currency translation through the ECB's euro rates passes its rules on the local engine.

The rules (a weekend takes Friday's rate, a run of closed days the last business day's, a cross
equals its two euro legs, constant currency uses one fixed rate set, exact rounding, null where no
rate applies) are dbt data and unit tests in `models/`; these tests run them over the committed
sample of real rates, check each ran and passed, and check the macro refuses a call it cannot
honour. The landed test runs the same rules over the whole landed rates file.
"""

import json
import os
from datetime import timedelta
from pathlib import Path

import duckdb
import pytest
from test_models import dbt

from ingest.fetch import landing_path

RULES = [
    "a_weekend_takes_fridays_rate",
    "closed_days_over_easter_take_the_last_business_day",
    "a_cross_rate_equals_its_two_euro_legs",
    "constant_currency_uses_one_fixed_rate_set",
    "a_currency_to_itself_keeps_the_amount",
    "untranslatable_amounts_are_null",
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
    result = dbt(env, tmp_path, "build", *SELECTION, "--vars", json.dumps({"ecb_rates": rates}))
    assert result.returncode == 0, result.stdout + result.stderr
    run = json.loads((tmp_path / "target" / "run_results.json").read_text())
    return {r["unique_id"].split(".")[-1]: r["status"] for r in run["results"]}


@pytest.fixture(scope="module")
def sample(tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    return build_fx(tmp_path_factory.mktemp("fx"), "sample")


@pytest.mark.parametrize("rule", RULES)
def test_each_translation_rule_ran_and_passed(sample: dict[str, str], rule: str) -> None:
    assert sample.get(rule) == "pass"


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
    results = build_fx(tmp_path, "landed")
    assert {rule: results.get(rule) for rule in RULES} == dict.fromkeys(RULES, "pass")
    with duckdb.connect(str(tmp_path / "ledger_lab.duckdb"), read_only=True) as db:
        days = db.execute(
            "select currency, count(*), min(rate_on), max(rate_on), max(rate_on - published_on),"
            " count(*) filter (where rate_on != published_on) from fx_rates group by currency"
            " order by currency"
        ).fetchall()
        landed = db.execute(
            "select count(*) from read_parquet(?)", [str(landing_path() / "ecb-rates")]
        ).fetchall()[0][0]
    for currency, count, first, last, carried, filled in days:
        assert count == (last - first) // timedelta(days=1) + 1
        assert carried <= 4
        print(f"{currency}: {count} days, {first} to {last}, {filled} filled, at most {carried}")
    assert [c for c, *_ in days] == ["EUR", "GBP", "USD"]
    print(f"landed rates: {landed} rows")
