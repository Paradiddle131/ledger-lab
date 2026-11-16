"""The BigQuery parity check's local half, on DuckDB: what the canonical row text makes equal and
what it tells apart. Its BigQuery half runs only against the lab project
(`python -m tests.models.bigquery_parity`)."""

import duckdb
import pytest

from tests.models.bigquery_parity import (
    Column,
    bigquery_digest_sql,
    duckdb_cell,
    duckdb_digest_sql,
    family,
    local_relations,
)


def cell(type_: str, literal: str) -> str | None:
    with duckdb.connect() as db:
        db.execute(f"create table t as select cast({literal} as {type_}) as v")
        row = db.execute(f"select {duckdb_cell(Column('v', type_.upper()))} from t").fetchone()  # noqa: S608
    assert row is not None
    return row[0]


@pytest.mark.parametrize(
    ("type_", "literal", "text"),
    [
        ("decimal(28,6)", "'12.5'", "12.5"),
        ("decimal(28,6)", "'-0.5'", "-0.5"),
        ("decimal(28,6)", "'120'", "120"),
        ("decimal(28,6)", "'0'", "0"),
        ("decimal(18,0)", "'120'", "120"),
        ("float", "0.9", "0.9"),
        ("double", "1.0", "1"),
        ("bigint", "42", "42"),
        ("boolean", "true", "true"),
        ("date", "'2024-03-29'", "2024-03-29"),
        ("timestamp", "'2025-04-01 09:30:00'", "2025-04-01 09:30:00.000000"),
        ("varchar", "'person:ab'", "person:ab"),
    ],
)
def test_a_cell_is_written_in_its_familys_canonical_text(
    type_: str, literal: str, text: str
) -> None:
    """The texts BigQuery's `cast(... as string)` gives for the same values (`12.5`, `120`, `0`,
    `true`), which the BigQuery half writes."""
    assert cell(type_, literal) == text


def test_a_type_outside_the_families_stops_the_comparison() -> None:
    with pytest.raises(ValueError, match="no canonical text"):
        family("duckdb", "BLOB")
    with pytest.raises(ValueError, match="no canonical text"):
        family("bigquery", "JSON")


def digest(rows: str) -> tuple[int, str]:
    with duckdb.connect() as db:
        db.execute(f"create table t as select * from (values {rows}) as v(k, amount)")  # noqa: S608
        columns = local_relations(db)["t"]
        found = db.execute(duckdb_digest_sql("t", columns)).fetchone()
    assert found is not None
    return int(found[0]), str(found[1])


def test_the_digest_ignores_row_order_and_tells_a_changed_cell_or_a_null_apart() -> None:
    rows = "('a', 1.50::decimal(28,6)), ('b', 2::decimal(28,6))"
    same = digest(rows)
    assert digest("('b', 2::decimal(28,6)), ('a', 1.50::decimal(28,6))") == same
    assert digest("('a', 1.51::decimal(28,6)), ('b', 2::decimal(28,6))") != same
    assert digest("('a', null::decimal(28,6)), ('b', 2::decimal(28,6))") != same
    assert digest("('a', 1.50::decimal(28,6)), ('a', 1.50::decimal(28,6))") != same


def test_the_bigquery_digest_names_only_the_dataset_and_relation() -> None:
    sql = bigquery_digest_sql("intermediate", "payments", [Column("amount", "NUMERIC")])
    assert "from `intermediate.payments`" in sql
    assert "cast(`amount` as string)" in sql
