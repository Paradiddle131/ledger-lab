"""The landed sets' quality suites (`python -m ingest.quality <source>`): each suite passes on a
fixture of its source and fails on the same fixture with one planted break of each kind it covers
(the schema, a null key, a value out of range, a known-mess count moved), and its report never
prints a value of a column that can hold a name.

The fixtures are the committed samples of real landed rows (Chicago's contracts and payments, the
UK spend, the ECB rates, the Dutch budget), and for the register and the filings the real records
and rows their own step tests land (`tests/ingest/test_gleif_legal_entities.py`,
`tests/ingest/test_sec_annual_filings.py`), landed here through the source's step. A fixture's
counts are its own (`FIXTURE_COUNTS`), passed as the suite's parameters in place of the pinned
file's. Every planted break is made inside the test, on a copy of the fixture written as
`altered-<kind>`, and is labelled as altered there.

The landed test runs every suite on the landed file under the pinned values, and holds those
values to the counts each source's own `profile` reads, which share no code with the suites.
"""

import json
import os
import subprocess
import sys
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import duckdb
import pytest
from conftest import REPO, landed_file

from ingest import fetch, quality
from ingest.chicago import contracts, payments, together
from ingest.filings.facts import step as filings_step
from ingest.gleif import legal_entities
from ingest.nl_government import budget
from ingest.uk_transport import spend
from tests.ingest.test_gleif_legal_entities import (
    APPLE,
    BARCLAYS,
    ING,
    PARTNERSHIP,
    SOLE_PROPRIETOR,
)
from tests.ingest.test_gleif_legal_entities import archive as register_archive
from tests.ingest.test_sec_annual_filings import LISTED, served

SEEDS = REPO / "models/seeds"
SOURCES = quality.sources()


def from_sample(sample: Path, out: Path, integers: tuple[str, ...] = ()) -> Path:
    """A committed sample of landed rows written as the landed Parquet file: text columns, with
    the columns the landing keeps as integers cast back."""
    with duckdb.connect() as con:
        con.execute(
            "create table t as select * from read_csv(?, all_varchar = true)", [str(sample)]
        )
        for column in integers:
            con.execute(f'alter table t alter "{column}" type bigint')
        con.execute(f"copy t to '{out}' (format parquet)")
    return out


def register(out: Path) -> Path:
    """Real register records (and an invented sole proprietor and partnership, which the step
    leaves out) landed through the register's step."""
    out.write_bytes(
        legal_entities.step(register_archive(APPLE, BARCLAYS, ING, SOLE_PROPRIETOR, PARTNERSHIP))
    )
    return out


def filings(out: Path) -> Path:
    """Real rows of the regulator's archives landed through the filings' step."""
    out.write_bytes(filings_step(served(), LISTED))
    return out


FIXTURES: dict[str, Callable[[Path], Path]] = {
    "chicago-contracts": lambda d: from_sample(
        SEEDS / "staging/chicago/chicago_contracts_sample.csv", d / "chicago-contracts"
    ),
    "chicago-payments": lambda d: from_sample(
        SEEDS / "staging/chicago/chicago_payments_sample.csv", d / "chicago-payments"
    ),
    "ecb-rates": lambda d: from_sample(
        SEEDS / "reference/fx/ecb_rates_sample.csv", d / "ecb-rates"
    ),
    "nl-government-budget": lambda d: from_sample(
        SEEDS / "staging/nl_government/nl_government_budget_sample.csv",
        d / "nl-government-budget",
        ("source_row",),
    ),
    "uk-transport-spend-2025": lambda d: from_sample(
        SEEDS / "staging/uk_transport/uk_transport_spend_sample.csv",
        d / "uk-transport-spend-2025",
        ("source_row",),
    ),
    "gleif-legal-entities": lambda d: register(d / "gleif-legal-entities"),
    "sec-annual-filings": lambda d: filings(d / "sec-annual-filings"),
}

