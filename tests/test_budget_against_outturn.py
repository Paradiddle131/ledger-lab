"""The Dutch budget against outturn builds and passes its rules on the local engine.

The rules (the latest reached moment is the current budget, outturn never stands in for it, a
moment is reached on every line of a year or on none, every served row is in one line, names are
repaired) are dbt data and unit tests in `models/`; these tests run them over the committed
sample of real landed rows and check each ran and passed, and check what a passing build cannot
show: the sample's lines as the publisher serves them, and that a build with nothing landed fails.
The landed test builds over the whole landed file and reconciles the model's totals with totals
computed here, in Python, straight from the served rows.
"""

import json
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import pytest
from test_fx_translation import environment
from test_models import dbt

from ingest.fetch import landing_path
from ingest.nl_government.budget import MOMENTS

SELECTION = ["--select", "+budget_against_outturn+", "fiscal_calendar"]
RULES = [
    "the_current_budget_is_the_latest_reached_moment",
    "outturn_never_stands_in_for_the_budget",
    "a_moment_is_reached_on_every_line_or_none",
    "every_served_row_of_a_covered_year_is_in_one_line",
    "names_carry_no_double_encoding",
    "the_latest_reached_moment_is_the_current_budget",
    "outturn_never_replaces_the_budget",
    "an_article_on_two_rows_is_one_line_and_articles_sharing_a_code_stay_apart",
    "unique_budget_against_outturn_budget_line_key",
    "not_null_budget_against_outturn_budget_line_key",
    "accepted_values_budget_against_outturn_entity_id__nl_government",
    "accepted_values_budget_against_outturn_currency__EUR",
    "accepted_values_budget_against_outturn_kind__V__U__O",
    "accepted_values_budget_against_outturn_current_budget_moment__StandOWB__StandO1__StandSBS__StandO2",
    "relationships_budget_against_outturn_fiscal_year__fiscal_year__ref_fiscal_periods_",
    "accepted_values_stg_nl_government__budget_moments_served_unit__EUR_thousand",
]
COLUMNS = {
    "StandOWB": "draft_budget_eur",
    "StandO1": "first_supplementary_eur",
    "StandSBS": "september_supplementary_eur",
    "StandO2": "second_supplementary_eur",
    "Realisatie": "outturn_eur",
}


@dataclass(frozen=True)
class Build:
    results: dict[str, str]
    database: Path


def build_budget(tmp_path: Path, rows: str) -> Build:
    """Build the budget models over the `rows` set and run every rule; each result by name."""
    env = environment(tmp_path)
    variables = json.dumps({"nl_government_budget": rows})
    result = dbt(env, tmp_path, "build", *SELECTION, "--vars", variables)
    assert result.returncode == 0, result.stdout + result.stderr
    run = json.loads((tmp_path / "target" / "run_results.json").read_text())
    names = [r["unique_id"].split(".") for r in run["results"]]
    results = {
        (n[3] if n[0] == "unit_test" else n[2]): r["status"]
        for n, r in zip(names, run["results"], strict=True)
    }
    return Build(results, tmp_path / "ledger_lab.duckdb")


def query(build: Build, sql: str) -> list[tuple[Any, ...]]:
    with duckdb.connect(str(build.database), read_only=True) as db:
        return db.execute(sql).fetchall()


@pytest.fixture(scope="module")
def sample(tmp_path_factory: pytest.TempPathFactory) -> Build:
    return build_budget(tmp_path_factory.mktemp("budget"), "sample")


@pytest.mark.parametrize("rule", RULES)
def test_each_budget_rule_ran_and_passed(sample: Build, rule: str) -> None:
    assert sample.results.get(rule) == "pass"


def test_only_the_years_the_calendar_covers_appear(sample: Build) -> None:
    """The sample holds 2022 and 2027 rows too; the calendar covers 2023 to 2026."""
    years = query(sample, "select distinct fiscal_year from budget_against_outturn order by 1")
    assert [y for (y,) in years] == [2023, 2024, 2025, 2026]


def test_an_article_on_two_rows_is_one_line_with_each_figure_once(sample: Build) -> None:
    rows = query(
        sample,
        "select served_rows, draft_budget_eur, september_supplementary_eur,"
        " second_supplementary_eur, outturn_eur from budget_against_outturn"
        " where budget_line_key = 'nl-government-2024-U-IX-1-1'",
    )
    assert rows == [
        (2, Decimal(3475108000), Decimal(4053750000), Decimal(3993674000), Decimal(3918024000))
    ]


