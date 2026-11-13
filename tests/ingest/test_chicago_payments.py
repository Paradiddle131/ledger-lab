"""Chicago's payments land with every payee who may be a person hashed, and keep their mess.

The organisations are real rows of the City's table; every person's name and voucher is invented.
"""

import csv
import io
import re
from pathlib import Path

import duckdb
import pytest

from ingest import fetch
from ingest.chicago.payments import COLUMNS, profile, step
from ingest.chicago.table import TableError
from ingest.entities import Register
from ingest.individuals import KEY_VARIABLE

ENV = {KEY_VARIABLE: "k" * 32}
# A register that vouches for no name, so only the classifier decides.
NONE = Register(())
ITEMISED = {
    "voucher_number": "PV84248442063",
    "amount": "45111.74",
    "check_date": "01/17/2025",
    "department_name": "CHICAGO DEPARTMENT OF TRANSPORTATION",
    "contract_number": "176643",
    "vendor_name": "MOORE LANDSCAPES LLC",
}
DIRECT = {
    "voucher_number": "PV54225400030",
    "amount": "3750",
    "check_date": "03/17/2022",
    "contract_number": "DV",
    "vendor_name": "KMW COMMUNITIES LLC",
}
ROLLED_UP_2002 = {
    "amount": "420877.01",
    "check_date": "2002",
    "department_name": "MAYORS OFFICE FOR PEOPLE WITH DISABILITIES",
    "contract_number": "D6952900712",
    "vendor_name": "IAM CARES",
}
ROLLED_UP_2009 = {
    "amount": "17104.23",
    "check_date": "2009",
    "contract_number": "19120",
    "vendor_name": "POLISH/AMERICAN ASSOCIATION",
}
PERSON = {
    "voucher_number": "PVRF000000001",
    "amount": "-70",
    "check_date": "07/03/2023",
    "contract_number": "DV",
    "vendor_name": "JANE Q EXAMPLE",
}


def served(*rows: dict[str, str], columns: tuple[str, ...] = COLUMNS) -> bytes:
    out = io.StringIO()
    writer = csv.DictWriter(out, columns, quoting=csv.QUOTE_NONNUMERIC)
    writer.writeheader()
    for row in rows:
        writer.writerow({c: row.get(c, "") for c in columns})
    return out.getvalue().encode()


def landed(tmp_path: Path, *rows: dict[str, str]) -> Path:
    path = tmp_path / "chicago-payments"
    path.write_bytes(step(served(*rows), ENV, NONE))
    return path


def rows(path: Path) -> list[dict[str, object]]:
    rel = duckdb.read_parquet(str(path))
    return [dict(zip(rel.columns, r, strict=True)) for r in rel.fetchall()]


def test_an_organisation_lands_as_served(tmp_path: Path) -> None:
    (row,) = rows(landed(tmp_path, ITEMISED))
    assert row == ITEMISED


def test_a_blank_lands_as_null(tmp_path: Path) -> None:
    (row,) = rows(landed(tmp_path, ROLLED_UP_2002))
    assert row["voucher_number"] is None
    assert row["check_date"] == "2002"


def test_a_person_leaves_only_as_a_hash(tmp_path: Path) -> None:
    (row,) = rows(landed(tmp_path, PERSON))
    assert re.fullmatch(r"person:[0-9a-f]{64}", str(row["vendor_name"]))
    values = " ".join(str(v) for v in row.values()).upper()
    assert "JANE" not in values
    assert "EXAMPLE" not in values
    kept = ("voucher_number", "amount", "check_date", "contract_number")
    assert [row[c] for c in kept] == [PERSON[c] for c in kept]


def test_an_organisation_without_a_legal_form_is_hashed_as_a_person(tmp_path: Path) -> None:
    (row,) = rows(landed(tmp_path, ROLLED_UP_2009))
    assert str(row["vendor_name"]).startswith("person:")


def test_the_step_refuses_without_the_hash_key() -> None:
    with pytest.raises(Exception, match=KEY_VARIABLE):
        step(served(PERSON), {}, NONE)


def test_the_step_refuses_a_table_whose_columns_changed() -> None:
    with pytest.raises(TableError):
        step(served(ITEMISED, columns=(*COLUMNS, "payee_address")), ENV, NONE)


@pytest.mark.parametrize("column", ["voucher_number", "contract_number"])
@pytest.mark.parametrize("words", ["JANE EXAMPLE", "EXAMPLE", "JANE 2"])
def test_the_step_refuses_a_code_column_that_reads_as_words(column: str, words: str) -> None:
    with pytest.raises(TableError, match=column):
        step(served({**PERSON, column: words}), ENV, NONE)


@pytest.mark.parametrize(
    "voucher", ["Multiple", "PV_092921_1551", "PV39233950173��", "PV81248101215`"]
)
def test_the_placeholder_and_the_publishers_odd_codes_land(tmp_path: Path, voucher: str) -> None:
    (row,) = rows(landed(tmp_path, {**DIRECT, "voucher_number": voucher}))
    assert row["voucher_number"] == voucher


def test_a_contract_number_with_a_dash_lands(tmp_path: Path) -> None:
    (row,) = rows(landed(tmp_path, {**ITEMISED, "contract_number": "B-12345"}))
    assert row["contract_number"] == "B-12345"


def test_the_fetch_kit_lands_the_payments_only_through_this_step() -> None:
    assert fetch.STEPS["chicago-payments"] is step
    assert fetch.load_sources(fetch.SOURCES)["chicago-payments"]["names_people"] is True


def test_the_profile_counts_roll_ups_the_2002_bucket_and_direct_vouchers(tmp_path: Path) -> None:
    counts = profile(landed(tmp_path, ITEMISED, DIRECT, ROLLED_UP_2002, ROLLED_UP_2009, PERSON))
    assert counts == {
        "rows": 5,
        "itemised_rows": 3,
        "rolled_up_rows": 2,
        "rolled_up_by_year": {"2002": 1, "2009": 1},
        "unreadable_dates": 0,
        "earliest_year": 2002,
        "without_contract": 2,
        "without_voucher": 2,
        "negative_amounts": 1,
        "rows_hashed": 3,
        "payees_hashed": 3,
        "hashed_rows_with_voucher": 1,
        "hashed_rows_with_contract": 2,
    }


def test_the_profile_counts_a_date_in_neither_form(tmp_path: Path) -> None:
    counts = profile(landed(tmp_path, ITEMISED, {**DIRECT, "check_date": "2022-03-17"}))
    assert counts["unreadable_dates"] == 1
    assert (counts["itemised_rows"], counts["rolled_up_rows"]) == (1, 0)