# Each fixture's own counts, in place of the pinned file's: the sample's, or those of what the step
# lands from the step test's rows.
FIXTURE_COUNTS: dict[str, dict[str, Any]] = {
    "chicago-contracts": {
        "rows": 84,
        "contracts": 42,
        "contracts_with_several_rows": 16,
        "most_rows_for_one_contract": 11,
        "order_number_formats": 6,
        "negative_awards": 1,
        "zero_awards": 22,
        "non_numeric_revisions": 4,
        "repeated_revisions": 1,
        "implausible_years": 0,
        "ends_before_starts": 0,
        "rows_without_vendor_id": 84,
        "hashed_rows": 7,
        "hashed_vendors": 5,
    },
    "chicago-payments": {
        "rows": 74,
        "itemised_rows": 60,
        "rolled_up_rows": 14,
        "rolled_up_years": ["2002, 2003, 2004, 2005, 2006, 2007, 2008, 2011, 2023, 2024"],
        "rows_of_2002": 3,
        "without_contract": 45,
        "multiple_vouchers": 3,
        "negative_amounts": 3,
        "zero_amounts": 0,
        "identical_copies": 1,
        "hashed_rows": 15,
        "hashed_payees": 8,
    },
    "ecb-rates": {
        "rows": 64,
        "rates_of_currency": 32,
        "longest_gap_days": 693,
        "weekdays_without_rate": 1112,
    },
    "gleif-legal-entities": {
        "rows": 3,
        "records_of_countries": ["GB 1, NL 1, US 1"],
        "without_real_legal_form": 0,
        "left_out_sole_proprietors": 1,
        "left_out_partnerships": 1,
        "left_out_dutch_firms": 1,
        "left_out_without_legal_form": 0,
        "other_names_blanked": 0,
    },
    "nl-government-budget": {
        "rows": 130,
        "served_rows": 26,
        "articles_on_several_rows": 9,
        "doubly_encoded_name_rows": 10,
        "name_with_edge_space_rows": 5,
        "zero_amounts": 44,
        "negative_amounts": 3,
        "moments_not_served": [
            "2022 StandSBS, 2026 Realisatie, 2026 StandO1, 2026 StandO2, 2027 Realisatie,"
            " 2027 StandO1, 2027 StandO2, 2027 StandSBS"
        ],
    },
    "sec-annual-filings": {
        "rows": 14,
        "filings": 6,
        "companies": 2,
        "amended_filings": 1,
        "restated_periods": 4,
        "reports_without_own_revenue": 1,
        "company_reports": ["1069183 2023 2024, 78814 2023 2024 2025 2025/A"],
        "wrong_lengths": ["0001069183-25-000019"],
    },
    "uk-transport-spend-2025": {
        "rows": 57,
        "files": 12,
        "months": 12,
        "bodies": 7,
        "hidden_rows": 9,
        "hashed_suppliers": 8,
        "redacted_rows": 0,
        "placeholder_references": 16,
        "hashed_references_on_clear_rows": 0,
        "identical_copies": 1,
    },
}


@pytest.fixture(autouse=True)
def work_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    """A landing of the test's own, where the register's step keeps its work files; a landed
    test keeps the real one."""
    if not request.node.get_closest_marker("landed"):
        monkeypatch.setenv("LEDGER_LAB_LANDING", str(tmp_path / "landing"))


def fixture(source: str, tmp_path: Path) -> Path:
    folder = tmp_path / "fixtures"
    folder.mkdir(exist_ok=True)
    return FIXTURES[source](folder)


def failed(lines: list[str]) -> list[str]:
    return [line for line in lines if line.startswith("FAIL")]


def landed_files() -> set[str]:
    """The files the fetch kit lands: each source no set prefixes, each set that lands one file,
    and each member of the Chicago set, which lands its two tables under their own names."""
    sources = fetch.load_sources(fetch.SOURCES)
    alone = {n for n in sources if not any(n.startswith(f"{s}-") for s in fetch.SET_STEPS)}
    return alone | (set(fetch.SET_STEPS) - {together.SET}) | set(together.TABLES)


def test_every_landed_file_has_a_suite_and_a_fixture() -> None:
    assert set(SOURCES) == landed_files()
    assert set(FIXTURES) == set(SOURCES) == set(FIXTURE_COUNTS)