def test_two_funds_sharing_a_chapter_code_stay_two_lines(sample: Build) -> None:
    rows = query(
        sample,
        "select line_in_article, chapter_name, article_name from budget_against_outturn"
        " where fiscal_year = 2025 and kind = 'U' and chapter_code = 'L' order by 1",
    )
    assert rows == [
        (1, "Nationaal Groeifonds", "Kennisontwikkeling"),
        (2, "Klimaatfonds", "Kernenergie"),
    ]


def test_a_moment_not_reached_is_null_and_a_real_zero_is_zero(sample: Build) -> None:
    rows = query(
        sample,
        "select budget_line_key, first_supplementary_eur, second_supplementary_eur, outturn_eur,"
        " current_budget_eur, current_budget_moment from budget_against_outturn"
        " where budget_line_key in"
        " ('nl-government-2026-U-I-1-1', 'nl-government-2024-U-IX-10-1') order by 1",
    )
    assert rows == [
        ("nl-government-2024-U-IX-10-1", 387279000, 0, 0, 0, "StandO2"),
        ("nl-government-2026-U-I-1-1", None, None, None, 12571000, "StandSBS"),
    ]


def test_names_are_repaired_and_trimmed(sample: Build) -> None:
    names = {n for (n,) in query(sample, "select article_name from budget_against_outturn")}
    assert "Kabinet van de Gouverneur van Curaçao" in names
    assert "Slavernijverleden: fonds en herdenkingscomité" in names
    assert "Uitgaven ten behoeve van leden en oud-leden Tweede Kamer" in names


def test_every_amount_is_a_decimal_in_euros(sample: Build) -> None:
    types = dict(
        query(sample, "select column_name, column_type from (describe budget_against_outturn)")
    )
    for column in [*COLUMNS.values(), "current_budget_eur"]:
        assert types[column].startswith("DECIMAL"), column
    king = query(
        sample,
        "select outturn_eur from budget_against_outturn"
        " where budget_line_key = 'nl-government-2024-U-I-1-1'",
    )
    assert king == [(Decimal(12043000),)], "12,043 thousand euros as served"


def test_a_build_without_the_landed_file_fails_on_it(tmp_path: Path) -> None:
    """The default rows are the landed file: with none landed, the build stops, never falling back
    to the test sample."""
    env = environment(tmp_path) | {"LEDGER_LAB_LANDING": str(tmp_path / "empty")}
    result = dbt(env, tmp_path, "build", "--select", "stg_nl_government__budget_moments")
    assert result.returncode != 0
    assert str(tmp_path / "empty" / "nl-government-budget") in result.stdout + result.stderr


def test_the_rows_var_refuses_anything_but_landed_or_sample(tmp_path: Path) -> None:
    variables = json.dumps({"nl_government_budget": "fixtures"})
    result = dbt(environment(tmp_path), tmp_path, "compile", *SELECTION, "--vars", variables)
    assert result.returncode != 0
    assert "'sample' or 'landed', not 'fixtures'" in result.stdout + result.stderr


def repaired(name: str) -> str:
    """A name with UTF-8 read as Latin-1 decoded again, and its edge spaces dropped."""
    if "Ã" in name:
        name = name.encode("latin-1").decode("utf-8")
    return name.strip()


def served_totals(path: Path, years: set[int]) -> tuple[dict[tuple[int, str, str], int], int]:
    """Totals per year, kind and moment, in thousands of euros, computed from the served rows:
    rows of one article (year, kind, chapter code and name, article number) whose non-zero amounts
    never differ at a moment are one article, taken once; others are each their own. Also the
    plain sum's excess over those totals, the double count a sum of served rows would make."""
    with duckdb.connect() as db:
        served = db.execute(
            "select source_row, Begrotingsjaar, VUO, HoofdstukRomeins, Hoofdstuknaam,"
            " Artikelnummer, moment, amount from read_parquet(?)",
            [str(path)],
        ).fetchall()
    articles: dict[tuple[int, str, str, str, str], dict[int, dict[str, int]]] = defaultdict(
        lambda: defaultdict(dict)
    )
    for source_row, year, kind, code, chapter, article, moment, amount in served:
        if int(year) in years:
            key = (int(year), kind, code, repaired(chapter), article)
            articles[key][source_row][moment] = int(amount)
    totals: dict[tuple[int, str, str], int] = defaultdict(int)
    plain: dict[tuple[int, str, str], int] = defaultdict(int)
    for (year, kind, *_), rows in articles.items():
        one = all(len({r[m] for r in rows.values() if r[m] != 0}) <= 1 for m in MOMENTS)
        for moment in MOMENTS:
            amounts = [r[moment] for r in rows.values()]
            plain[year, kind, moment] += sum(amounts)
            if one:
                totals[year, kind, moment] += next((a for a in amounts if a != 0), 0)
            else:
                totals[year, kind, moment] += sum(amounts)
    excess = sum(plain.values()) - sum(totals.values())
    return dict(totals), excess


