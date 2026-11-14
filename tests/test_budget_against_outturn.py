"""The Dutch budget against outturn builds and passes its rules on the local engine.

The rules (the current budget is the latest served figure, a moment is served on every line of a
year or on none, two rows are one line only on a repeated figure, every served row and figure is
on a line, the figures the publisher prints tie, names are repaired) are dbt data and unit tests
in `models/`; these tests run them over the committed sample of real landed rows and check each
ran and passed, and check what a passing build cannot show: the sample's lines as the file
serves them, and that a build with nothing landed fails. The landed tests run the rules over the
whole landed file and pin today's counts of the figures the file serves wrong or leaves out.
"""

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import pytest
from conftest import landed_file
from test_fx_translation import environment

from tests.models.dbt_build import dbt

# Only the tests whose every parent is selected: a test of another model that also reads the
# calendar (`dates_in_fiscal_calendar`) would fail on a model this build leaves out.
SELECTION = [
    "--select",
    "+budget_against_outturn+",
    "fiscal_calendar",
    "--indirect-selection",
    "cautious",
]
RULES = [
    "the_current_budget_is_the_latest_served_figure",
    "a_moment_is_served_on_every_line_or_none",
    "every_served_row_of_a_covered_year_is_in_one_line",
    "every_served_figure_is_on_a_line_of_its_article",
    "no_repeated_figure_is_split_across_lines",
    "a_line_of_two_rows_repeats_a_figure",
    "lines_sharing_a_code_without_evidence_are_known",
    "figures_tie_to_the_publishers_reports",
    "names_carry_no_double_encoding",
    "a_zero_served_at_the_latest_moment_is_the_current_budget",
    "a_moment_not_served_is_null_and_the_outturn_stays_beside_the_budget",
    "a_repeated_figure_makes_two_rows_one_line_and_a_differing_one_keeps_them_apart",
    "unique_budget_against_outturn_budget_line_key",
    "not_null_budget_against_outturn_budget_line_key",
    "accepted_values_budget_against_outturn_entity_id__nl_government",
    "accepted_values_budget_against_outturn_currency__EUR",
    "accepted_values_stg_nl_government__budget_moments_kind__V__U__O",
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
    variables = json.dumps({"source_rows": rows})
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


def test_a_moment_not_served_is_null_and_a_served_zero_is_zero(sample: Build) -> None:
    rows = query(
        sample,
        "select budget_line_key, first_supplementary_eur, second_supplementary_eur, outturn_eur,"
        " current_budget_eur, current_budget_moment, has_doubtful_zero from budget_against_outturn"
        " where budget_line_key in"
        " ('nl-government-2026-U-I-1-1', 'nl-government-2024-U-IX-10-1') order by 1",
    )
    assert rows == [
        ("nl-government-2024-U-IX-10-1", 387279000, 0, 0, 0, "StandO2", False),
        ("nl-government-2026-U-I-1-1", None, None, None, 12571000, "StandSBS", False),
    ]


def test_a_zero_between_figures_is_flagged_doubtful(sample: Build) -> None:
    """Curaçao's 2024 first supplementary figure is served as zero between non-zero draft and
    September figures; the King's allowance has none."""
    flags = dict(
        query(
            sample,
            "select budget_line_key, has_doubtful_zero from budget_against_outturn"
            " where budget_line_key in"
            " ('nl-government-2024-U-IIB-7-1', 'nl-government-2024-U-I-1-1')",
        )
    )
    assert flags == {"nl-government-2024-U-IIB-7-1": True, "nl-government-2024-U-I-1-1": False}


def test_rows_that_repeat_no_figure_stay_two_lines(sample: Build) -> None:
    rows = query(
        sample,
        "select budget_line_key, served_rows, draft_budget_eur, september_supplementary_eur"
        " from budget_against_outturn where fiscal_year = 2024 and kind = 'U'"
        " and chapter_code = 'IX' and article_number = '10' order by 1",
    )
    assert rows == [
        ("nl-government-2024-U-IX-10-1", 1, 154748000, 0),
        ("nl-government-2024-U-IX-10-2", 1, 0, 54112000),
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
    variables = json.dumps({"source_rows": "fixtures"})
    result = dbt(environment(tmp_path), tmp_path, "compile", *SELECTION, "--vars", variables)
    assert result.returncode != 0
    assert "'sample' or 'landed', not 'fixtures'" in result.stdout + result.stderr


# Today's counts over the landed file, each a kind of figure the file serves that a reader
# comparing budget with outturn must know of; a re-pin that changes one fails here for a look.
ANOMALIES = {
    # A budget moment served as zero between non-zero figures, or a zero draft before non-zero
    # figures (the publisher prints figures for some of these; the column flags them).
    "lines with a doubtful zero, per year": (
        "select fiscal_year, count(*) from budget_against_outturn where has_doubtful_zero"
        " group by 1 order by 1",
        [(2023, 29), (2024, 78), (2025, 23), (2026, 21)],
    ),
    # A first supplementary figure about three times both the draft and the September figure
    # (the King's 2025 allowance: served 37,266 thousand, published 12,422).
    "lines with a first supplementary figure about three times too high, per year": (
        "select fiscal_year, count(*) from budget_against_outturn"
        " where draft_budget_eur <> 0 and september_supplementary_eur <> 0"
        " and abs(first_supplementary_eur) >= 2.5 * abs(draft_budget_eur)"
        " and abs(first_supplementary_eur) >= 2.5 * abs(september_supplementary_eur)"
        " and abs(first_supplementary_eur)"
        " <= 3.5 * greatest(abs(draft_budget_eur), abs(september_supplementary_eur))"
        " group by 1 order by 1",
        [(2024, 2), (2025, 225)],
    ),
    # A first supplementary figure over a hundred times both its neighbours (2024 chapter XV
    # commitments, articles 3, 8 and 9).
    "lines with a first supplementary figure over a hundred times its neighbours": (
        "select budget_line_key from budget_against_outturn"
        " where draft_budget_eur <> 0 and september_supplementary_eur <> 0"
        " and abs(first_supplementary_eur) >= 100 * abs(draft_budget_eur)"
        " and abs(first_supplementary_eur) >= 100 * abs(september_supplementary_eur)"
        " order by 1",
        [
            ("nl-government-2024-V-XV-3-1",),
            ("nl-government-2024-V-XV-8-1",),
            ("nl-government-2024-V-XV-9-1",),
        ],
    ),
    # One article served under two chapter codes in one year, so a sum over chapters counts it
    # twice (2026 Algemene Zaken under IIB, with the Raad van State's September figure, and III).
    "articles served under two chapter codes": (
        "select fiscal_year, kind, chapter_name, article_number from budget_against_outturn"
        " group by fiscal_year, kind, chapter_name, article_number, article_name"
        " having count(distinct chapter_code) > 1 order by all",
        [
            (2026, "O", "Algemene Zaken", "1"),
            (2026, "O", "Buitenlandse Zaken", "2"),
            (2026, "U", "Algemene Zaken", "1"),
            (2026, "U", "Klimaatfonds", "1"),
        ],
    ),
}


@pytest.fixture(scope="module")
def landed(tmp_path_factory: pytest.TempPathFactory) -> Build:
    landed_file("nl-government-budget")
    return build_budget(tmp_path_factory.mktemp("landed"), "landed")


@pytest.mark.landed
def test_every_rule_holds_over_the_whole_landed_file(landed: Build) -> None:
    """Over the whole landed file: every rule passes, the figures the publisher prints in its
    annual reports among them, and only 2026's first and second supplementary budget and outturn
    are moments the file does not serve."""
    assert {r: landed.results.get(r) for r in RULES} == dict.fromkeys(RULES, "pass")
    counts = ", ".join(f"count({c})" for c in COLUMNS.values())
    not_served = {
        (year, moment)
        for year, *figures in query(
            landed,
            f"select fiscal_year, {counts} from budget_against_outturn group by 1",  # noqa: S608
        )
        for moment, figure in zip(COLUMNS, figures, strict=True)
        if figure == 0
    }
    assert not_served == {(2026, "StandO1"), (2026, "StandO2"), (2026, "Realisatie")}
    lines, served_rows, two_rows = query(
        landed,
        "select count(*), sum(served_rows), count(*) filter (where served_rows = 2)"
        " from budget_against_outturn",
    )[0]
    assert (lines, served_rows, two_rows) == (2239, 2287, 48)


@pytest.mark.landed
@pytest.mark.parametrize("anomaly", ANOMALIES)
def test_each_kind_of_doubtful_figure_is_counted(landed: Build, anomaly: str) -> None:
    sql, today = ANOMALIES[anomaly]
    assert query(landed, sql) == today


@pytest.mark.landed
def test_a_plain_sum_of_the_served_rows_would_count_repeats_twice(landed: Build) -> None:
    """The 2024 cash outturn the served rows add up to, against the model's."""
    path = landed_file("nl-government-budget")
    with duckdb.connect() as db:
        served = db.execute(
            "select sum(cast(amount as hugeint)) from read_parquet(?)"
            " where Begrotingsjaar = '2024' and VUO = 'U' and moment = 'Realisatie'",
            [str(path)],
        ).fetchall()
    model = query(
        landed,
        "select sum(outturn_eur) from budget_against_outturn"
        " where fiscal_year = 2024 and kind = 'U'",
    )
    assert served == [(470517247,)]
    assert model == [(Decimal(394204901000),)]


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
                str(landed_file("nl-government-budget")),
            ],
        ).fetchall()
    assert missing == [(0,)]
