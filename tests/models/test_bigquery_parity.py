"""The BigQuery parity check: its local half on DuckDB (what the canonical row text makes equal
and what it tells apart), the SQL its BigQuery half sends, and its verdict, with `bq` stood in for
by a stub that answers from a second DuckDB database. The live run is
`python -m tests.models.bigquery_parity` against the lab project."""

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import duckdb
import pytest

from ingest.load import LoadError
from tests.models import bigquery_parity
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
    """The DuckDB half's text for each family; the BigQuery half is expected to give the same text
    and is checked only by a live run."""
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


def test_the_digest_ignores_row_order_and_tells_these_changed_rows_apart() -> None:
    """Row order kept out, and a changed decimal, a null in place of a decimal and a duplicated
    row told apart; not a text cell holding U+001E or U+001F (see the module's docstring)."""
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


FAMILIES = [
    ("STRING", "cast(`c` as string)"),
    ("INT64", "cast(`c` as string)"),
    ("NUMERIC", "cast(`c` as string)"),
    ("FLOAT64", "cast(cast(round(`c`, 6) as numeric) as string)"),
    ("BOOL", "lower(cast(`c` as string))"),
    ("DATE", "cast(`c` as string)"),
    ("DATETIME", "format_datetime('%Y-%m-%d %H:%M:%E6S', `c`)"),
    ("TIMESTAMP", "format_timestamp('%Y-%m-%d %H:%M:%E6S', `c`, 'UTC')"),
]


@pytest.mark.parametrize(("type_", "cell_sql"), FAMILIES)
def test_the_bigquery_digest_sql_of_one_column_of_each_family(type_: str, cell_sql: str) -> None:
    hashed = f"to_hex(md5(coalesce({cell_sql}, code_points_to_string([30]))))"
    digest = "to_hex(md5(coalesce(string_agg(h, '' order by h), '')))"
    inner = f"select {hashed} as h from `intermediate.t`"  # noqa: S608
    expected = f"select count(*) as n, {digest} as d from ({inner})"  # noqa: S608
    assert bigquery_digest_sql("intermediate", "t", [Column("c", type_)]) == expected


# The type pairs (local, BigQuery) of the 34 relations the landed build makes, as the last live
# parity run printed them.
BUILT_PAIRS = [
    ("BIGINT", "INTEGER"),
    ("BOOLEAN", "BOOLEAN"),
    ("DATE", "DATE"),
    ("DECIMAL(22,2)", "NUMERIC"),
    ("DECIMAL(28,6)", "NUMERIC"),
    ("DECIMAL(38,6)", "NUMERIC"),
    ("FLOAT", "FLOAT"),
    ("HUGEINT", "INTEGER"),
    ("INTEGER", "INTEGER"),
    ("TIMESTAMP", "TIMESTAMP"),
    ("VARCHAR", "STRING"),
]

ENV = {"LEDGER_LAB_GCP_PROJECT": "the-lab", "LEDGER_LAB_BQ_LOCATION": "us-central1"}


class StubBq:
    """`bq` answering `ls`, `show` and `query` for the relations of `remote`, each given as its
    BigQuery column types and a DuckDB table whose digest stands in for BigQuery's, keyed by
    `<dataset>.<relation>`, or by the relation alone for one in `intermediate`."""

    def __init__(self, remote: dict[str, tuple[list[Column], tuple[int, str]]]) -> None:
        self.remote = {k if "." in k else f"intermediate.{k}": v for k, v in remote.items()}
        self.calls: list[list[str]] = []

    def __call__(self, command: Sequence[str], **_: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(command))
        verb = next(c for c in command if not c.startswith("-") and not c.endswith("bq"))
        if verb == "ls":
            out = [
                {"tableReference": {"tableId": key.split(".")[1]}}
                for key in self.remote
                if key.split(".")[0] == command[-1]
            ]
        elif verb == "show":
            fields = [{"name": c.name, "type": c.type} for c in self.remote[command[-1]][0]]
            out = {"schema": {"fields": fields}}
        else:
            key = command[-1].rsplit("`", 2)[-2]
            out = [dict(zip(("n", "d"), map(str, self.remote[key][1]), strict=True))]
        return subprocess.CompletedProcess(list(command), 0, json.dumps(out), "")


def remote_digest(rows: str, columns: str) -> tuple[int, str]:
    """The digest BigQuery's half would give, taken with DuckDB's half over the same rows."""
    with duckdb.connect() as db:
        db.execute(f"create table r as select * from (values {rows}) as v({columns})")  # noqa: S608
        found = db.execute(duckdb_digest_sql("r", local_relations(db)["r"])).fetchone()
    assert found is not None
    return int(found[0]), str(found[1])


