"""The register lands as Parquet for the vendors' countries, with no record and no other name
that may be a person's in it.

The organisations are real records of the register (CC0); every person-like name is invented.
"""

import csv
import io
import os
import re
import zipfile
from collections.abc import Callable
from pathlib import Path

import duckdb
import pytest
from conftest import landed_file

from ingest import fetch
from ingest.fetch import landing_path
from ingest.gleif import legal_entities
from ingest.gleif.legal_entities import (
    COLUMNS,
    COUNTRIES,
    PARTNERSHIPS,
    SOLE_PROPRIETORSHIPS,
    LegalEntitiesError,
    profile,
    step,
)

OTHER_NAME = "Entity.OtherEntityNames.OtherEntityName.1"
TRANSLITERATED = "Entity.TransliteratedOtherEntityNames.TransliteratedOtherEntityName.1"
# A column the register serves that the matching does not need, so it never lands.
CITY = "Entity.LegalAddress.City"
SERVED = (*COLUMNS[:3], CITY, *COLUMNS[3:])


def record(fields: dict[str, str]) -> dict[str, str]:
    return {
        "Entity.EntityCategory": "GENERAL",
        "Entity.EntityStatus": "ACTIVE",
        "Registration.RegistrationStatus": "ISSUED",
        **fields,
    }


APPLE = record(
    {
        "LEI": "HWUPKR0MPOU8FGXBT394",
        "Entity.LegalName": "Apple Inc.",
        "Entity.LegalName.xmllang": "en",
        "Entity.OtherEntityNames.OtherEntityName.1": "Apple Computer, Inc.",
        "Entity.OtherEntityNames.OtherEntityName.1.type": "PREVIOUS_LEGAL_NAME",
        "Entity.LegalAddress.City": "Glendale",
        "Entity.LegalAddress.Country": "US",
        "Entity.LegalJurisdiction": "US-CA",
        "Entity.LegalForm.EntityLegalFormCode": "H1UM",
        "Registration.LastUpdateDate": "2026-03-03T16:34:33.150Z",
    }
)
BARCLAYS = record(
    {
        "LEI": "213800LBQA1Y9L22JB70",
        "Entity.LegalName": "BARCLAYS PLC",
        "Entity.LegalName.xmllang": "en",
        "Entity.OtherEntityNames.OtherEntityName.1": "BARCLAYS BANK PLC",
        "Entity.OtherEntityNames.OtherEntityName.1.type": "PREVIOUS_LEGAL_NAME",
        "Entity.LegalAddress.City": "LONDON",
        "Entity.LegalAddress.Country": "GB",
        "Entity.LegalJurisdiction": "GB",
        "Entity.LegalForm.EntityLegalFormCode": "B6ES",
        "Registration.LastUpdateDate": "2026-10-26T17:28:30.693Z",
    }
)
ING = record(
    {
        "LEI": "549300NYKK9MWM7GGW15",
        "Entity.LegalName": "ING GROEP N.V.",
        "Entity.LegalName.xmllang": "en",
        "Entity.LegalAddress.City": "AMSTERDAM",
        "Entity.LegalAddress.Country": "NL",
        "Entity.LegalJurisdiction": "NL",
        "Entity.LegalForm.EntityLegalFormCode": "B5PM",
        "Registration.LastUpdateDate": "2026-10-28T08:29:48.423Z",
    }
)
SIEMENS = record(
    {
        "LEI": "W38RGI023J3WT1HWRP32",
        "Entity.LegalName": "Siemens Aktiengesellschaft",
        "Entity.LegalAddress.Country": "DE",
        "Entity.LegalJurisdiction": "DE",
        "Entity.LegalForm.EntityLegalFormCode": "6QQB",
    }
)
SOLE_PROPRIETOR = record(
    {
        "LEI": "0000EXAMPLE000000000",
        "Entity.LegalName": "Jane Example",
        "Entity.OtherEntityNames.OtherEntityName.1": "Example Bakery",
        "Entity.OtherEntityNames.OtherEntityName.1.type": "TRADING_OR_OPERATING_NAME",
        "Entity.LegalAddress.Country": "NL",
        "Entity.LegalJurisdiction": "NL",
        "Entity.EntityCategory": "SOLE_PROPRIETOR",
        "Entity.LegalForm.EntityLegalFormCode": "4QXM",
    }
)

