"""The load step runs one Parquet load job per landed file into the `landed` dataset of the lab's
own project, against a stand-in for `bq` and an in-memory bucket, and refuses everything else
before any call. The `warehouse` tests read the loaded tables live and hold them to the local
landing: row for row and column for column, and with no name the landing hashes in clear."""

import json
import re
import subprocess
from collections.abc import Sequence
from typing import Any

import duckdb
import pytest
from conftest import landed_file
from fsspec.implementations.memory import MemoryFileSystem

from ingest.bucket import LOCK
from ingest.fetch import landing_path
from ingest.individuals import person_hasher
from ingest.load import (
    DATASET,
    MAX_BYTES_BILLED,
    STORAGE_CAP,
    LoadError,
    load,
    load_command,
    main,
    query,
    table_name,
)

PROJECT = "the-lab-project"
BUCKET = f"{PROJECT}-landing"
ENV = {"LEDGER_LAB_GCP_PROJECT": PROJECT, "LEDGER_LAB_BQ_LOCATION": "us-central1"}
LANDED = ("chicago-contracts", "ecb-rates")


def verb_of(command: Sequence[str]) -> str:
    return next(c for c in command[1:] if not c.startswith("-"))


class Bq:
    """A stand-in for `bq`: records every command and answers `show` with a table of `rows` rows
    and `size` bytes, a dry run with `scanned` bytes and a query with `rows_answered`."""

    def __init__(self, rows: int = 7, size: int = 1000, scanned: int = 10) -> None:
        self.calls: list[list[str]] = []
        self.rows, self.size, self.scanned = rows, size, scanned
        self.rows_answered: list[dict[str, Any]] = [{"n": "1"}]
        self.fail_on: str | None = None

    def __call__(self, command: Sequence[str]) -> str:
        self.calls.append(list(command))
        verb = verb_of(command)
        if verb == self.fail_on:
            raise subprocess.CalledProcessError(
                2, command, output=f"BigQuery error in {PROJECT}", stderr=f"{PROJECT} denied"
            )
        if verb == "show":
            dataset, table = command[-1].split(":", 1)[1].split(".")
            return json.dumps(
                {
                    "tableReference": {
                        "projectId": PROJECT,
                        "datasetId": dataset,
                        "tableId": table,
                    },
                    "numRows": str(self.rows),
                    "numBytes": str(self.size),
                    "schema": {"fields": [{"name": "vendor_name", "type": "STRING"}]},
                }
            )
        if verb == "query" and "--dry_run" in command:
            return json.dumps({"statistics": {"totalBytesProcessed": str(self.scanned)}})
        if verb == "query":
            return json.dumps(self.rows_answered)
        return ""


def bucket_holding(*names: str) -> MemoryFileSystem:
    fs = MemoryFileSystem(skip_instance_cache=True)
    fs.store.clear()
    fs.pseudo_dirs.clear()
    fs.pseudo_dirs.append("")
    fs.mkdir(BUCKET)
    for name in names:
        fs.pipe_file(f"{BUCKET}/{name}", b"PAR1")
    return fs


def test_each_landed_file_is_one_parquet_load_job_replacing_its_landed_table() -> None:
    bq = Bq()
    tables = load(LANDED, ENV, bq, bucket_holding(*LANDED, "gleif-legal-entities"))
    assert [t.table for t in tables] == ["landed.chicago_contracts", "landed.ecb_rates"]
    assert [t.rows for t in tables] == [7, 7]
    assert [verb_of(c) for c in bq.calls] == ["load", "show", "load", "show"]
    first = bq.calls[0]
    assert first == load_command(PROJECT, "us-central1", BUCKET, "chicago-contracts")
    assert first[:4] == ["bq", f"--project_id={PROJECT}", "--location=us-central1", "--headless"]
    for flag in ("--source_format=PARQUET", "--replace", "--max_bad_records=0"):
        assert flag in first
    assert first[-2:] == [f"{PROJECT}:landed.chicago_contracts", f"gs://{BUCKET}/chicago-contracts"]


def test_no_command_streams_rows_or_reaches_another_dataset() -> None:
    bq = Bq()
    load(LANDED, ENV, bq, bucket_holding(*LANDED))
    words = {w for c in bq.calls for w in c}
    assert not words & {"insert", "query", "mk", "rm", "cp", "update", "extract"}
    targets = [w for w in words if w.startswith(f"{PROJECT}:")]
    assert targets and all(t.startswith(f"{PROJECT}:{DATASET}.") for t in targets)