def parameters(suite: quality.Suite) -> set[str]:
    return {
        value["$PARAMETER"]
        for expectation in suite.expectations
        for value in expectation["kwargs"].values()
        if isinstance(value, dict) and "$PARAMETER" in value
    }


@pytest.mark.parametrize("source", SOURCES)
def test_every_parameter_has_its_pinned_value_and_its_fixtures(source: str) -> None:
    the = quality.suite(source)
    assert parameters(the) == set(the.pinned) == set(FIXTURE_COUNTS[source])


# The rules a column that can hold a name may carry: their result is a count, or the column's type.
COUNT_RULES = {
    "expect_column_values_to_not_be_null",
    "expect_column_values_to_match_regex",
    "expect_column_values_to_be_of_type",
    "expect_column_unique_value_count_to_be_between",
}


@pytest.mark.parametrize("source", SOURCES)
def test_a_column_that_can_hold_a_name_carries_only_rules_whose_result_is_a_count(
    source: str,
) -> None:
    """So neither GX's result nor the report can carry a name (a distinct-value set, a minimum or
    a most common value would); the columns named are columns of the batch."""
    the = quality.suite(source)
    (columns,) = [
        x["kwargs"]["column_list"]
        for x in the.expectations
        if x["type"] == "expect_table_columns_to_match_ordered_list"
    ]
    assert the.names <= set(columns)
    on_names = {
        x["type"]
        for x in the.expectations
        if {x["kwargs"].get(k) for k in ("column", "column_A", "column_B")} & the.names
        or set(x["kwargs"].get("column_list", ())) & the.names
        and x["type"] != "expect_table_columns_to_match_ordered_list"
    }
    assert on_names <= COUNT_RULES


# Each suite's rules, counted: a rule taken out, or one added, is a reviewed change here.
RULES = {
    "chicago-contracts": 51,
    "chicago-payments": 33,
    "ecb-rates": 20,
    "gleif-legal-entities": 70,
    "nl-government-budget": 37,
    "sec-annual-filings": 47,
    "uk-transport-spend-2025": 44,
}
# Arguments that let a rule pass on a share of the rows, or on rows a condition picks.
WEAKENING = {"mostly", "row_condition", "condition_parser"}


def test_each_suite_keeps_its_rules_and_none_passes_on_a_share_of_the_rows() -> None:
    assert {s: len(quality.suite(s).expectations) for s in SOURCES} == RULES
    weakened = {
        (s, x["type"], x["kwargs"].get("column"))
        for s in SOURCES
        for x in quality.suite(s).expectations
        if set(x["kwargs"]) & WEAKENING or set(x) - {"type", "kwargs", "notes"}
    }
    assert weakened == set()