PARTNERSHIP = record(
    {
        "LEI": "0000EXAMPLE000000001",
        "Entity.LegalName": "Voorbeeld en Example",
        "Entity.LegalAddress.Country": "NL",
        "Entity.LegalJurisdiction": "NL",
        "Entity.LegalForm.EntityLegalFormCode": "62Y3",
    }
)


@pytest.fixture(autouse=True)
def landing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> Path:
    """The landing path the step works under, empty and apart from the files the tests land; a
    landed test keeps the real one."""
    if request.node.get_closest_marker("landed"):
        return landing_path()
    path = tmp_path / "landing"
    monkeypatch.setenv("LEDGER_LAB_LANDING", str(path))
    return path


def archive(*rows: dict[str, str], columns: tuple[str, ...] = SERVED, members: int = 1) -> bytes:
    text = io.StringIO()
    writer = csv.DictWriter(text, columns, quoting=csv.QUOTE_ALL)
    writer.writeheader()
    for row in rows:
        writer.writerow({c: row.get(c, "") for c in columns})
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for n in range(members):
            z.writestr(f"golden-copy-{n}.csv", text.getvalue())
    return out.getvalue()


def landed(tmp_path: Path, *rows: dict[str, str]) -> Path:
    path = tmp_path / "gleif-legal-entities"
    path.write_bytes(step(archive(*rows)))
    return path


def rows(path: Path) -> list[dict[str, object]]:
    rel = duckdb.read_parquet(str(path))
    return [dict(zip(rel.columns, r, strict=True)) for r in rel.fetchall()]


def test_only_the_vendors_countries_land(tmp_path: Path) -> None:
    landed_rows = rows(landed(tmp_path, APPLE, SIEMENS, BARCLAYS, ING))
    assert [r["LEI"] for r in landed_rows] == [APPLE["LEI"], BARCLAYS["LEI"], ING["LEI"]]
    assert {r["Entity.LegalAddress.Country"] for r in landed_rows} == set(COUNTRIES)


def test_a_record_lands_as_served_in_the_needed_columns_with_blanks_as_nulls(
    tmp_path: Path,
) -> None:
    path = landed(tmp_path, BARCLAYS)
    assert tuple(duckdb.read_parquet(str(path)).columns) == COLUMNS
    (row,) = rows(path)
    assert row["Entity.LegalName"] == "BARCLAYS PLC"
    assert row[OTHER_NAME] == "BARCLAYS BANK PLC"
    assert row["Entity.LegalForm.EntityLegalFormCode"] == "B6ES"
    assert row["Registration.LastUpdateDate"] == "2026-10-26T17:28:30.693Z"
    assert row["Entity.LegalForm.OtherLegalForm"] is None, "a blank lands as null"


def test_a_sole_proprietor_never_lands(tmp_path: Path) -> None:
    path = landed(tmp_path, ING, SOLE_PROPRIETOR)
    assert [r["LEI"] for r in rows(path)] == [ING["LEI"]]
    data = path.read_bytes()
    for clue in (b"Jane", b"Example", SOLE_PROPRIETOR["LEI"].encode()):
        assert clue not in data


def test_every_landed_record_has_an_lei_a_legal_name_a_legal_form_and_a_country(
    tmp_path: Path,
) -> None:
    counts = profile(landed(tmp_path, APPLE, BARCLAYS, ING))
    assert counts["incomplete"] == 0
    assert counts["malformed_leis"] == 0
    assert counts["rows_by_country"] == {"GB": 1, "NL": 1, "US": 1}