@pytest.mark.parametrize(
    ("env", "refusal"),
    [
        ({"LEDGER_LAB_BQ_LOCATION": "us-central1"}, "unset"),
        ({"GCP_PROJECT": "another-lab", "LEDGER_LAB_BQ_LOCATION": "us-central1"}, "another lab"),
        ({"LEDGER_LAB_GCP_PROJECT": PROJECT}, "LEDGER_LAB_BQ_LOCATION"),
        ({**ENV, "LEDGER_LAB_BQ_LOCATION": "US"}, "free-tier|one of"),
        ({**ENV, "LEDGER_LAB_BQ_LOCATION": "europe-west2"}, "one of"),
    ],
)
def test_another_project_or_a_location_outside_the_free_regions_is_refused_before_any_call(
    env: dict[str, str], refusal: str
) -> None:
    bq = Bq()
    with pytest.raises(LoadError, match=refusal):
        load(LANDED, env, bq, bucket_holding(*LANDED))
    assert bq.calls == []


@pytest.mark.parametrize(
    "name",
    [LOCK, ".chicago-contracts.new.0", "a/b", "../ecb-rates", "ECB-rates", "", "ecb_rates", "x-"],
)
def test_a_name_that_is_no_landed_file_is_refused_before_any_call(name: str) -> None:
    bq = Bq()
    with pytest.raises(LoadError, match="not a landed file"):
        load([name], ENV, bq, bucket_holding(name) if name and "/" not in name else None)
    assert bq.calls == []


def test_a_file_the_bucket_does_not_hold_is_refused_before_any_load() -> None:
    bq = Bq()
    with pytest.raises(LoadError, match="holds no ecb-rates"):
        load(LANDED, ENV, bq, bucket_holding("chicago-contracts"))
    assert bq.calls == []


def test_nothing_loads_while_a_landing_holds_the_lock() -> None:
    bq = Bq()
    with pytest.raises(LoadError, match="lock"):
        load(LANDED, ENV, bq, bucket_holding(*LANDED, LOCK))
    assert bq.calls == []


def test_no_name_twice_and_at_least_one() -> None:
    for names in ([], ["ecb-rates", "ecb-rates"]):
        bq = Bq()
        with pytest.raises(LoadError):
            load(names, ENV, bq, bucket_holding(*LANDED))
        assert bq.calls == []


def test_a_failed_load_stops_before_the_next_and_names_no_project() -> None:
    bq = Bq()
    bq.fail_on = "load"
    with pytest.raises(LoadError, match=r"cannot load chicago-contracts \(bq exit 2\)") as raised:
        load(LANDED, ENV, bq, bucket_holding(*LANDED))
    assert len(bq.calls) == 1
    assert PROJECT not in str(raised.value)


