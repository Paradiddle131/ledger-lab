"""The Dutch central government's budget statements land one row per budget moment, as published.

The served lines are real rows of the publisher's file (CC0), with its separator and line ends;
the odd ones (a blank, a literal `NULL`, a leading zero, a misspelt year) are edited real rows.
"""

from pathlib import Path

import duckdb
import pytest
from conftest import landed_file

from ingest import fetch
from ingest.nl_government.budget import (
    COLUMNS,
    MOMENTS,
    UNIT,
    BudgetError,
    profile,
    step,
)

HEADER = ";".join(COLUMNS)
KING_2023 = (
    "2023;U;I;De Koning;1;Grondwettelijke uitkering aan de leden van het Koninklijk Huis;"
    "10985;10985;10985;10985;11369"
)
KING_2024 = (
    "2024;U;I;De Koning;1;Grondwettelijke uitkering aan de leden van het Koninklijk Huis;"
    "11640;11640;11895;11895;12043"
)
KING_2025 = (
    "2025;U;I;De Koning;1;Grondwettelijke uitkering aan de leden van het Koninklijk Huis;"
    "12422;37266;12422;12422;12452"
)
# One article served on two rows, each carrying some of its moments.
TAXES_2024 = (
    "2024;U;IX;Financiën en Nationale Schuld;1;Belastingen;3475108;3602111;0;3993674;3918024",
    "2024;U;IX;Financiën en Nationale Schuld;1;Belastingen;0;0;4053750;3993674;3918024",
)
NEGATIVE_2024 = (
    "2024;V;IX;Financiën en Nationale Schuld;2;Financiële markten;-4129465;-4130301;0;33028;32166"
)
TRAILING_SPACE_2023 = (
    "2023;U;IIA;Staten-Generaal;2;Uitgaven ten behoeve van leden en oud-leden Tweede Kamer ;"
    "34771;36861;36861;36861;38833"
)
BEFORE = "2013;O;A;Infrastructuurfonds;12;Hoofdwegennet;262105;127451;0;173148;150887"
AFTER = (
    "2026;U;I;De Koning;1;Grondwettelijke uitkering aan de leden van het Koninklijk Huis;"
    "12571;0;12571;0;0"
)
ODD_TEXT = "2024;U;I;NULL;01;;11640;11640;11895;11895;012043"


def served(*lines: str, header: str = HEADER) -> bytes:
    return "".join(f"{line}\r\n" for line in (header, *lines)).encode()


def landed(tmp_path: Path, *lines: str) -> Path:
    path = tmp_path / "nl-government-budget"
    path.write_bytes(step(served(*lines)))
    return path


def rows(path: Path) -> list[dict[str, object]]:
    rel = duckdb.read_parquet(str(path))
    return [dict(zip(rel.columns, r, strict=True)) for r in rel.fetchall()]


def test_each_served_row_lands_once_per_moment_with_its_amount_as_published(
    tmp_path: Path,
) -> None:
    landed_rows = rows(landed(tmp_path, KING_2024))
    assert [r["moment"] for r in landed_rows] == list(MOMENTS)
    assert [r["amount"] for r in landed_rows] == ["11640", "11640", "11895", "11895", "12043"]
    assert {r["unit"] for r in landed_rows} == {UNIT}
    assert {r["source_row"] for r in landed_rows} == {1}
    (first, *_) = landed_rows
    assert (first["Begrotingsjaar"], first["VUO"], first["HoofdstukRomeins"]) == ("2024", "U", "I")
    assert first["Artikelnaam"] == "Grondwettelijke uitkering aan de leden van het Koninklijk Huis"


def test_every_column_stays_the_publishers_text(tmp_path: Path) -> None:
    landed_rows = rows(landed(tmp_path, NEGATIVE_2024, TRAILING_SPACE_2023))
    rel = duckdb.read_parquet(str(tmp_path / "nl-government-budget"))
    types = dict(zip(rel.columns, map(str, rel.types), strict=True))
    assert types.pop("source_row") == "BIGINT", "the position is this step's number, not text"
    assert set(types.values()) == {"VARCHAR"}
    assert [r["amount"] for r in landed_rows[:2]] == ["-4129465", "-4130301"]
    assert landed_rows[-1]["Artikelnaam"] == (
        "Uitgaven ten behoeve van leden en oud-leden Tweede Kamer "
    )


def test_a_blank_a_literal_null_and_a_leading_zero_land_as_served(tmp_path: Path) -> None:
    landed_rows = rows(landed(tmp_path, ODD_TEXT))
    (first, *_, outturn) = landed_rows
    assert first["Artikelnaam"] == "", "a blank lands as the empty text it was served as"
    assert first["Hoofdstuknaam"] == "NULL"
    assert first["Artikelnummer"] == "01"
    assert (outturn["moment"], outturn["amount"]) == ("Realisatie", "012043")


def test_every_served_year_lands(tmp_path: Path) -> None:
    landed_rows = rows(landed(tmp_path, BEFORE, KING_2023, AFTER))
    assert [r["Begrotingsjaar"] for r in landed_rows[:: len(MOMENTS)]] == ["2013", "2023", "2026"]
    assert [r["source_row"] for r in landed_rows[:: len(MOMENTS)]] == [1, 2, 3]


@pytest.mark.parametrize("year", ["24", "2024 ", "", "twee"])
def test_the_step_refuses_a_year_that_is_not_four_digits(year: str) -> None:
    with pytest.raises(BudgetError, match="served row 2"):
        step(served(KING_2023, KING_2024.replace("2024", year, 1)))


def test_the_step_refuses_a_file_whose_columns_changed() -> None:
    with pytest.raises(BudgetError):
        step(served(KING_2024, header=HEADER + ";StandVB"))


def test_the_fetch_kit_lands_the_statements_through_this_step() -> None:
    assert fetch.STEPS["nl-government-budget"] is step
    assert fetch.load_sources(fetch.SOURCES)["nl-government-budget"]["names_people"] is False


def test_the_profile_counts_rows_and_split_articles(tmp_path: Path) -> None:
    counts = profile(landed(tmp_path, KING_2023, KING_2024, KING_2025, *TAXES_2024))
    assert counts == {"rows": 5 * len(MOMENTS), "articles_on_several_rows": 1}


@pytest.mark.landed
def test_the_landed_unit_is_the_one_the_publishers_annual_report_prints() -> None:
    """The moments each year serves, the unit and the mess are the landed file's quality suite's
    (`ingest/quality`); this ties the unit to a figure the publisher prints."""
    path = landed_file("nl-government-budget")
    # The publisher's 2024 annual report for chapter I shows this outturn as 12.043 under
    # "bedragen x € 1.000" (table 1 of rijksfinancien.nl/jaarverslag/2024/I).
    king = (
        duckdb.connect()
        .execute(
            "SELECT amount, unit FROM read_parquet($path) WHERE Begrotingsjaar = '2024'"
            " AND VUO = 'U' AND HoofdstukRomeins = 'I' AND Artikelnummer = '1'"
            " AND moment = 'Realisatie'",
            {"path": str(path)},
        )
        .fetchall()
    )
    assert king == [("12043", UNIT)]
