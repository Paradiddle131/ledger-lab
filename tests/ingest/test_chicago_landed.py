"""The landed Chicago files: their profiles, a leak check across each file's rows, and a count of
what one file hashes and the other shows in clear.

These need the files landed under the landing path, the legal-entity register among them, and
`LEDGER_LAB_HASH_KEY` set to the key they were landed with (`pytest -m landed`). Every clear vendor
name is an organisation by its legal form or by the register's word, never a person-shaped name.
The leak check finds a cell's words with its own reading of the cell (letters and digits by
`str.isalnum`, plus every stretch of the cell as served from one word's start to a later word's
end) and hashes each run as a person's name, so it shares no tokenising with the step. It cannot
see a hashed name written in another order; the step blanks those, and nothing checks them here.
It asserts on free-text descriptions and code columns, and only counts vendor, department and city
cells, where a clear organisation's name may contain a hashed name by design.
Across the two files it only counts: a name one file hashes may sit in clear in the other's
descriptions, the known remaining gap.
"""

from collections.abc import Callable
from pathlib import Path

import duckdb
import pytest

from ingest.chicago import contracts, payments
from ingest.entities import Register, person_shaped
from ingest.fetch import landing_path
from ingest.individuals import is_organisation, person_hasher

pytestmark = pytest.mark.landed
LONGEST = 8


def landed(name: str) -> Path:
    path = landing_path() / name
    if not path.exists():
        pytest.skip(f"{name} is not landed")
    return path


def cells(path: Path, column: str) -> list[tuple[str, int]]:
    """Every distinct clear value of a column with how many rows carry it."""
    return [
        (str(v), int(n))
        for v, n in duckdb.connect()
        .execute(
            f'SELECT "{column}", count(*) FROM read_parquet(?) WHERE "{column}" IS NOT NULL'  # noqa: S608
            f" AND \"{column}\" NOT LIKE 'person:%' AND \"{column}\" NOT LIKE 'id:%' GROUP BY 1",
            [str(path)],
        )
        .fetchall()
    ]


def hashes(path: Path, column: str) -> set[str]:
    return {
        str(v)
        for (v,) in duckdb.connect()
        .execute(
            f'SELECT DISTINCT "{column}" FROM read_parquet(?) WHERE "{column}" LIKE \'person:%\'',  # noqa: S608
            [str(path)],
        )
        .fetchall()
    }


def runs(cell: str) -> set[str]:
    """Each run of up to LONGEST words of a cell, read two ways: the words' letters and digits
    joined by spaces, and the cell as served from one word's start to a later word's end."""
    spans: list[tuple[int, int]] = []
    start = None
    for i, char in enumerate([*cell, " "]):
        if char.isalnum() and start is None:
            start = i
        elif not char.isalnum() and start is not None:
            spans.append((start, i))
            start = None
    found = set()
    for i in range(len(spans)):
        for j in range(i, min(i + LONGEST, len(spans))):
            found.add(" ".join(cell[a:b] for a, b in spans[i : j + 1]))
            found.add(cell[spans[i][0] : spans[j][1]])
    return {run for run in found if any(c.isalpha() for c in run)}


def holding(
    column_cells: list[tuple[str, int]], hashed: set[str], as_person: Callable[[str], str]
) -> int:
    """How many rows' cells hold a run whose person hash is one of `hashed`."""
    return sum(n for cell, n in column_cells if {as_person(r) for r in runs(cell)} & hashed)


def clear_names_are_organisations(path: Path, column: str) -> None:
    """Every clear name is an organisation by its legal form or by the landed register's word, and
    the register's word is never taken for a person-shaped name."""
    in_clear = [cell for cell, _ in cells(path, column)]
    assert in_clear, "some names are organisations by evidence"
    register = Register.landed()
    vouched = [n for n in in_clear if not is_organisation(n)]
    print(f"{len(vouched)} clear names on the register's word alone")
    assert [n for n in vouched if not register.vouches(n)] == []
    assert not any(person_shaped(n) for n in vouched)


