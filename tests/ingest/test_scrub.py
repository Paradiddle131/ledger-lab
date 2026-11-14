"""The shared scrub finds a hashed name inside free text three ways, and writes Parquet in memory.

Every name here is invented.
"""

from pathlib import Path

import duckdb
import pytest

from ingest import scrub
from ingest.scrub import HashedNames, in_memory, parquet_from_csv

NAMES = HashedNames({"JANE EXAMPLE", "4400123"})


@pytest.mark.parametrize("cell", ["Refund (JANE EXAMPLE)", "EXAMPLE, JANE", "jane example's claim"])
def test_a_name_in_any_order_and_punctuation_is_found(cell: str) -> None:
    assert NAMES.in_any_order(cell)


def test_a_name_cut_short_at_the_end_is_found_only_there() -> None:
    assert NAMES.cut_short("Claim JANE EXAMP")
    assert not NAMES.cut_short("JANE EXAMP route 7")
    assert not NAMES.in_any_order("Claim JANE EXAMP")


def test_sharing_a_word_or_a_run_of_figures_finds_nothing() -> None:
    cell = "Example route 4400123"
    assert not (NAMES.in_any_order(cell) or NAMES.cut_short(cell))


def test_parquet_is_written_from_memory_and_leaves_nothing_there(tmp_path: Path) -> None:
    path = tmp_path / "rows.parquet"
    path.write_bytes(parquet_from_csv("a,b\n1,x\n", dtype={"a": "BIGINT", "b": "VARCHAR"}))
    assert duckdb.read_parquet(str(path)).fetchall() == [(1, "x")]
    assert scrub.MEMORY.ls("/") == []


def test_the_steps_engine_cannot_spill_to_disk() -> None:
    setting = in_memory().execute("SELECT current_setting('temp_directory')").fetchone()
    assert setting == ("",)
