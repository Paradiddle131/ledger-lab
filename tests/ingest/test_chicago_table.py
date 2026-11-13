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

from ingest.chicago import table
from ingest.chicago.table import Table, hashed_names_inside, land, words_inside
from ingest.individuals import KEY_VARIABLE, person_hasher

ENV = {KEY_VARIABLE: "k" * 32}
SPEC = Table(
    ("note", "payee", "payee_id", "street"),
    name="payee",
    identifier="payee_id",
    personal=("note", "street"),
    free_text=("note", "street"),
)


def served(*rows: tuple[str, str, str, str]) -> bytes:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(SPEC.columns)
    writer.writerows(rows)
    return out.getvalue().encode()


def landed(tmp_path: Path, *rows: tuple[str, str, str, str]) -> list[dict[str, object]]:
    path = tmp_path / "landed"
    path.write_bytes(land(served(*rows), SPEC, ENV))
    rel = duckdb.read_parquet(str(path))
    return [dict(zip(rel.columns, r, strict=True)) for r in rel.fetchall()]


def test_a_person_and_their_identifier_are_hashed_apart(tmp_path: Path) -> None:
    (row,) = landed(tmp_path, ("note", "JANE Q EXAMPLE", "V1", "1 EXAMPLE ST"))
    assert row["payee"] == person_hasher(ENV)("JANE Q EXAMPLE")
    assert str(row["payee_id"]).startswith("id:")
    assert str(row["payee_id"])[3:] != str(row["payee"])[7:]
    assert (row["note"], row["street"]) == (None, None)


def test_an_identifier_hashes_the_same_on_every_row(tmp_path: Path) -> None:
    first, second = landed(
        tmp_path, ("", "JANE Q EXAMPLE", "V1", ""), ("", "J Q EXAMPLE", "V1", "")
    )
    assert first["payee_id"] == second["payee_id"]


def test_an_identifier_beside_a_hashed_name_hashes_every_name_it_carries(tmp_path: Path) -> None:
    rows = landed(
        tmp_path,
        ("", "JANE Q EXAMPLE", "V1", ""),
        ("", "JANE EXAMPLE LLC", "V1", "2 EXAMPLE AVE"),
        ("", "JANE EXAMPLE LLC", "V2", ""),
        ("", "JANE EXAMPLE LLC", "V3", ""),
        ("", "OTHER EXAMPLE LLC", "V3", ""),
        ("", "UNRELATED EXAMPLE INC", "V4", ""),
    )
    assert [str(r["payee"]).startswith("person:") for r in rows] == [True] * 5 + [False]
    assert [str(r["payee_id"]).startswith("id:") for r in rows] == [True] * 5 + [False]
    assert rows[1]["street"] is None


def test_a_hashed_name_in_another_rows_free_text_blanks_that_cell(tmp_path: Path) -> None:
    rows = landed(
        tmp_path,
        ("", "JANE Q EXAMPLE", "V1", ""),
        ("REPAIRS FOR JANE Q EXAMPLE, 2024", "EXAMPLE WORKS LLC", "V2", "1 MAIN ST"),
        ("ATTN jane  q example", "EXAMPLE WORKS LLC", "V2", "C/O JANE Q EXAMPLE"),
        ("REPAIRS", "EXAMPLE WORKS LLC", "V2", "1 MAIN ST"),
    )
    assert [(r["note"], r["street"]) for r in rows[1:]] == [
        (None, "1 MAIN ST"),
        (None, None),
        ("REPAIRS", "1 MAIN ST"),
    ]


def test_a_company_named_after_a_hashed_person_stays_in_clear(tmp_path: Path) -> None:
    rows = landed(tmp_path, ("", "JANE Q EXAMPLE", "V1", ""), ("", "JANE Q EXAMPLE LLC", "V2", ""))
    assert rows[1]["payee"] == "JANE Q EXAMPLE LLC"


def test_words_inside_are_every_run_normalised() -> None:
    assert words_inside("A b, C 12", 2) == {"a", "b", "c", "a b", "b, c", "c 12"}


def test_nothing_is_written_outside_memory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scratch = tmp_path / "tmp"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    monkeypatch.setenv("TMPDIR", str(scratch))
    land(served(("note", "JANE Q EXAMPLE", "V1", "")), SPEC, ENV)
    assert os.listdir(scratch) == []
    assert table.MEMORY.ls("/") == []


def test_the_landing_refuses_without_the_key_before_reading_a_row() -> None:
    with pytest.raises(Exception, match=KEY_VARIABLE):
        land(b"not,the,table\n", SPEC, {})


def test_the_leak_check_finds_a_hashed_name_in_any_clear_cell(tmp_path: Path) -> None:
    path = tmp_path / "landed"
    path.write_bytes(land(served(("", "JANE Q EXAMPLE", "V1", "")), SPEC, ENV))
    assert hashed_names_inside(path, ENV) == dict.fromkeys(SPEC.columns, 0)
    duckdb.read_parquet(str(path)).project(
        "'PAID JANE Q EXAMPLE' AS note, payee, payee_id, street"
    ).write_parquet(str(tmp_path / "leaky"))
    assert hashed_names_inside(tmp_path / "leaky", ENV)["note"] == 1
