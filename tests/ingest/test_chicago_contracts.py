"""Chicago's contracts land with every vendor who may be a person hashed, and keep their mess.

The organisations are real rows of the City's table; every person's name is invented.
"""

import csv
import io
import re
from pathlib import Path

import duckdb
import pytest

from ingest import fetch
from ingest.chicago.contracts import COLUMNS, TABLE, profile, step
from ingest.chicago.table import TableError, hashed_names_inside
from ingest.fetch import landing_path
from ingest.individuals import KEY_VARIABLE, is_organisation

ENV = {KEY_VARIABLE: "k" * 32}
ORG = {
    "purchase_order_description": "HORSE TRAILERS",
    "purchase_order_contract_number": "C017892",
    "revision_number": "0",
    "specification_number": "C070940021",
    "start_date": "1995-06-01T00:00:00.000",
    "approval_date": "1995-06-08T00:00:00.000",
    "department": "CHICAGO POLICE DEPARTMENT",
    "vendor_name": "CHICAGO UNITED INDUSTRIES, LIMITED",
    "vendor_id": "22085024V",
    "address_1": "53 W JACKSON BLVD # 1450",
    "city": "CHICAGO",
    "state": "IL",
    "zip": "60604-3806",
    "award_amount": "28600",
}
PERSON = {
    "purchase_order_description": "PO 123 EXAMPLE, JANE: consulting",
    "purchase_order_contract_number": "T607891",
    "revision_number": "T1",
    "vendor_name": "JANE Q EXAMPLE",
    "vendor_id": "99999999X",
    "address_1": "1 EXAMPLE ST",
    "address_2": "APT 2",
    "city": "CHICAGO",
    "state": "IL",
    "zip": "60601",
    "award_amount": "-1050",
    "contract_pdf": "https://example.org/contract/T607891.pdf",
}


def served(*rows: dict[str, str], columns: tuple[str, ...] = COLUMNS) -> bytes:
    out = io.StringIO()
    writer = csv.DictWriter(out, columns, quoting=csv.QUOTE_NONNUMERIC)
    writer.writeheader()
    for row in rows:
        writer.writerow({c: row.get(c, "") for c in columns})
    return out.getvalue().encode()


def landed(tmp_path: Path, *rows: dict[str, str]) -> Path:
    path = tmp_path / "chicago-contracts"
    path.write_bytes(step(served(*rows), ENV))
    return path


def rows(path: Path) -> list[dict[str, object]]:
    rel = duckdb.read_parquet(str(path))
    return [dict(zip(rel.columns, r, strict=True)) for r in rel.fetchall()]


def test_an_organisation_lands_as_served(tmp_path: Path) -> None:
    (row,) = rows(landed(tmp_path, ORG))
    assert row["vendor_name"] == ORG["vendor_name"]
    assert row["address_1"] == ORG["address_1"]
    assert row["zip"] == ORG["zip"]
    assert row["end_date"] is None, "a blank lands as null"


def test_a_person_is_hashed_and_their_row_cut_to_what_cannot_name_them(tmp_path: Path) -> None:
    (row,) = rows(landed(tmp_path, PERSON))
    assert re.fullmatch(r"person:[0-9a-f]{64}", str(row["vendor_name"]))
    assert re.fullmatch(r"id:[0-9a-f]{64}", str(row["vendor_id"]))
    dropped = ("purchase_order_description", "address_1", "address_2", "zip", "contract_pdf")
    assert [row[c] for c in dropped] == [None] * len(dropped)
    assert (row["city"], row["state"]) == ("CHICAGO", "IL")
    values = " ".join(str(v) for v in row.values()).upper()
    assert "JANE" not in values
    assert "99999999X" not in values
    assert row["purchase_order_contract_number"] == "T607891"


def test_an_organisation_describing_work_for_a_hashed_vendor_loses_the_description(
    tmp_path: Path,
) -> None:
    for_her = {**ORG, "purchase_order_description": "HORSE TRAILERS FOR JANE Q EXAMPLE"}
    _, org = rows(landed(tmp_path, PERSON, for_her))
    assert org["vendor_name"] == ORG["vendor_name"]
    assert org["purchase_order_description"] is None
    assert org["address_1"] == ORG["address_1"]


def test_the_step_refuses_without_the_hash_key() -> None:
    with pytest.raises(Exception, match=KEY_VARIABLE):
        step(served(PERSON), {})


def test_the_step_refuses_a_table_whose_columns_changed() -> None:
    with pytest.raises(TableError):
        step(served(ORG, columns=(*COLUMNS, "new_column")), ENV)


def test_the_fetch_kit_lands_the_contracts_only_through_this_step() -> None:
    assert fetch.STEPS["chicago-contracts"] is step
    assert fetch.load_sources(fetch.SOURCES)["chicago-contracts"]["names_people"] is True


def test_the_profile_counts_revisions_negative_awards_and_formats(tmp_path: Path) -> None:
    revision = {**ORG, "revision_number": "1", "award_amount": "-500"}
    counts = profile(landed(tmp_path, ORG, revision, PERSON))
    assert counts["rows"] == 3
    assert counts["contracts"] == 2
    assert counts["contracts_with_several_rows"] == 1
    assert counts["most_rows_for_one_contract"] == 2
    assert counts["negative_awards"] == 2
    assert counts["non_numeric_revisions"] == 1
    assert counts["vendors_hashed"] == 1
    assert counts["hashed_rows"] == 1
    assert counts["hashed_rows_with_contract_number"] == 1
    assert counts["hashed_rows_with_specification_number"] == 0
    assert counts["order_number_formats"] == {"A999999": 3}


@pytest.mark.landed
def test_the_landed_contracts_carry_the_known_mess_and_name_no_hashed_vendor() -> None:
    path = landing_path() / "chicago-contracts"
    counts = profile(path)
    print(counts)
    assert counts["contracts_with_several_rows"] > 0
    assert counts["negative_awards"] > 0
    assert counts["non_numeric_revisions"] > 0
    assert len(counts["order_number_formats"]) >= 5
    assert counts["vendors_hashed"] > 0
    vendors = (
        duckdb.connect()
        .execute("SELECT DISTINCT vendor_name FROM read_parquet(?)", [str(path)])
        .fetchall()
    )
    in_clear = [v for (v,) in vendors if v and not str(v).startswith("person:")]
    assert [v for v in in_clear if not is_organisation(str(v))] == []
    inside = hashed_names_inside(path)
    print("cells holding a hashed vendor's name, by column:", inside)
    assert [inside[c] for c in TABLE.free_text] == [0] * len(TABLE.free_text)