def test_a_rule_that_cannot_be_evaluated_fails_the_check(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rule on a column the batch lacks raises inside GX; it is reported as failed, whatever
    GX's own verdict, and the command exits non-zero."""
    the = quality.suite("ecb-rates")
    blind = [
        *the.expectations,
        {"type": "expect_column_values_to_not_be_null", "kwargs": {"column": "served_later"}},
    ]
    monkeypatch.setattr(
        quality, "suite", lambda _: quality.Suite(the.source, blind, the.meta, the.query)
    )
    lines = quality.check("ecb-rates", fixture("ecb-rates", tmp_path), FIXTURE_COUNTS["ecb-rates"])
    assert failed(lines) == [
        "FAIL expect_column_values_to_not_be_null on served_later: could not be evaluated"
    ]
    land(tmp_path, "ecb-rates")
    assert quality.main(["ecb-rates"]) == 1
    assert "could not be evaluated" in capsys.readouterr().out


@pytest.mark.parametrize("source", SOURCES)
def test_each_suite_passes_on_its_fixture(source: str, tmp_path: Path) -> None:
    lines = quality.check(source, fixture(source, tmp_path), FIXTURE_COUNTS[source])
    assert failed(lines) == []
    assert len(lines) == len(quality.suite(source).expectations)


def plant(path: Path, kind: str, statements: list[str]) -> Path:
    """A copy of the fixture, altered by the statements over its rows as the table `t`, written
    as `altered-<kind>` with the fixture's file metadata."""
    out = path.parent / f"altered-{kind}"
    with duckdb.connect() as con:
        con.execute("create table t as select * from read_parquet(?)", [str(path)])
        for statement in statements:
            con.execute(statement)
        stored = con.execute(
            "select decode(key), decode(value) from parquet_kv_metadata(?)", [str(path)]
        ).fetchall()
        kv = ", ".join(f"'{k}': '{v.replace(chr(39), chr(39) * 2)}'" for k, v in stored)
        con.execute(f"copy t to '{out}' (format parquet{f', kv_metadata {{{kv}}}' if kv else ''})")
    return out


def on_first(assignment: str) -> str:
    """The statement setting a column on the fixture's first row."""
    return f"update t set {assignment} where rowid = (select min(rowid) from t)"  # noqa: S608


EXTRA_COLUMN = ["alter table t add column served_later varchar"]
# One planted break of each kind for each suite: the kind, the alteration, and the rule it breaks.
PLANTS: list[tuple[str, str, list[str], str]] = [
    ("chicago-contracts", "schema", EXTRA_COLUMN, "expect_table_columns_to_match_ordered_list"),
    (
        "chicago-contracts",
        "null-key",
        [on_first("purchase_order_contract_number = null")],
        "expect_column_values_to_not_be_null on purchase_order_contract_number",
    ),
    (
        "chicago-contracts",
        "out-of-range",
        [on_first("award_amount = '20000000000'")],
        "expect_column_values_to_be_between on award",
    ),
    (
        "chicago-contracts",
        "mess",
        [
            "update t set award_amount = '-' || award_amount where rowid = (select min(rowid)"
            " from t where award_amount not like '-%' and award_amount <> '0')"
        ],
        "expect_column_sum_to_be_between on is_negative_award",
    ),
    ("chicago-payments", "schema", EXTRA_COLUMN, "expect_table_columns_to_match_ordered_list"),
    (
        "chicago-payments",
        "null-key",
        [on_first("contract_number = null")],
        "expect_column_values_to_not_be_null on contract_number",
    ),
    (
        "chicago-payments",
        "out-of-range",
        [on_first("amount = '20000000000'")],
        "expect_column_values_to_be_between on amount_value",
    ),
    (
        "chicago-payments",
        "mess",
        ["insert into t select * from t where rowid = (select max(rowid) from t)"],
        "expect_column_sum_to_be_between on is_identical_copy",
    ),
    ("ecb-rates", "schema", EXTRA_COLUMN, "expect_table_columns_to_match_ordered_list"),
    (
        "ecb-rates",
        "null-key",
        [on_first("currency = null")],
        "expect_column_values_to_not_be_null on currency",
    ),
    (
        "ecb-rates",
        "out-of-range",
        [on_first("rate = '9.5'")],
        "expect_column_values_to_be_between on rate_value",
    ),
    (
        "ecb-rates",
        "mess",
        ["delete from t where date = '2024-04-03' and currency = 'GBP'"],
        "expect_column_sum_to_be_between on weekdays_without_rate_before",
    ),
    ("gleif-legal-entities", "schema", EXTRA_COLUMN, "expect_table_columns_to_match_ordered_list"),
    (
        "gleif-legal-entities",
        "null-key",
        [on_first('"LEI" = null')],
        "expect_column_values_to_not_be_null on LEI",
    ),
    (
        "gleif-legal-entities",
        "out-of-range",
        [on_first("\"Registration.LastUpdateDate\" = '2026-12-01T00:00:00Z'")],
        "expect_column_values_to_be_between on updated_on",
    ),
    (
        "gleif-legal-entities",
        "mess",
        [on_first("\"Entity.LegalForm.EntityLegalFormCode\" = '8888'")],
        "expect_column_sum_to_be_between on is_without_real_legal_form",
    ),
    ("nl-government-budget", "schema", EXTRA_COLUMN, "expect_table_columns_to_match_ordered_list"),
    (
        "nl-government-budget",
        "null-key",
        [on_first("Artikelnummer = null")],
        "expect_column_values_to_not_be_null on Artikelnummer",
    ),
    (
        "nl-government-budget",
        "out-of-range",
        [on_first("amount = '2000000000'")],
        "expect_column_values_to_be_between on amount_value",
    ),
    (
        "nl-government-budget",
        "mess",
        [
            "update t set Artikelnaam = Artikelnaam || ' ' where rowid = (select min(rowid)"
            " from t where Artikelnaam = trim(Artikelnaam) and Hoofdstuknaam = trim(Hoofdstuknaam))"
        ],
        "expect_column_sum_to_be_between on is_name_with_edge_space",
    ),
    ("sec-annual-filings", "schema", EXTRA_COLUMN, "expect_table_columns_to_match_ordered_list"),
    (
        "sec-annual-filings",
        "null-key",
        [on_first("accession_number = null")],
        "expect_column_values_to_not_be_null on accession_number",
    ),
    (
        "sec-annual-filings",
        "out-of-range",
        [on_first("filing_date = '20250415'")],
        "expect_column_values_to_be_between on filed_in_month",
    ),
    (
        "sec-annual-filings",
        "mess",
        [
            "update t set quarters = '1' where accession_number = '0000078814-25-000015'"
            " and period_end = report_period"
        ],
        "expect_column_most_common_value_to_be_in_set on wrong_lengths",
    ),
    (
        "uk-transport-spend-2025",
        "schema",
        EXTRA_COLUMN,
        "expect_table_columns_to_match_ordered_list",
    ),
    (
        "uk-transport-spend-2025",
        "null-key",
        [on_first("source_file = null")],
        "expect_column_values_to_not_be_null on source_file",
    ),
    (
        "uk-transport-spend-2025",
        "out-of-range",
        [on_first("date = '31/03/2025'")],
        "expect_column_values_to_be_between on paid_on",
    ),
    (
        "uk-transport-spend-2025",
        "mess",
        [
            "insert into t select * replace (source_row + 1000 as source_row) from t"
            " where rowid = (select max(rowid) from t)"
        ],
        "expect_column_sum_to_be_between on is_identical_copy",
    ),
    (
        "uk-transport-spend-2025",
        "mess",
        [
            "update t set transaction_number = '1021398' where rowid ="
            " (select min(rowid) from t where supplier like 'person:%')"
        ],
        "expect_column_values_to_be_between on is_hidden_row_with_clear_reference",
    ),
    (
        "uk-transport-spend-2025",
        "mess",
        [
            "update t set transaction_number = 'id:' || repeat('0', 64) where rowid ="
            " (select min(rowid) from t where supplier not like 'person:%')"
        ],
        "expect_column_values_to_be_between on is_hashed_reference_of_no_hidden_row",
    ),
]


# A day no calendar has, in each date a suite types: it must break a rule, never pass as a null.
IMPOSSIBLE_DAYS: list[tuple[str, str, str]] = [
    (
        "chicago-contracts",
        "approval_date = '2020-02-30T00:00:00.000'",
        "expect_column_values_to_not_be_null on approved_on",
    ),
    (
        "chicago-contracts",
        "end_date = '2020-02-30T00:00:00.000'",
        "expect_column_sum_to_be_between on is_impossible_start_or_end",
    ),
    (
        "chicago-payments",
        "check_date = '02/30/2020'",
        "expect_column_sum_to_be_between on is_itemised_without_day",
    ),
    ("ecb-rates", "date = '2024-02-30'", "expect_column_values_to_not_be_null on day_number"),
    (
        "gleif-legal-entities",
        "\"Registration.LastUpdateDate\" = '2026-02-30T00:00:00Z'",
        "expect_column_values_to_not_be_null on updated_on",
    ),
    (
        "sec-annual-filings",
        "filing_date = '20250230'",
        "expect_column_values_to_not_be_null on filed_on",
    ),
    (
        "sec-annual-filings",
        "period_end = '20240231'",
        "expect_column_sum_to_be_between on is_impossible_period_day",
    ),
    (
        "uk-transport-spend-2025",
        "date = '31/02/2026'",
        "expect_column_values_to_not_be_null on paid_on",
    ),
]


@pytest.mark.parametrize(
    ("source", "assignment", "rule"),
    IMPOSSIBLE_DAYS,
    ids=[f"{s}-{r.rsplit(' ', 1)[1]}" for s, _, r in IMPOSSIBLE_DAYS],
)
def test_a_day_no_calendar_has_breaks_a_rule(
    source: str, assignment: str, rule: str, tmp_path: Path
) -> None:
    altered = plant(fixture(source, tmp_path), "impossible-day", [on_first(assignment)])
    broken = failed(quality.check(source, altered, FIXTURE_COUNTS[source]))
    assert any(line.startswith(f"FAIL {rule}:") for line in broken), broken


def test_every_suite_has_a_planted_break_of_every_kind() -> None:
    assert {(s, k) for s, k, _, _ in PLANTS} == {
        (s, k) for s in SOURCES for k in ("schema", "null-key", "out-of-range", "mess")
    }


@pytest.mark.parametrize(
    ("source", "kind", "statements", "rule"), PLANTS, ids=[f"{s}-{k}" for s, k, _, _ in PLANTS]
)
def test_each_suite_fails_on_its_fixture_with_one_planted_break(
    source: str, kind: str, statements: list[str], rule: str, tmp_path: Path
) -> None:
    altered = plant(fixture(source, tmp_path), kind, statements)
    broken = failed(quality.check(source, altered, FIXTURE_COUNTS[source]))
    assert any(line.startswith(f"FAIL {rule}:") for line in broken), broken


def test_a_file_missing_a_column_the_suite_reads_fails_as_a_schema_break(tmp_path: Path) -> None:
    altered = plant(fixture("ecb-rates", tmp_path), "schema", ["alter table t drop column rate"])
    assert quality.check("ecb-rates", altered, FIXTURE_COUNTS["ecb-rates"]) == [
        "FAIL schema: the suite's query cannot read the file (BinderException)"
    ]


PLANTED_NAME = "Jane Example"  # invented


def test_a_break_in_a_name_column_reports_counts_and_never_the_name(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A payee planted as a malformed hash that carries an invented clear name breaks the hash
    rule; the report, GX's validation result, the command's output and the logs hold the count
    and never the name. GX's own default result format would have listed it."""
    source = "chicago-payments"
    path = fixture(source, tmp_path)
    altered = plant(path, "name", [on_first(f"vendor_name = 'person:{PLANTED_NAME}'")])
    the = quality.suite(source)
    result = quality.validate(the, quality.batch(the, altered), FIXTURE_COUNTS[source])
    lines = quality.report(the, result)
    assert "FAIL expect_column_values_to_match_regex on vendor_name: 1 of 74 rows break it" in lines
    landing = altered.parent / "landing"
    landing.mkdir()
    (landing / source).write_bytes(altered.read_bytes())
    monkeypatch.setenv("LEDGER_LAB_LANDING", str(landing))
    with caplog.at_level("DEBUG"):
        assert quality.main([source]) == 1
    out = capsys.readouterr()
    for text in (
        str(result.describe()),
        json.dumps(result.to_json_dict()),
        *lines,
        out.out,
        out.err,
    ):
        assert PLANTED_NAME not in text
    assert PLANTED_NAME not in caplog.text
    monkeypatch.setattr(quality, "RESULT_FORMAT", {"result_format": "SUMMARY"})
    listed = quality.validate(the, quality.batch(the, altered), FIXTURE_COUNTS[source])
    assert PLANTED_NAME in json.dumps(listed.to_json_dict()), "GX's default would list the name"


def test_a_suite_holding_an_expectation_gx_cannot_build_is_refused(tmp_path: Path) -> None:
    """GX logs and skips an expectation it cannot build; a suite must never lose a rule silently."""
    the = quality.suite("ecb-rates")
    misspelt = [*the.expectations, {"type": "expect_column_values_to_be_positive", "kwargs": {}}]
    broken = quality.Suite(the.source, misspelt, the.meta, the.query)
    frame = quality.batch(broken, fixture("ecb-rates", tmp_path))
    with pytest.raises(quality.QualityError, match="cannot build"):
        quality.validate(broken, frame, FIXTURE_COUNTS["ecb-rates"])


def land(tmp_path: Path, *sources: str) -> Path:
    landing = tmp_path / "landing"
    landing.mkdir(exist_ok=True)
    for source in sources:
        (landing / source).write_bytes(fixture(source, tmp_path).read_bytes())
    return landing


def test_the_command_exits_non_zero_on_a_break_and_names_each_landed_file_of_a_set(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A fixture is not the pinned file, so its counts break the pinned values; a set's name
    checks every landed file it prefixes."""
    land(tmp_path, "chicago-contracts", "chicago-payments")
    assert quality.main(["chicago"]) == 1
    out = capsys.readouterr().out.splitlines()
    assert {line.split(":")[0] for line in out} == {"chicago-contracts", "chicago-payments"}
    assert "chicago-payments: FAIL expect_table_row_count_to_equal: observed 74" in out


def test_the_command_refuses_what_is_not_landed_and_a_landing_that_is_not_local(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    land(tmp_path)
    assert quality.main(["ecb-rates"]) == 1
    assert capsys.readouterr().err == "quality: ecb-rates not landed in the local landing\n"
    assert quality.main(["budget"]) == 1
    assert capsys.readouterr().err == "quality: budget: no suite\n"
    monkeypatch.setenv("LEDGER_LAB_LANDING", "gs://example-lab-landing")
    assert quality.main(["ecb-rates"]) == 1
    err = capsys.readouterr().err
    assert err == "quality: LEDGER_LAB_LANDING is not a local folder; the checks read one\n"


# Runs the command with every way Python opens a connection refused and counted, after `{before}`.
OFFLINE = """
import runpy, socket, sys
attempts = []
def refuse(*args, **kwargs):
    attempts.append(1)
    raise OSError("the test refuses the network")
socket.socket.connect = socket.socket.connect_ex = refuse
socket.create_connection = socket.getaddrinfo = refuse
sys.argv = ["ingest.quality", *sys.argv[1:]]
try:
    {before}
    runpy.run_module("ingest.quality", run_name="__main__", alter_sys=True)
finally:
    print(f"connections attempted: {{len(attempts)}}", file=sys.stderr)
"""
# The HTTP client GX would call out with, asking PyPI for its latest release as GX's version
# check does: the refusal must count it.
CALLING_OUT = (
    "import requests\n    try: requests.get('https://pypi.org/pypi/great_expectations/json')\n"
    "    except OSError: pass"
)


def offline(landing: Path, before: str = "pass") -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GX_")}
    return subprocess.run(
        [sys.executable, "-c", OFFLINE.format(before=before), "ecb-rates"],
        capture_output=True,
        text=True,
        cwd=REPO,
        env=env | {"LEDGER_LAB_LANDING": str(landing)},
        check=False,
    )


def test_the_command_attempts_no_connection(tmp_path: Path) -> None:
    """GX's usage statistics are off and nothing of it that calls out is used: a run, imports
    included, attempts no connection through Python's sockets, while a call through GX's HTTP
    client is counted; DuckDB reads only the local file, with extension installing and loading
    off."""
    landing = land(tmp_path, "ecb-rates")
    run = offline(landing)
    assert run.returncode == 1, run.stderr
    assert "ecb-rates: FAILED, 20 rules" in run.stdout, "the fixture breaks the pinned counts"
    assert run.stderr.splitlines()[-1] == "connections attempted: 0"
    calling = offline(landing, CALLING_OUT)
    assert calling.stderr.splitlines()[-1] != "connections attempted: 0", calling.stderr


def ecb_counts(path: Path) -> dict[str, Any]:
    """The rate counts by plain arithmetic over the landed rows."""
    with duckdb.connect() as con:
        rows = con.execute("select date, currency from read_parquet(?)", [str(path)]).fetchall()
    days: dict[str, list[date]] = {}
    for day, currency in rows:
        days.setdefault(currency, []).append(date.fromisoformat(day))
    per_currency = {len(d) for d in days.values()}
    absent, longest = 0, 0
    for listed in days.values():
        listed.sort()
        for before, after in zip(listed, listed[1:], strict=False):
            between = (before + timedelta(n) for n in range(1, (after - before).days))
            absent += sum(d.weekday() < 5 for d in between)
            longest = max(longest, (after - before).days)
    (rates,) = per_currency
    return {
        "rows": len(rows),
        "rates_of_currency": rates,
        "longest_gap_days": longest,
        "weekdays_without_rate": absent,
    }


def filings_counts(path: Path) -> dict[str, Any]:
    with duckdb.connect() as con:
        rows = con.execute(
            "select cik, accession_number, is_amendment, concept, unit, period_end, quarters, value"
            " from read_parquet(?)",
            [str(path)],
        ).fetchall()
    values: dict[tuple, set[str]] = {}
    for cik, _, _, concept, unit, end, quarters, value in rows:
        values.setdefault((cik, concept, unit, end, quarters), set()).add(value)
    return {
        "rows": len(rows),
        "filings": len({r[1] for r in rows}),
        "companies": len({r[0] for r in rows}),
        "amended_filings": len({r[1] for r in rows if r[2]}),
        "restated_periods": sum(len(v) > 1 for v in values.values()),
    }


def contracts_counts(path: Path) -> dict[str, Any]:
    c = contracts.profile(path)
    same = ("rows", "contracts", "contracts_with_several_rows", "most_rows_for_one_contract")
    same += ("negative_awards", "non_numeric_revisions", "hashed_rows")
    return {k: c[k] for k in same} | {
        "hashed_vendors": c["vendors_hashed"],
        "order_number_formats": len(c["order_number_formats"]),
    }


def payments_counts(path: Path) -> dict[str, Any]:
    p = payments.profile(path)
    same = ("rows", "itemised_rows", "rolled_up_rows", "without_contract", "negative_amounts")
    return {k: p[k] for k in same} | {
        "rolled_up_years": [", ".join(sorted(p["rolled_up_by_year"]))],
        "rows_of_2002": p["rolled_up_by_year"]["2002"],
        "hashed_rows": p["rows_hashed"],
        "hashed_payees": p["payees_hashed"],
    }


def spend_counts(path: Path) -> dict[str, Any]:
    s = spend.profile(path)
    return {
        "rows": s["rows"],
        "files": s["files"],
        "months": len(s["months"]),
        "hashed_suppliers": s["suppliers_hashed"],
        "hidden_rows": s["rows_hashed"] + s["rows_redacted"],
        "redacted_rows": s["rows_redacted"],
    }


def register_counts(path: Path) -> dict[str, Any]:
    r = legal_entities.profile(path)
    left_out = r["left_out"]
    return {
        "rows": sum(r["rows_by_country"].values()),
        "records_of_countries": [
            ", ".join(sorted(f"{k} {v}" for k, v in r["rows_by_country"].items()))
        ],
        "left_out_dutch_firms": left_out["partnership 62Y3"],
        "left_out_sole_proprietors": left_out["sole proprietor category"],
        "left_out_partnerships": sum(n for k, n in left_out.items() if k.startswith("partner")),
        "left_out_without_legal_form": sum(
            n for k, n in left_out.items() if k.startswith("no legal form")
        ),
        "other_names_blanked": r["other_names_blanked"],
    }


def budget_counts(path: Path) -> dict[str, Any]:
    b = budget.profile(path)
    return {"rows": b["rows"], "articles_on_several_rows": b["articles_on_several_rows"]}


# The pinned values each landed file's own profile, or plain arithmetic here, counts too.
COUNTED: dict[str, Callable[[Path], dict[str, Any]]] = {
    "chicago-contracts": contracts_counts,
    "chicago-payments": payments_counts,
    "ecb-rates": ecb_counts,
    "gleif-legal-entities": register_counts,
    "nl-government-budget": budget_counts,
    "sec-annual-filings": filings_counts,
    "uk-transport-spend-2025": spend_counts,
}


@pytest.mark.landed
@pytest.mark.parametrize("source", SOURCES)
def test_each_landed_file_passes_its_suite_at_the_values_its_profile_counts(source: str) -> None:
    path = landed_file(source)
    lines = quality.check(source, path)
    print("\n".join(lines))
    assert failed(lines) == []
    pinned = quality.suite(source).pinned
    counted = COUNTED[source](path)
    assert {k: pinned[k] for k in counted} == counted