def test_the_profile_counts_a_record_without_a_legal_form(tmp_path: Path) -> None:
    formless = {**APPLE, "Entity.LegalForm.EntityLegalFormCode": ""}
    unknown = {**BARCLAYS, "Entity.LegalForm.EntityLegalFormCode": "9999"}
    counts = profile(landed(tmp_path, formless, unknown, ING))
    assert counts["incomplete"] == 1
    assert counts["legal_form_not_available"] == 1


def test_the_step_refuses_a_file_without_a_needed_column() -> None:
    with pytest.raises(LegalEntitiesError):
        step(archive(APPLE, columns=tuple(c for c in SERVED if c != OTHER_NAME)))


@pytest.mark.parametrize("members", [0, 2])
def test_the_step_refuses_an_archive_that_is_not_one_file(members: int) -> None:
    with pytest.raises(LegalEntitiesError):
        step(archive(APPLE, members=members))


def test_the_fetch_kit_lands_a_dated_golden_copy_only_through_this_step() -> None:
    entry = fetch.load_sources(fetch.SOURCES)["gleif-legal-entities"]
    assert fetch.STEPS["gleif-legal-entities"] is step
    assert entry["names_people"] is True
    dated = r"^https://goldencopy\.gleif\.org/storage/golden-copy-files/\d{4}/\d{2}/\d{2}/\d+/"
    assert re.match(dated, entry["url"])


def test_a_partnership_never_lands_and_is_counted_by_form(tmp_path: Path) -> None:
    partners = {**PARTNERSHIP, "LEI": "0000EXAMPLE000000002", "Entity.LegalName": "Doe Partners"}
    limited = {**partners, "LEI": "0000EXAMPLE000000003"}
    limited["Entity.LegalForm.EntityLegalFormCode"] = "CODH"
    shipowners = {**partners, "LEI": "0000EXAMPLE000000004"}
    shipowners["Entity.LegalForm.EntityLegalFormCode"] = "UNJ2"
    general = {**APPLE, "LEI": "0000EXAMPLE000000005", "Entity.LegalName": "Doe And Roe"}
    general["Entity.LegalForm.EntityLegalFormCode"] = "SQ7B"
    path = landed(tmp_path, ING, PARTNERSHIP, partners, limited, shipowners, general)
    assert [r["LEI"] for r in rows(path)] == [ING["LEI"]]
    assert b"Voorbeeld" not in path.read_bytes()
    counts = profile(path)
    left_out = {k: v for k, v in counts["left_out"].items() if v}
    assert left_out == {
        "partnership 62Y3": 2,
        "partnership CODH": 1,
        "partnership UNJ2": 1,
        "partnership SQ7B": 1,
    }
    assert counts["still_to_leave_out"] == {}
    assert {"62Y3", "9AAK", "CODH", "UNJ2", "SQ7B", "QF4W"} <= set(PARTNERSHIPS)
    assert "5WU6" not in PARTNERSHIPS, "a Dutch EEIG is a legal person"


def test_a_sole_proprietorship_never_lands_whatever_its_category(tmp_path: Path) -> None:
    filed_as_general = {**SOLE_PROPRIETOR, "LEI": "0000EXAMPLE000000006"}
    filed_as_general["Entity.EntityCategory"] = "GENERAL"
    maryland = {**APPLE, "LEI": "0000EXAMPLE000000007", "Entity.LegalName": "Jane Example"}
    maryland["Entity.LegalForm.EntityLegalFormCode"] = "XHT1"
    path = landed(tmp_path, ING, SOLE_PROPRIETOR, filed_as_general, maryland)
    assert [r["LEI"] for r in rows(path)] == [ING["LEI"]]
    left_out = {k: v for k, v in profile(path)["left_out"].items() if v}
    assert left_out == {
        "sole proprietor category": 1,
        "sole proprietorship 4QXM": 1,
        "sole proprietorship XHT1": 1,
    }
    assert set(SOLE_PROPRIETORSHIPS) == {"4QXM", "88XU", "AREY", "EQAQ", "XHT1"}