def run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    local: dict[str, str],
    remote: dict[str, tuple[list[Column], tuple[int, str]]],
    env: dict[str, str] = ENV,
    stub: StubBq | None = None,
) -> tuple[int, StubBq]:
    database = tmp_path / "local.duckdb"
    database.parent.mkdir(parents=True, exist_ok=True)
    with duckdb.connect(str(database)) as db:
        for name, select in local.items():
            db.execute(f"create table {name} as {select}")
    stub = stub or StubBq(remote)
    for name in ("LEDGER_LAB_GCP_PROJECT", "LEDGER_LAB_BQ_LOCATION", "GCP_PROJECT"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(bigquery_parity.subprocess, "run", stub)
    return bigquery_parity.main([str(database)]), stub


ROWS = "('a', 0.15::decimal(28,6)), ('b', 0.3::decimal(28,6))"
LOCAL = {"r": f"select * from (values {ROWS}) as v(k, amt)"}  # noqa: S608


def test_a_relation_equal_on_both_sides_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    columns = [Column("k", "STRING"), Column("amt", "NUMERIC")]
    status, _ = run(tmp_path, monkeypatch, LOCAL, {"r": (columns, remote_digest(ROWS, "k, amt"))})
    assert status == 0


@pytest.mark.parametrize("remote_type", ["FLOAT64", "STRING"])
def test_a_decimal_against_a_float_or_a_text_column_differs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, remote_type: str
) -> None:
    """Even when the remote cells' text, and so the digest, is the same."""
    columns = [Column("k", "STRING"), Column("amt", remote_type)]
    status, _ = run(tmp_path, monkeypatch, LOCAL, {"r": (columns, remote_digest(ROWS, "k, amt"))})
    assert status == 1


def test_a_digest_or_a_row_count_that_differs_differs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    columns = [Column("k", "STRING"), Column("amt", "NUMERIC")]
    other = remote_digest("('a', 0.15::decimal(28,6)), ('b', 0.31::decimal(28,6))", "k, amt")
    assert run(tmp_path, monkeypatch, LOCAL, {"r": (columns, other)})[0] == 1
    fewer = remote_digest("('a', 0.15::decimal(28,6))", "k, amt")
    assert run(tmp_path / "b", monkeypatch, LOCAL, {"r": (columns, fewer)})[0] == 1


def test_a_row_count_alone_that_differs_differs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same digest stubbed with another count, which the digest itself would not allow."""
    columns = [Column("k", "STRING"), Column("amt", "NUMERIC")]
    rows, digest_ = remote_digest(ROWS, "k, amt")
    assert run(tmp_path, monkeypatch, LOCAL, {"r": (columns, (rows + 1, digest_))})[0] == 1


def test_a_renamed_column_differs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Even with the same digest."""
    columns = [Column("k", "STRING"), Column("amount", "NUMERIC")]
    status, _ = run(tmp_path, monkeypatch, LOCAL, {"r": (columns, remote_digest(ROWS, "k, amt"))})
    assert status == 1


def test_a_relation_on_one_side_only_differs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    columns = [Column("k", "STRING"), Column("amt", "NUMERIC")]
    both = {"r": (columns, remote_digest(ROWS, "k, amt"))}
    local_too = LOCAL | {"extra": "select 1 as x"}
    assert run(tmp_path, monkeypatch, local_too, both)[0] == 1
    remote_too = both | {"stray": ([Column("x", "INT64")], (1, "0" * 32))}
    assert run(tmp_path / "b", monkeypatch, LOCAL, remote_too)[0] == 1


def test_a_relation_in_a_routed_dataset_is_compared_there(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The build's datasets are the profile's and the routing's; a relation is read where it is."""
    assert bigquery_parity.DATASETS == ("intermediate", "group_totals")
    columns = [Column("k", "STRING"), Column("amt", "NUMERIC")]
    remote = {"group_totals.r": (columns, remote_digest(ROWS, "k, amt"))}
    status, stub = run(tmp_path, monkeypatch, LOCAL, remote)
    assert status == 0
    assert any("group_totals.r" in call for call in stub.calls if "show" in call)


def test_a_relation_in_two_of_the_build_datasets_differs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A copy left in the profile's dataset beside the routed one, even with the same rows."""
    columns = [Column("k", "STRING"), Column("amt", "NUMERIC")]
    same = (columns, remote_digest(ROWS, "k, amt"))
    status, _ = run(tmp_path, monkeypatch, LOCAL, {"group_totals.r": same, "r": same})
    assert status == 1


def test_nothing_compared_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert run(tmp_path, monkeypatch, {}, {})[0] == 1


@pytest.mark.parametrize(("local_type", "remote_type"), BUILT_PAIRS)
def test_each_type_pair_the_build_makes_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, local_type: str, remote_type: str
) -> None:
    """A null of the local type against BigQuery's type, the digest taken from the same row."""
    select = f"select cast(null as {local_type}) as c"
    with duckdb.connect() as db:
        db.execute(f"create table r as {select}")
        found = db.execute(duckdb_digest_sql("r", local_relations(db)["r"])).fetchone()
    assert found is not None
    remote = {"r": ([Column("c", remote_type)], (int(found[0]), str(found[1])))}
    assert run(tmp_path, monkeypatch, {"r": select}, remote)[0] == 0


def test_every_query_carries_the_bytes_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    columns = [Column("k", "STRING"), Column("amt", "NUMERIC")]
    _, stub = run(tmp_path, monkeypatch, LOCAL, {"r": (columns, remote_digest(ROWS, "k, amt"))})
    queries = [call for call in stub.calls if "query" in call]
    assert queries
    assert all("--maximum_bytes_billed=1000000000" in call for call in queries)
    assert all("--location=us-central1" in call for call in stub.calls)


@pytest.mark.parametrize("where", [None, "US", "EU", "europe-west1"])
def test_a_region_outside_the_free_tier_is_refused_before_any_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, where: str | None
) -> None:
    env = {"LEDGER_LAB_GCP_PROJECT": "the-lab"} | (
        {"LEDGER_LAB_BQ_LOCATION": where} if where else {}
    )
    stub = StubBq({})
    with pytest.raises(LoadError, match="LEDGER_LAB_BQ_LOCATION"):
        run(tmp_path, monkeypatch, LOCAL, {}, env, stub)
    assert stub.calls == []
