"""The register lands as Parquet for the vendors' countries, with no sole proprietor in it.

The organisations are real records of the register (CC0); the sole proprietor is invented.
"""

import csv
import io
import re
import zipfile
from pathlib import Path

import duckdb
import pytest

from ingest import fetch
from ingest.fetch import landing_path
from ingest.gleif.legal_entities import (
    COLUMNS,
    COUNTRIES,
    LegalEntitiesError,
    profile,
    step,
)

OTHER_NAME = "Entity.OtherEntityNames.OtherEntityName.1"
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
    unknown = {**ING, "Entity.LegalForm.EntityLegalFormCode": "9999"}
    counts = profile(landed(tmp_path, formless, unknown, BARCLAYS))
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


@pytest.mark.landed
def test_the_landed_register_holds_complete_records_for_the_vendors_countries() -> None:
    counts = profile(landing_path() / "gleif-legal-entities")
    print(counts)
    assert counts["incomplete"] == 0
    assert counts["malformed_leis"] == 0
    assert counts["duplicate_leis"] == 0
    assert counts["sole_proprietors"] == 0
    assert set(counts["rows_by_country"]) == set(COUNTRIES)
    assert min(counts["rows_by_country"].values()) > 100_000