@pytest.mark.parametrize("code", ["8888", "9999", ""])
def test_a_record_without_a_real_legal_form_lands_only_on_its_name_as_evidence(
    tmp_path: Path, code: str
) -> None:
    trader = {**BARCLAYS, "LEI": "0000EXAMPLE000000008", "Entity.LegalName": "Jane Example"}
    company = {**BARCLAYS, "LEI": "0000EXAMPLE000000009", "Entity.LegalName": "Example Works Ltd"}
    for row in (trader, company):
        row["Entity.LegalForm.EntityLegalFormCode"] = code
        row["Entity.LegalForm.OtherLegalForm"] = "Example form" if code == "8888" else ""
    path = landed(tmp_path, trader, company)
    assert [r["LEI"] for r in rows(path)] == [company["LEI"]]
    assert b"Jane" not in path.read_bytes()
    assert profile(path)["left_out"]["no legal form, GB"] == 1


@pytest.mark.parametrize("category", ["", "SOMETHING_NEW"])
def test_a_blank_or_unknown_category_never_lands(tmp_path: Path, category: str) -> None:
    unknown = {**APPLE, "LEI": "0000EXAMPLE000000010", "Entity.EntityCategory": category}
    path = landed(tmp_path, unknown, ING)
    assert [r["LEI"] for r in rows(path)] == [ING["LEI"]]
    assert profile(path)["left_out"]["unknown or blank category"] == 1


def test_an_other_name_that_may_be_a_persons_is_blanked_with_its_language_and_type(
    tmp_path: Path,
) -> None:
    trading = {
        **ING,
        OTHER_NAME: "Jan Voorbeeld",
        f"{OTHER_NAME}.xmllang": "nl",
        f"{OTHER_NAME}.type": "TRADING_OR_OPERATING_NAME",
        TRANSLITERATED: "Jan Voorbeeld",
    }
    path = landed(tmp_path, trading, APPLE)
    ing, apple = rows(path)
    assert [ing[OTHER_NAME], ing[f"{OTHER_NAME}.xmllang"], ing[f"{OTHER_NAME}.type"]] == [None] * 3
    assert ing[TRANSLITERATED] is None
    assert ing["Entity.LegalName"] == "ING GROEP N.V."
    assert apple[OTHER_NAME] == "Apple Computer, Inc."
    assert b"Voorbeeld" not in path.read_bytes()
    assert profile(path)["other_names_blanked"] == 2


def test_the_step_works_in_a_private_folder_under_the_landing_path_and_leaves_nothing(
    tmp_path: Path, landing: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[str, int]] = []
    classify: Callable[[str], bool] = legal_entities.is_organisation

    def watching(name: str) -> bool:
        for entry in landing.iterdir():
            seen.append((entry.name, entry.stat().st_mode & 0o777))
        return classify(name)

    monkeypatch.setattr(legal_entities, "is_organisation", watching)
    landed(tmp_path, APPLE)
    assert seen, "the step classified a name while its work folder existed"
    assert all(name.startswith(".gleif-legal-entities.") for name, _ in seen)
    assert {mode for _, mode in seen} == {0o700}
    assert os.listdir(landing) == []


def test_the_step_refuses_an_archive_that_declares_more_than_the_bound_unzipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert legal_entities.MAX_UNZIPPED > 5_060_480_759, "the pinned file passes"
    served = archive(APPLE, BARCLAYS)
    with zipfile.ZipFile(io.BytesIO(served)) as z:
        size = z.infolist()[0].file_size
    monkeypatch.setattr(legal_entities, "MAX_UNZIPPED", size - 1)
    with pytest.raises(LegalEntitiesError, match="unzipped"):
        step(served)


@pytest.mark.landed
def test_no_landed_record_is_one_the_step_should_have_left_out() -> None:
    """Every leave-out rule applied again to the landed records; their keys, counts and what the
    step left out are the register's quality suite's (`ingest/quality`)."""
    counts = profile(landed_file("gleif-legal-entities"))
    assert counts["still_to_leave_out"] == {}