def test_loaded_tables_above_the_storage_cap_stop_the_load() -> None:
    bq = Bq(size=STORAGE_CAP // 2 + 1)
    with pytest.raises(LoadError, match="cap: stop"):
        load(LANDED, ENV, bq, bucket_holding(*LANDED))
    assert len(bq.calls) == 4, "the second table tips it over and nothing follows"


@pytest.mark.parametrize(
    "shown",
    [
        "not json",
        json.dumps({"numRows": "1"}),
        json.dumps(
            {
                "tableReference": {"datasetId": "payments", "tableId": "chicago_contracts"},
                "numRows": "1",
                "numBytes": "1",
                "schema": {"fields": []},
            }
        ),
    ],
)
def test_a_table_answer_of_another_shape_or_table_is_refused(shown: str) -> None:
    with pytest.raises(LoadError):
        load(["chicago-contracts"], ENV, lambda c: shown, bucket_holding("chicago-contracts"))


def test_main_prints_tables_and_never_the_project(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(list(LANDED), ENV, Bq(), bucket_holding(*LANDED)) == 0
    out = capsys.readouterr()
    assert (
        "landed.chicago_contracts: 7 rows, 1000 bytes, 1 columns, from chicago-contracts" in out.out
    )
    assert PROJECT not in out.out + out.err
    assert main([], ENV) == 2
    failing = Bq()
    failing.fail_on = "load"
    assert main(list(LANDED), ENV, failing, bucket_holding(*LANDED)) == 1
    out = capsys.readouterr()
    assert "load: cannot load chicago-contracts (bq exit 2)" in out.err
    assert PROJECT not in out.out + out.err


def test_a_query_is_dry_run_first_and_capped() -> None:
    bq = Bq()
    assert query("SELECT 1", ENV, bq) == [{"n": "1"}]
    dry, real = bq.calls
    assert "--dry_run" in dry and "--dry_run" not in real
    for command in (dry, real):
        assert f"--maximum_bytes_billed={MAX_BYTES_BILLED}" in command
        assert "--nouse_legacy_sql" in command


def test_a_query_that_would_scan_above_the_cap_is_not_run() -> None:
    bq = Bq(scanned=MAX_BYTES_BILLED + 1)
    with pytest.raises(LoadError, match="not run"):
        query("SELECT 1", ENV, bq)
    assert len(bq.calls) == 1


def test_a_query_outside_the_lab_project_is_refused_before_any_call() -> None:
    bq = Bq()
    with pytest.raises(LoadError):
        query("SELECT 1", {"GCP_PROJECT": "another-lab"}, bq)
    assert bq.calls == []


# The live comparisons: the loaded tables against the local landing.

# Parquet's types as DuckDB reads them, and the BigQuery type a load job gives each.
BIGQUERY_TYPE = {
    "VARCHAR": "STRING",
    "DATE": "DATE",
    "DOUBLE": "FLOAT",
    "BIGINT": "INTEGER",
    "INTEGER": "INTEGER",
    "BOOLEAN": "BOOLEAN",
    "TIMESTAMP WITH TIME ZONE": "TIMESTAMP",
}
# The landed tables' columns that hold a payee's or supplier's name, hashed as a person's where
# the landing step found no evidence of an organisation.
HASHED_NAMES = {
    "chicago-contracts": "vendor_name",
    "chicago-payments": "vendor_name",
    "uk-transport-spend": "supplier",
}
HASH = re.compile(r"^person:[0-9a-f]{64}$")


def lab_env() -> dict[str, str]:
    import os

    return dict(os.environ)


def local_files() -> list[str]:
    names = sorted(p.name for p in landing_path().iterdir() if p.is_file())
    assert names, "the local landing is empty"
    return names


def expected_type(duck: str) -> str:
    if duck.startswith("DECIMAL"):
        precision, scale = map(int, duck[8:-1].split(","))
        return "NUMERIC" if precision - scale <= 29 and scale <= 9 else "BIGNUMERIC"
    return BIGQUERY_TYPE[duck]


def full(name: str, env: dict[str, str]) -> str:
    return f"`{env['LEDGER_LAB_GCP_PROJECT']}.{DATASET}.{table_name(name)}`"


@pytest.mark.warehouse
def test_each_landed_table_holds_its_local_file_row_for_row_and_column_for_column() -> None:
    """Every file of the local landing is a `landed` table with as many rows, the same columns
    in the same order and of the matching types, and in each column as many non-null cells, so
    no row is dropped or split and no cell moved; the row counts from the table's metadata, the
    cells from one capped query per table."""
    from ingest.load import run_tool, show_command, table_of

    env = lab_env()
    report = {}
    for name in local_files():
        path = str(landing_path() / name)
        con = duckdb.connect()
        described = con.execute("DESCRIBE SELECT * FROM read_parquet(?)", [path]).fetchall()
        columns = [(str(c), expected_type(str(t))) for c, t, *_ in described]
        (local_rows,) = con.execute("SELECT count(*) FROM read_parquet(?)", [path]).fetchone() or (
            0,
        )
        project, where = env["LEDGER_LAB_GCP_PROJECT"], env["LEDGER_LAB_BQ_LOCATION"]
        table = table_of(name, run_tool(show_command(project, where, name)))
        assert list(table.columns) == columns, name
        assert table.rows == local_rows, name
        counts = ", ".join(f"COUNT(`{c}`) AS c{i}" for i, (c, _) in enumerate(columns))
        local_counts = con.execute(
            "SELECT " + ", ".join(f'count("{c}")' for c, _ in columns) + " FROM read_parquet(?)",  # noqa: S608
            [path],
        ).fetchone()
        (row,) = query(f"SELECT {counts} FROM {full(name, env)}", env)  # noqa: S608
        assert [int(row[f"c{i}"]) for i in range(len(columns))] == list(local_counts or ()), name
        report[table.table] = (local_rows, table.rows)
    print("table, local rows, loaded rows:")
    for table, (local, loaded) in report.items():
        print(f"{table} {local} {loaded}")


@pytest.mark.warehouse
@pytest.mark.parametrize("name", sorted(HASHED_NAMES))
def test_no_name_the_landing_hashes_is_in_clear_in_the_warehouse(name: str) -> None:
    """The loaded column of names holds exactly the local landing's values, every hash well
    formed, and no clear value whose keyed hash as a person is one of the column's hashes: the
    landing's leak hunts (`pytest -m landed`) hold for the table. Values stay in memory."""
    column, env = HASHED_NAMES[name], lab_env()
    path = str(landed_file(name))
    local = {
        v
        for (v,) in duckdb.connect()
        .execute(f'SELECT DISTINCT "{column}" FROM read_parquet(?)', [path])  # noqa: S608
        .fetchall()
    }
    rows = query(f"SELECT DISTINCT `{column}` AS v FROM {full(name, env)}", env)  # noqa: S608
    loaded = {r["v"] for r in rows}
    assert loaded == local
    hashed = {v for v in loaded if isinstance(v, str) and v.startswith("person:")}
    clear = {v for v in loaded if isinstance(v, str) and not v.startswith("person:")}
    assert hashed and clear
    assert all(HASH.match(v) for v in hashed)
    as_person = person_hasher()
    assert {as_person(v) for v in clear} & hashed == set()
    print(f"{table_name(name)}.{column}: {len(hashed)} hashed, {len(clear)} clear, none a hash")
