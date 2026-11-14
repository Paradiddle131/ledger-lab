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
from ingest.chicago import together
from ingest.chicago.contracts import COLUMNS, TABLE, profile
from ingest.chicago.table import TableError, land_all
from ingest.entities import Register
from ingest.individuals import KEY_VARIABLE

ENV = {KEY_VARIABLE: "k" * 32}
# A register that vouches for no name, so only the classifier decides.
NONE = Register(())


def step(raw: bytes, env: dict[str, str], register: Register) -> bytes:
    """The table landed on its own, as the set's landing scrubs it."""
    return land_all({"t": (raw, TABLE)}, env, register)["t"]


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
    path.write_bytes(step(served(*rows), ENV, NONE))
    return path


def rows(path: Path) -> list[dict[str, object]]:
    rel = duckdb.read_parquet(str(path))
    return [dict(zip(rel.columns, r, strict=True)) for r in rel.fetchall()]


def test_an_organisation_lands_as_served(tmp_path: Path) -> None:
    (row,) = rows(landed(tmp_path, ORG))
    assert row["vendor_name"] == ORG["vendor_name"]
    assert row["zip"] == ORG["zip"]
    assert "address_1" not in row and "address_2" not in row, "street addresses never land"
    assert row["end_date"] is None, "a blank lands as null"


def test_a_person_is_hashed_and_their_row_cut_to_what_cannot_name_them(tmp_path: Path) -> None:
    (row,) = rows(landed(tmp_path, PERSON))
    assert re.fullmatch(r"person:[0-9a-f]{64}", str(row["vendor_name"]))
    assert re.fullmatch(r"id:[0-9a-f]{64}", str(row["vendor_id"]))
    dropped = ("purchase_order_description", "zip", "contract_pdf")
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
    assert org["zip"] == ORG["zip"]


def test_a_vendor_id_stays_in_clear_beside_a_clear_name(tmp_path: Path) -> None:
    person, org = rows(landed(tmp_path, PERSON, {**ORG, "vendor_id": PERSON["vendor_id"]}))
    assert str(person["vendor_id"]).startswith("id:")
    assert (org["vendor_name"], org["vendor_id"]) == (ORG["vendor_name"], PERSON["vendor_id"])


def test_the_step_refuses_without_the_hash_key() -> None:
    with pytest.raises(Exception, match=KEY_VARIABLE):
        step(served(PERSON), {}, NONE)


def test_the_step_refuses_a_table_whose_columns_changed() -> None:
    with pytest.raises(TableError):
        step(served(ORG, columns=(*COLUMNS, "new_column")), ENV, NONE)


def test_the_fetch_kit_lands_the_table_only_in_the_chicago_set() -> None:
    assert "chicago-contracts" not in fetch.STEPS
    assert "chicago-contracts".startswith(f"{together.SET}-")
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
    assert counts["order_number_formats"] == {"A999999": 3}