@pytest.mark.landed
def test_the_model_reconciles_with_the_served_rows_over_the_whole_landed_file(
    tmp_path: Path,
) -> None:
    """Over the whole landed file: every rule passes, each year, kind and moment the model
    carries totals what the served rows total once an article served on two rows is counted
    once, a moment the year has not reached is null throughout, and two figures tie to the
    publisher's annual report."""
    path = landing_path() / "nl-government-budget"
    landed = build_budget(tmp_path, "landed")
    assert {r: landed.results.get(r) for r in RULES} == dict.fromkeys(RULES, "pass")
    years = {y for (y,) in query(landed, "select distinct fiscal_year from budget_against_outturn")}
    assert years == {2023, 2024, 2025, 2026}
    expected, excess = served_totals(path, years)
    sums = ", ".join(f"sum({c}), count({c})" for c in COLUMNS.values())
    model: dict[tuple[int, str, str], Decimal | None] = {}
    for year, kind, *figures in query(
        landed,
        f"select fiscal_year, kind, {sums} from budget_against_outturn group by all",  # noqa: S608
    ):
        for moment, total, count in zip(COLUMNS, figures[::2], figures[1::2], strict=True):
            model[year, kind, moment] = None if count == 0 else total
    reached = {key: value for key, value in model.items() if value is not None}
    not_reached = {key for key, value in model.items() if value is None}
    assert reached == {key: Decimal(expected[key]) * 1000 for key in reached}
    assert all(expected[key] == 0 for key in not_reached)
    assert {(y, m) for y, _, m in not_reached} == {
        (2026, "StandO1"),
        (2026, "StandO2"),
        (2026, "Realisatie"),
    }
    lines, served_rows, two_row = query(
        landed,
        "select count(*), sum(served_rows), count(*) filter (where served_rows = 2)"
        " from budget_against_outturn",
    )[0]
    print(f"lines {lines}, served rows {served_rows}, lines of two rows {two_row}")
    print(f"double count avoided: {excess} thousand euros")
    for key in sorted(reached):
        print(key, f"{reached[key]:,.0f}")
    # The publisher's 2024 annual reports show, under "bedragen x € 1.000", the King's allowance
    # outturn as 12.043 (rijksfinancien.nl/jaarverslag/2024/I, table 1) and, for the taxes
    # article served on two rows, cash expenditure of 3.918.024 against a budget as adopted of
    # 3.475.108 (rijksfinancien.nl/jaarverslag/2024/IX, article 1).
    published = query(
        landed,
        "select budget_line_key, draft_budget_eur, outturn_eur from budget_against_outturn"
        " where budget_line_key in"
        " ('nl-government-2024-U-I-1-1', 'nl-government-2024-U-IX-1-1') order by 1",
    )
    assert published == [
        ("nl-government-2024-U-I-1-1", Decimal(11640000), Decimal(12043000)),
        ("nl-government-2024-U-IX-1-1", Decimal(3475108000), Decimal(3918024000)),
    ]


@pytest.mark.landed
def test_every_sample_row_is_a_landed_row_unchanged() -> None:
    sample = Path(__file__).parents[1] / "models/seeds/staging/nl_government"
    with duckdb.connect() as db:
        missing = db.execute(
            "select count(*) from (select * from read_csv(?, all_varchar = true)"
            " except select cast(source_row as varchar), * exclude (source_row)"
            " from read_parquet(?))",
            [
                str(sample / "nl_government_budget_sample.csv"),
                str(landing_path() / "nl-government-budget"),
            ],
        ).fetchall()
    assert missing == [(0,)]
