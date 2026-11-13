"""The Dutch central government's budget statements land one row per budget moment, as published.

The served lines are real rows of the publisher's file (CC0), with its separator and line ends.
"""

from pathlib import Path

import duckdb
import pytest

from ingest import fetch
from ingest.fetch import landing_path
from ingest.nl_government.budget import (
    COLUMNS,
    MOMENTS,
    UNIT,
    YEARS,
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
    assert {r["source_row"] for r in landed_rows} == {"1"}
    (first, *_) = landed_rows
    assert (first["Begrotingsjaar"], first["VUO"], first["HoofdstukRomeins"]) == ("2024", "U", "I")
    assert first["Artikelnaam"] == "Grondwettelijke uitkering aan de leden van het Koninklijk Huis"


def test_every_column_stays_the_publishers_text(tmp_path: Path) -> None:
    landed_rows = rows(landed(tmp_path, NEGATIVE_2024, TRAILING_SPACE_2023))
    schema = duckdb.read_parquet(str(tmp_path / "nl-government-budget")).types
    assert {str(t) for t in schema} == {"VARCHAR"}
    assert [r["amount"] for r in landed_rows[:2]] == ["-4129465", "-4130301"]
    assert landed_rows[-1]["Artikelnaam"] == (
        "Uitgaven ten behoeve van leden en oud-leden Tweede Kamer "
    )


def test_only_the_chosen_years_land(tmp_path: Path) -> None:
    landed_rows = rows(landed(tmp_path, BEFORE, KING_2023, AFTER))
    assert {r["Begrotingsjaar"] for r in landed_rows} == {"2023"}
    assert {r["source_row"] for r in landed_rows} == {"2"}, "positions count every served row"


def test_the_step_refuses_a_file_whose_columns_changed() -> None:
    with pytest.raises(BudgetError):
        step(served(KING_2024, header=HEADER + ";StandVB"))


def test_the_fetch_kit_lands_the_statements_through_this_step() -> None:
    assert fetch.STEPS["nl-government-budget"] is step
    assert fetch.load_sources(fetch.SOURCES)["nl-government-budget"]["names_people"] is False


def test_the_profile_counts_moments_per_year_units_and_split_articles(tmp_path: Path) -> None:
    counts = profile(landed(tmp_path, KING_2023, KING_2024, KING_2025, *TAXES_2024))
    assert counts["rows"] == 5 * len(MOMENTS)
    assert counts["units"] == [UNIT]
    assert counts["non_integer_amounts"] == 0
    assert counts["articles_on_several_rows"] == 1
    assert counts["moments"]["2024"] == {
        "StandOWB": {"rows": 3, "non_zero": 2},
        "StandO1": {"rows": 3, "non_zero": 2},
        "StandSBS": {"rows": 3, "non_zero": 2},
        "StandO2": {"rows": 3, "non_zero": 3},
        "Realisatie": {"rows": 3, "non_zero": 3},
    }
    assert sorted(counts["moments"]) == ["2023", "2024", "2025"]


@pytest.mark.landed
def test_each_landed_year_has_its_draft_supplementary_and_outturn_moments() -> None:
    counts = profile(landing_path() / "nl-government-budget")
    print(counts)
    assert sorted(counts["moments"]) == list(YEARS)
    for year in YEARS:
        for moment in MOMENTS:
            assert counts["moments"][year][moment]["non_zero"] > 0, (year, moment)
    assert counts["units"] == [UNIT]
    assert counts["non_integer_amounts"] == 0
    assert counts["articles_on_several_rows"] > 0