def test_the_landed_contracts_carry_their_mess_and_no_hashed_name_in_a_description() -> None:
    path = landed("chicago-contracts")
    counts = contracts.profile(path)
    print(counts)
    assert sum(counts["order_number_formats"].values()) == counts["rows"]
    assert counts["contracts_with_several_rows"] < counts["contracts"] < counts["rows"]
    assert 0 < counts["negative_awards"] < counts["rows"]
    assert 0 < counts["non_numeric_revisions"] < counts["rows"]
    assert counts["vendors_hashed"] <= counts["hashed_rows"] < counts["rows"]
    assert counts["hashed_rows_with_contract_number"] <= counts["hashed_rows"]
    assert counts["hashed_rows_with_specification_number"] <= counts["hashed_rows"]
    clear_names_are_organisations(path, "vendor_name")
    hashed, as_person = hashes(path, "vendor_name"), person_hasher()
    found = {c: holding(cells(path, c), hashed, as_person) for c in contracts.TABLE.landed}
    print("rows whose clear cell holds a hashed vendor's name, by column:", found)
    assert found["vendor_name"] > 0, "a clear company holds a hashed name: the key matches"
    unchecked = ("vendor_name", "department", "city")
    assert {c: n for c, n in found.items() if c not in unchecked and n} == {}


def test_the_landed_payments_carry_their_roll_ups_and_no_hashed_name_in_a_code() -> None:
    path = landed("chicago-payments")
    counts = payments.profile(path)
    print(counts)
    assert counts["itemised_rows"] + counts["rolled_up_rows"] == counts["rows"]
    assert sum(counts["rolled_up_by_year"].values()) == counts["rolled_up_rows"]
    assert 0 < counts["rolled_up_by_year"]["2002"] < counts["rolled_up_rows"]
    assert counts["earliest_year"] == 2002, "1996 to 2001 live only inside the 2002 bucket"
    assert counts["unreadable_dates"] == 0
    assert counts["without_voucher"] == counts["rolled_up_rows"], "only roll-ups lack a voucher"
    assert 0 < counts["without_contract"] < counts["itemised_rows"]
    assert counts["payees_hashed"] <= counts["rows_hashed"] < counts["rows"]
    assert counts["hashed_rows_with_voucher"] <= counts["rows_hashed"]
    clear_names_are_organisations(path, "vendor_name")
    hashed, as_person = hashes(path, "vendor_name"), person_hasher()
    found = {c: holding(cells(path, c), hashed, as_person) for c in payments.TABLE.landed}
    print("rows whose clear cell holds a hashed payee's name, by column:", found)
    assert found["vendor_name"] > 0, "a clear company holds a hashed name: the key matches"
    unchecked = ("vendor_name", "department_name")
    assert {c: n for c, n in found.items() if c not in unchecked and n} == {}


def exposed(
    column_cells: list[tuple[str, int]], hashed: set[str], as_person: Callable[[str], str]
) -> tuple[set[str], int]:
    """The hashes of `hashed` that some cell's runs carry, and how many rows hold one."""
    names, rows = set(), 0
    for cell, n in column_cells:
        found = {as_person(r) for r in runs(cell)} & hashed
        names |= found
        rows += n if found else 0
    return names, rows


def test_the_cross_file_exposure_is_counted_and_consistent() -> None:
    """A name one file hashes may sit in clear in the other file's descriptions: each step sees
    only its own table, and a landing must not depend on another table's data. The count is the
    known remaining gap, mostly organisations hashed for lack of evidence; a vendor the register
    vouches for keeps its description, so the gap also holds what those descriptions name. It is
    reported, not asserted to be zero."""
    contract_path, payment_path = landed("chicago-contracts"), landed("chicago-payments")
    as_person = person_hasher()
    from_contracts = hashes(contract_path, "vendor_name")
    from_payments = hashes(payment_path, "vendor_name")
    report = {}
    for label, path, columns, own, other in (
        (
            "contract descriptions",
            contract_path,
            contracts.TABLE.descriptions,
            from_contracts,
            from_payments,
        ),
        (
            "payment descriptions",
            payment_path,
            payments.TABLE.descriptions,
            from_payments,
            from_contracts,
        ),
        ("contract vendor names", contract_path, ("vendor_name",), from_contracts, from_payments),
        ("payee names", payment_path, ("vendor_name",), from_payments, from_contracts),
    ):
        names, rows = set(), 0
        for column in columns:
            found, n = exposed(cells(path, column), other, as_person)
            names, rows = names | found, rows + n
        assert names <= other, "every counted name is one the other file hashes"
        if "descriptions" in label:
            assert not names & own, "a name its own file hashes never sits in its descriptions"
        assert (rows == 0) == (not names)
        report[label] = {"distinct names": len(names), "rows": rows}
    print("names the other file hashes, in clear here:", report)
