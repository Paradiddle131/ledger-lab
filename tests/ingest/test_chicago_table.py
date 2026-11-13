"""The shared Chicago landing: who is hashed, what else is blanked, and that nothing touches disk.

Every person is invented; the organisations are invented too.
"""

import csv
import io
import os
import tempfile
from pathlib import Path

import duckdb
import pytest

from ingest import scrub
from ingest.chicago.table import Table, land_all
from ingest.entities import Register
from ingest.individuals import KEY_VARIABLE, person_hasher

ENV = {KEY_VARIABLE: "k" * 32}
NONE = Register(())
SPEC = Table(
    ("note", "payee", "payee_id", "street", "zip"),
    name="payee",
    identifier="payee_id",
    personal=("note", "zip"),
    dropped=("street",),
    descriptions=("note",),
)
PERSON = "JANE Q EXAMPLE"
COMPANY = "EXAMPLE WORKS LLC"


def served(*rows: tuple[str, str, str, str, str]) -> bytes:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(SPEC.columns)
    writer.writerows(rows)
    return out.getvalue().encode()


def landed(tmp_path: Path, *rows: tuple[str, str, str, str, str]) -> list[dict[str, object]]:
    path = tmp_path / "landed"
    path.write_bytes(land_all({"t": (served(*rows), SPEC)}, ENV, NONE)["t"])
    rel = duckdb.read_parquet(str(path))
    return [dict(zip(rel.columns, r, strict=True)) for r in rel.fetchall()]


def test_a_person_and_their_identifier_are_hashed_apart(tmp_path: Path) -> None:
    (row,) = landed(tmp_path, ("note", PERSON, "V1", "1 EXAMPLE ST", "60601"))
    assert row["payee"] == person_hasher(ENV)(PERSON)
    assert str(row["payee_id"]).startswith("id:")
    assert str(row["payee_id"])[3:] != str(row["payee"])[7:]
    assert (row["note"], row["zip"]) == (None, None)


def test_an_identifier_hashes_the_same_on_every_hashed_row(tmp_path: Path) -> None:
    first, second = landed(tmp_path, ("", PERSON, "V1", "", ""), ("", "J Q EXAMPLE", "V1", "", ""))
    assert first["payee_id"] == second["payee_id"]


def test_an_identifier_is_hashed_only_where_its_own_rows_name_is(tmp_path: Path) -> None:
    person, company = landed(tmp_path, ("", PERSON, "V1", "", ""), ("", COMPANY, "V1", "", ""))
    assert str(person["payee"]).startswith("person:")
    assert str(person["payee_id"]).startswith("id:")
    assert (company["payee"], company["payee_id"]) == (COMPANY, "V1")


def test_the_street_columns_are_left_out_of_every_row(tmp_path: Path) -> None:
    (row,) = landed(tmp_path, ("note", COMPANY, "V2", "ATTN JANE Q EXAMPLE", "60601"))
    assert "street" not in row
    assert (row["note"], row["zip"]) == ("note", "60601")


@pytest.mark.parametrize(
    "note",
    [
        "REPAIRS FOR JANE Q EXAMPLE, 2024",
        "ATTN jane  q example",
        "REPAIRS (JANE Q EXAMPLE)",
        'REPAIRS "JANE Q EXAMPLE"',
        "JANE Q EXAMPLE'S GARAGE",
        "JANE Q EXAMPLE-LOT 2",
        "JANE Q. EXAMPLE",
        "EXAMPLE, JANE Q",
        "EXAMPLE,JANE Q.",
    ],
)
def test_a_hashed_name_in_another_rows_description_blanks_it(tmp_path: Path, note: str) -> None:
    _, other = landed(tmp_path, ("", PERSON, "V1", "", ""), (note, COMPANY, "V2", "", ""))
    assert other["note"] is None


@pytest.mark.parametrize("note", ["ESTATE OF A PAYEE, SETTLEMENT", "REFUND, DECEASED OWNER"])
def test_a_description_with_a_person_marker_is_blanked(tmp_path: Path, note: str) -> None:
    (row,) = landed(tmp_path, (note, COMPANY, "V2", "", ""))
    assert row["note"] is None


def test_an_organisations_description_is_otherwise_landed_as_served(tmp_path: Path) -> None:
    _, other = landed(
        tmp_path, ("", PERSON, "V1", "", ""), ("REPAIRS FOR JANET EXAMPLES", COMPANY, "V2", "", "")
    )
    assert other["note"] == "REPAIRS FOR JANET EXAMPLES"


def test_a_name_of_figures_only_blanks_nothing(tmp_path: Path) -> None:
    _, other = landed(tmp_path, ("", "12345", "V1", "", ""), ("LOT 12345", COMPANY, "V2", "", ""))
    assert other["note"] == "LOT 12345"


def test_a_company_named_after_a_hashed_person_stays_in_clear(tmp_path: Path) -> None:
    rows = landed(tmp_path, ("", PERSON, "V1", "", ""), ("", f"{PERSON} LLC", "V2", "", ""))
    assert rows[1]["payee"] == f"{PERSON} LLC"


def test_nothing_is_written_outside_memory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scratch = tmp_path / "tmp"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    monkeypatch.setenv("TMPDIR", str(scratch))
    land_all({"t": (served(("note", PERSON, "V1", "", "")), SPEC)}, ENV, NONE)
    assert os.listdir(scratch) == []
    assert scrub.MEMORY.ls("/") == []


def test_the_landing_refuses_without_the_key_before_reading_a_row() -> None:
    with pytest.raises(Exception, match=KEY_VARIABLE):
        land_all({"t": (b"not,the,table\n", SPEC)}, {}, NONE)
