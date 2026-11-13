"""Chicago's contracts and payments land together as one set, so neither table's free text names a
payee the other table hashes, and neither lands alone.

Every person's name is invented; the organisations are real rows of the City's tables.
"""

import csv
import io
from pathlib import Path

import duckdb
import pytest

from ingest import fetch
from ingest.chicago import contracts, payments, together
from ingest.chicago.table import TableError
from ingest.entities import Record, Register
from ingest.individuals import KEY_VARIABLE, person_hasher

ENV = {KEY_VARIABLE: "k" * 32}
NONE = Register(())
# Paid, never contracted: only the payments table names this person.
PAYEE = "JANE Q EXAMPLE"
# Contracted, never paid: only the contracts table names this one.
VENDOR = "JOHN R EXAMPLE"
ORG = {
    "purchase_order_description": "HORSE TRAILERS",
    "purchase_order_contract_number": "C017892",
    "revision_number": "0",
    "vendor_name": "CHICAGO UNITED INDUSTRIES, LIMITED",
    "vendor_id": "22085024V",
    "zip": "60604-3806",
}
PAID = {
    "voucher_number": "PV84248442063",
    "amount": "45111.74",
    "check_date": "01/17/2025",
    "contract_number": "176643",
    "vendor_name": "MOORE LANDSCAPES LLC",
}


def served(columns: tuple[str, ...], *rows: dict[str, str]) -> bytes:
    out = io.StringIO()
    writer = csv.DictWriter(out, columns, quoting=csv.QUOTE_NONNUMERIC)
    writer.writeheader()
    for row in rows:
        writer.writerow({c: row.get(c, "") for c in columns})
    return out.getvalue().encode()


def files(contract_rows: list[dict[str, str]], payment_rows: list[dict[str, str]]) -> dict:
    """The set's two members by URL, payments first: the step tells them apart by their columns."""
    return {
        "https://example.org/payments.csv": served(payments.COLUMNS, *payment_rows),
        "https://example.org/contracts.csv": served(contracts.COLUMNS, *contract_rows),
    }


def rows(data: bytes, tmp_path: Path, name: str) -> list[dict[str, object]]:
    path = tmp_path / name
    path.write_bytes(data)
    rel = duckdb.read_parquet(str(path))
    return [dict(zip(rel.columns, r, strict=True)) for r in rel.fetchall()]


def test_a_payee_hashed_in_payments_blanks_a_contract_description_naming_them(
    tmp_path: Path,
) -> None:
    landed = together.step(
        files(
            [{**ORG, "purchase_order_description": f"REPAIRS FOR {PAYEE}"}, ORG],
            [{**PAID, "vendor_name": PAYEE}, PAID],
        ),
        ENV,
        NONE,
    )
    assert set(landed) == {"chicago-contracts", "chicago-payments"}
    named, plain = rows(landed["chicago-contracts"], tmp_path, "chicago-contracts")
    assert named["purchase_order_description"] is None
    assert named["vendor_name"] == ORG["vendor_name"], "only the description goes"
    assert plain["purchase_order_description"] == "HORSE TRAILERS"
    paid, _ = rows(landed["chicago-payments"], tmp_path, "chicago-payments")
    assert paid["vendor_name"] == person_hasher(ENV)(PAYEE)


def test_a_vendor_hashed_only_in_contracts_is_hashed_the_same_in_payments(tmp_path: Path) -> None:
    landed = together.step(
        files([{**ORG, "vendor_name": VENDOR}], [{**PAID, "vendor_name": VENDOR}]),
        ENV,
        NONE,
    )
    (contract,) = rows(landed["chicago-contracts"], tmp_path, "chicago-contracts")
    (payment,) = rows(landed["chicago-payments"], tmp_path, "chicago-payments")
    assert contract["vendor_name"] == payment["vendor_name"] == person_hasher(ENV)(VENDOR)


def test_a_name_the_register_vouches_for_is_clear_in_both_tables(tmp_path: Path) -> None:
    hospital = "EXAMPLE HEALTH SYSTEM OF CHICAGO"
    register = Register([Record(hospital, "GENERAL", "HSPI")])
    landed = together.step(
        files(
            [{**ORG, "purchase_order_description": f"NURSES FOR {hospital}"}],
            [{**PAID, "vendor_name": hospital}],
        ),
        ENV,
        register,
    )
    (contract,) = rows(landed["chicago-contracts"], tmp_path, "chicago-contracts")
    (payment,) = rows(landed["chicago-payments"], tmp_path, "chicago-payments")
    assert payment["vendor_name"] == hospital
    assert contract["purchase_order_description"] == f"NURSES FOR {hospital}"


def test_the_set_refuses_a_member_it_does_not_know_or_one_missing() -> None:
    with pytest.raises(TableError):
        together.step({"https://example.org/a.csv": b"not,the,table\n"}, ENV, NONE)
    with pytest.raises(TableError):
        together.step({"https://example.org/p.csv": served(payments.COLUMNS, PAID)}, ENV, NONE)
    twice = served(payments.COLUMNS, PAID)
    with pytest.raises(TableError):
        together.step(
            {"https://a.example/p.csv": twice, "https://b.example/p.csv": twice}, ENV, NONE
        )


def test_the_set_refuses_without_the_hash_key() -> None:
    with pytest.raises(Exception, match=KEY_VARIABLE):
        together.step(files([ORG], [PAID]), {}, NONE)


def test_the_fetch_kit_lands_the_two_tables_only_together() -> None:
    assert fetch.SET_STEPS[together.SET] is together.step
    sources = fetch.load_sources(fetch.SOURCES)
    members = [name for name in sources if name.startswith(f"{together.SET}-")]
    assert sorted(members) == ["chicago-contracts", "chicago-payments"]
    assert all(sources[name]["names_people"] for name in members)
    assert not set(members) & set(fetch.STEPS), "neither table lands alone"
