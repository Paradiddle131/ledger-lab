"""The load step runs one Parquet load job per landed file into the `landed` dataset of the lab's
own project while it holds the landing's lock, against a stand-in for `bq` and an in-memory bucket;
it refuses another project, a location outside the free regions and a name no landing step writes
before any call, and a held lock or a missing object before any load. Its queries name
only `<lab project>.landed.<table>`, built from a landed file's name and the table's own columns.
The `warehouse` tests read the loaded tables live and hold them to the local landing: the same
rows, columns and content digest, and no name the landing hashes in clear."""

import json
import os
import random
import re
import secrets
import subprocess
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import duckdb
import hcl2
import pytest
import sqlglot
from conftest import REPO, landed_file
from fsspec.implementations.memory import MemoryFileSystem
from sqlglot import exp

from ingest.bucket import LOCK, Bucket, BucketError
from ingest.fetch import landing_path
from ingest.individuals import KEY_VARIABLE, person_hasher
from ingest.load import (
    DATASET,
    LOCATION_VARIABLE,
    MAX_BYTES_BILLED,
    STORAGE_CAP,
    Digest,
    LoadError,
    bigquery_digest_sql,
    count,
    counts,
    digest,
    distinct_values,
    free_regions,
    lab,
    landed_names,
    load,
    load_command,
    local_digest,
    main,
    run_tool,
    show_command,
    table_name,
    table_of,
)

PROJECT = "the-lab-project"
BUCKET = f"{PROJECT}-landing"
ENV = {"LEDGER_LAB_GCP_PROJECT": PROJECT, "LEDGER_LAB_BQ_LOCATION": "us-central1"}
ENV_KEYED = {**ENV, "LEDGER_LAB_HASH_KEY": "invented-key-0000000000000000000000000000"}
LANDED = ("chicago-contracts", "ecb-rates")
# DuckDB's reading of a Parquet type, and the type a BigQuery load job gives that column.
BIGQUERY_TYPE = {"VARCHAR": "STRING", "BIGINT": "INTEGER", "BOOLEAN": "BOOLEAN"}


def verb_of(command: Sequence[str]) -> str:
    return next(c for c in command[1:] if not c.startswith("-"))


def target_of(command: Sequence[str]) -> str:
    return command[-1].split(":", 1)[1].split(".")[-1]


def parquet_columns(path: Path) -> list[tuple[str, str]]:
    rows = duckdb.connect().execute("DESCRIBE SELECT * FROM read_parquet(?)", [str(path)])
    return [(str(c), BIGQUERY_TYPE[str(t)]) for c, t, *_ in rows.fetchall()]


def on_duckdb(sql: str, tables: dict[str, Path]) -> list[dict[str, Any]]:
    """A BigQuery query run on DuckDB over the stand-in's tables, as sqlglot reads it, its answer
    as `bq --format=json` gives one: every value text, nulls null."""
    parsed = sqlglot.parse_one(sql, read="bigquery")
    for table in parsed.find_all(exp.Table):
        assert (table.catalog, table.db) == (PROJECT, DATASET), table.sql("bigquery")
        table.replace(exp.to_table(f"t_{table.name}"))
    duck = parsed.sql("duckdb")
    # sqlglot gives DuckDB a text column's byte length as OCTET_LENGTH, which takes only bytes.
    duck = re.sub(r'OCTET_LENGTH\(("[^"]+")\)', r"OCTET_LENGTH(ENCODE(\1))", duck)
    con = duckdb.connect()
    for name, path in tables.items():
        con.read_parquet(str(path)).create_view(f"t_{name}")
    cursor = con.execute(duck)
    names = [d[0] for d in cursor.description or ()]
    return [
        {n: None if v is None else str(v) for n, v in zip(names, row, strict=True)}
        for row in cursor.fetchall()
    ]


class Bq:
    """A stand-in for `bq`: records every command; a load adds its table to `landed`; `show`
    describes a table from the Parquet file `tables` holds for it, or as `rows` rows of `size`
    bytes; `ls` lists `landed`; a dry run scans `scanned` bytes and a query runs on DuckDB over
    `tables`, or answers `rows_answered`. `during_load` runs inside each load."""

    def __init__(self, rows: int = 7, size: int = 1000, scanned: int = 10) -> None:
        self.calls: list[list[str]] = []
        self.rows, self.size, self.scanned = rows, size, scanned
        self.tables: dict[str, Path] = {}
        self.listed: set[str] = set()
        self.rows_answered: list[dict[str, Any]] = [{"n": "1"}]
        self.dry_answer: str | None = None
        self.answer: str | None = None
        self.fail_on: str | None = None
        self.during_load: Callable[[], None] | None = None

    def __call__(self, command: Sequence[str]) -> str:
        self.calls.append(list(command))
        verb = verb_of(command)
        if verb == self.fail_on:
            raise subprocess.CalledProcessError(
                2, command, output=f"BigQuery error in {PROJECT}", stderr=f"{PROJECT} denied"
            )
        if verb == "load":
            if self.during_load:
                self.during_load()
            self.listed.add(command[-2].split(".")[-1])
            return ""
        if verb == "ls":
            return json.dumps(
                [{"tableReference": {"datasetId": DATASET, "tableId": t}} for t in self.listed]
            )
        if verb == "show":
            table = target_of(command)
            path = self.tables.get(table)
            rows = (
                self.rows
                if path is None
                else int(
                    (
                        duckdb.connect()
                        .execute("SELECT count(*) FROM read_parquet(?)", [str(path)])
                        .fetchone()
                        or [0]
                    )[0]
                )
            )
            columns = [("vendor_name", "STRING")] if path is None else parquet_columns(path)
            return json.dumps(
                {
                    "tableReference": {
                        "projectId": PROJECT,
                        "datasetId": DATASET,
                        "tableId": table,
                    },
                    "numRows": str(rows),
                    "numBytes": str(self.size),
                    "schema": {"fields": [{"name": c, "type": t} for c, t in columns]},
                }
            )
        if verb == "query" and "--dry_run" in command:
            if self.dry_answer is not None:
                return self.dry_answer
            return json.dumps({"statistics": {"totalBytesProcessed": str(self.scanned)}})
        if verb == "query":
            if self.answer is not None:
                return self.answer
            if self.tables:
                return json.dumps(on_duckdb(command[-1], self.tables))
            return json.dumps(self.rows_answered)
        return ""

    def verbs(self) -> list[str]:
        return [verb_of(c) for c in self.calls]


def bucket_holding(*names: str) -> MemoryFileSystem:
    """A bucket of its own: fsspec's memory file system keeps one store for every instance, which
    other tests read, so each stand-in is a class with a fresh store."""

    class Own(MemoryFileSystem):
        store: dict[str, Any] = {}
        pseudo_dirs = [""]
        cachable = False

    fs = Own(skip_instance_cache=True)
    fs.mkdir(BUCKET)
    for name in names:
        fs.pipe_file(f"{BUCKET}/{name}", b"PAR1")
    return fs


def locked(fs: MemoryFileSystem) -> bool:
    return fs.exists(f"{BUCKET}/{LOCK}")


# The load.


def test_each_landed_file_is_one_parquet_load_job_replacing_its_landed_table() -> None:
    bq, fs = Bq(), bucket_holding(*LANDED, "gleif-legal-entities")
    tables = load(LANDED, ENV, bq, fs)
    assert [t.table for t in tables] == ["landed.chicago_contracts", "landed.ecb_rates"]
    assert [t.rows for t in tables] == [7, 7]
    assert bq.verbs() == [
        "ls",
        "load",
        "show",
        "ls",
        "show",
        "load",
        "show",
        "ls",
        "show",
        "show",
    ]
    first = bq.calls[1]
    assert first == load_command(PROJECT, "us-central1", BUCKET, "chicago-contracts")
    assert first[:4] == ["bq", f"--project_id={PROJECT}", "--location=us-central1", "--headless"]
    for flag in ("--source_format=PARQUET", "--replace", "--max_bad_records=0"):
        assert flag in first
    assert first[-2:] == [f"{PROJECT}:landed.chicago_contracts", f"gs://{BUCKET}/chicago-contracts"]
    assert not locked(fs), "the lock is released at the end"


def test_no_command_streams_rows_or_reaches_another_dataset() -> None:
    bq = Bq()
    load(LANDED, ENV, bq, bucket_holding(*LANDED))
    assert set(bq.verbs()) == {"load", "show", "ls"}
    targets = [w for c in bq.calls for w in c if w.startswith(f"{PROJECT}:")]
    assert targets and all(
        t == f"{PROJECT}:{DATASET}" or t.startswith(f"{PROJECT}:{DATASET}.") for t in targets
    )
    assert all(c[1] == f"--project_id={PROJECT}" for c in bq.calls)


@pytest.mark.parametrize(
    ("env", "refusal"),
    [
        ({"LEDGER_LAB_BQ_LOCATION": "us-central1"}, "LEDGER_LAB_GCP_PROJECT is unset"),
        ({"GCP_PROJECT": "another-lab", "LEDGER_LAB_BQ_LOCATION": "us-central1"}, "another lab"),
        ({**ENV, "GCP_PROJECT": "another-lab"}, "another lab"),
        ({"LEDGER_LAB_GCP_PROJECT": PROJECT}, "LEDGER_LAB_BQ_LOCATION must be"),
        ({**ENV, "LEDGER_LAB_BQ_LOCATION": "US"}, "LEDGER_LAB_BQ_LOCATION must be"),
        ({**ENV, "LEDGER_LAB_BQ_LOCATION": "europe-west2"}, "LEDGER_LAB_BQ_LOCATION must be"),
        ({**ENV, "LEDGER_LAB_BQ_LOCATION": "us-east4"}, "LEDGER_LAB_BQ_LOCATION must be"),
    ],
)
def test_another_project_or_a_location_outside_the_free_regions_is_refused_before_any_call(
    env: dict[str, str], refusal: str
) -> None:
    bq, fs = Bq(), bucket_holding(*LANDED)
    with pytest.raises(LoadError, match=refusal):
        load(LANDED, env, bq, fs)
    assert bq.calls == []
    assert not locked(fs), "no lock is taken"


@pytest.mark.parametrize(
    "name",
    [
        LOCK,
        ".chicago-contracts.new.0",
        "a/b",
        "../ecb-rates",
        "ECB-rates",
        "",
        "ecb_rates",
        "x-",
        "not-a-source",
        "chicago-contracts-copy",
        "chicago",
        "sec-annual-filings-2024q1",
        "uk-transport-spend-2025-apr",
    ],
)
def test_a_name_no_landing_step_writes_is_refused_before_any_call(name: str) -> None:
    """Among them a set's name whose step lands its members, a quarter of the securities filings
    (the fetch kit would land it alone as the publisher's file) and a month of the transport spend
    (the fetch kit refuses to land it alone: it names people and has no step of its own)."""
    bq, fs = Bq(), bucket_holding(*LANDED)
    if name and "/" not in name:
        fs.pipe_file(f"{BUCKET}/{name}", b"PAR1")
    with pytest.raises(LoadError, match="not a landed file"):
        load([name], ENV, bq, fs)
    assert bq.calls == []
    assert not locked(fs) or name == LOCK


def test_the_landed_names_are_the_seven_files_the_landing_steps_write() -> None:
    assert landed_names() == {
        "chicago-contracts",
        "chicago-payments",
        "ecb-rates",
        "gleif-legal-entities",
        "nl-government-budget",
        "sec-annual-filings",
        "uk-transport-spend-2025",
    }


def test_a_file_the_bucket_does_not_hold_is_refused_before_any_load() -> None:
    bq, fs = Bq(), bucket_holding("chicago-contracts")
    with pytest.raises(LoadError, match="holds no ecb-rates"):
        load(LANDED, ENV, bq, fs)
    assert bq.calls == []
    assert not locked(fs), "the refused load releases its lock"


def test_no_load_starts_while_a_landing_holds_the_lock_and_none_takes_it() -> None:
    bq, fs = Bq(), bucket_holding(*LANDED, LOCK)
    with pytest.raises(LoadError, match="delete it by hand"):
        load(LANDED, ENV, bq, fs)
    assert bq.calls == []
    assert locked(fs), "the landing's lock stays"


def test_no_landing_starts_during_a_load() -> None:
    """The verifier's case: a landing that starts while the first file loads, to replace the
    second, is refused, so the set loads as it was when the load began."""
    bq, fs = Bq(), bucket_holding("chicago-contracts", "chicago-payments")
    refusals: list[str] = []

    def landing_meanwhile() -> None:
        assert locked(fs), "the load holds the lock while it loads"
        try:
            Bucket(fs, BUCKET).land_all({"chicago-payments": b"new"})
        except BucketError as error:
            refusals.append(str(error))

    bq.during_load = landing_meanwhile
    load(["chicago-contracts", "chicago-payments"], ENV, bq, fs)
    assert len(refusals) == 2 and all("lock" in r for r in refusals)
    assert fs.cat_file(f"{BUCKET}/chicago-payments") == b"PAR1"
    assert not locked(fs)


def test_no_load_starts_during_a_landing() -> None:
    """A load asked for while a landing uploads is refused, and the landing completes."""
    fs = bucket_holding(*LANDED)
    refusals: list[str] = []
    upload = fs.pipe_file

    def uploading(path: str, value: bytes, *args: Any, **kwargs: Any) -> Any:
        if ".new." in path:
            try:
                load(LANDED, ENV, Bq(), fs)
            except LoadError as error:
                refusals.append(str(error))
        return upload(path, value, *args, **kwargs)

    fs.pipe_file = uploading  # type: ignore[method-assign]
    Bucket(fs, BUCKET).land_all({"ecb-rates": b"new"})
    assert refusals and all("lock" in r for r in refusals)
    assert fs.cat_file(f"{BUCKET}/ecb-rates") == b"new"
    assert not locked(fs)


def test_an_interrupted_load_releases_the_lock() -> None:
    bq, fs = Bq(), bucket_holding(*LANDED)

    def interrupt() -> None:
        raise KeyboardInterrupt

    bq.during_load = interrupt
    with pytest.raises(KeyboardInterrupt):
        load(LANDED, ENV, bq, fs)
    assert not locked(fs)


def test_no_name_twice_and_at_least_one() -> None:
    for names in ([], ["ecb-rates", "ecb-rates"]):
        bq, fs = Bq(), bucket_holding(*LANDED)
        with pytest.raises(LoadError):
            load(names, ENV, bq, fs)
        assert bq.calls == [] and not locked(fs)


def test_a_failed_load_stops_before_the_next_releases_the_lock_and_names_no_project() -> None:
    bq, fs = Bq(), bucket_holding(*LANDED)
    bq.fail_on = "load"
    with pytest.raises(LoadError, match=r"cannot load chicago-contracts \(bq exit 2\)") as raised:
        load(LANDED, ENV, bq, fs)
    assert bq.verbs() == ["ls", "load"]
    assert PROJECT not in str(raised.value)
    assert not locked(fs)


def test_what_landed_already_holds_counts_against_the_storage_cap() -> None:
    bq = Bq(size=STORAGE_CAP // 2 + 1)
    bq.listed = {"from_an_earlier_run"}
    with pytest.raises(LoadError, match="cap: stop"):
        load(["chicago-contracts"], ENV, bq, bucket_holding(*LANDED))


@pytest.mark.parametrize(
    "listing",
    [
        "",
        "[]",
        json.dumps([{"tableReference": {"datasetId": DATASET, "tableId": "chicago_payments"}}]),
    ],
    ids=["an empty answer", "an empty list", "a list without the loaded table"],
)
def test_a_listing_that_leaves_out_a_table_this_run_loaded_is_refused(listing: str) -> None:
    """The reviewer's case: the load's own `bq show` reports three times the cap, and a listing
    without its table, given after the load, would count none of it."""
    bq, fs = Bq(size=STORAGE_CAP * 3), bucket_holding("chicago-contracts")

    def run(command: Sequence[str]) -> str:
        return listing if verb_of(command) == "ls" and bq.listed else bq(command)

    with pytest.raises(LoadError, match="leaves out chicago_contracts, loaded in this run: stop"):
        load(["chicago-contracts"], ENV, run, fs)
    assert not locked(fs)


def test_a_listing_that_leaves_out_an_earlier_table_of_the_run_is_refused() -> None:
    bq, fs = Bq(), bucket_holding("chicago-contracts", "ecb-rates")

    def run(command: Sequence[str]) -> str:
        if verb_of(command) == "ls" and "ecb_rates" in bq.listed:
            return json.dumps([{"tableReference": {"datasetId": DATASET, "tableId": "ecb_rates"}}])
        return bq(command)

    with pytest.raises(LoadError, match="leaves out chicago_contracts, loaded in this run"):
        load(["chicago-contracts", "ecb-rates"], ENV, run, fs)
    assert bq.verbs().count("load") == 2


def test_a_loaded_table_counts_at_its_bytes_after_its_load_when_a_later_show_reports_fewer() -> (
    None
):
    bq, shows = Bq(size=STORAGE_CAP + 1), 0

    def run(command: Sequence[str]) -> str:
        nonlocal shows
        if verb_of(command) == "show":
            shows += 1
            bq.size = STORAGE_CAP + 1 if shows == 1 else 0
        return bq(command)

    with pytest.raises(LoadError, match="cap: stop"):
        load(["chicago-contracts"], ENV, run, bucket_holding("chicago-contracts"))
    assert shows == 2


def test_a_table_listed_twice_counts_once() -> None:
    bq = Bq(size=STORAGE_CAP // 2 + 1)

    def run(command: Sequence[str]) -> str:
        if verb_of(command) == "ls":
            listed = json.loads(bq(command))
            return json.dumps(listed * 2)
        return bq(command)

    tables = load(["chicago-contracts"], ENV, run, bucket_holding("chicago-contracts"))
    assert [t.table for t in tables] == ["landed.chicago_contracts"]


def test_a_run_that_starts_above_the_storage_cap_loads_nothing() -> None:
    bq, fs = Bq(size=STORAGE_CAP + 1), bucket_holding("chicago-contracts")
    bq.listed = {"from_an_earlier_run"}
    with pytest.raises(LoadError, match="cap before any load: stop"):
        load(["chicago-contracts"], ENV, bq, fs)
    assert "load" not in bq.verbs()
    assert not locked(fs)


def test_a_negative_byte_count_cancels_no_other_tables_bytes() -> None:
    """An earlier table holds three quarters of the cap, and the table this run loads reports a
    negative size at each `bq show`: the load is refused at the first such answer, before a
    negative could offset the bytes of a table loaded after it. The reviewer's case, an earlier
    table of one and a half caps, is refused before any load."""
    bq = Bq(size=0)
    bq.listed = {"from_an_earlier_run"}

    def run(command: Sequence[str]) -> str:
        out = bq(command)
        if verb_of(command) == "show":
            answer = json.loads(out)
            earlier = answer["tableReference"]["tableId"] == "from_an_earlier_run"
            answer["numBytes"] = str(STORAGE_CAP * share // 4) if earlier else "-2147483648"
            return json.dumps(answer)
        return out

    share = 3
    with pytest.raises(LoadError, match="unexpected shape"):
        load(["chicago-contracts"], ENV, run, bucket_holding("chicago-contracts"))
    assert bq.verbs().count("load") == 1
    share, bq.calls, bq.listed = 6, [], {"from_an_earlier_run"}
    with pytest.raises(LoadError, match="cap before any load"):
        load(["chicago-contracts"], ENV, run, bucket_holding("chicago-contracts"))
    assert "load" not in bq.verbs()


def test_the_caps_are_the_figures_the_documents_state() -> None:
    assert STORAGE_CAP == 2 * 1024**3
    assert MAX_BYTES_BILLED == 1_000_000_000


def test_the_load_that_tips_the_storage_cap_is_the_last() -> None:
    names = ["chicago-contracts", "chicago-payments", "ecb-rates"]
    bq, fs = Bq(size=STORAGE_CAP // 2 + 1), bucket_holding(*names)
    with pytest.raises(LoadError, match="cap: stop"):
        load(names, ENV, bq, fs)
    assert bq.verbs().count("load") == 2, "no third load"
    assert not locked(fs)


def shown(**reference: str) -> str:
    return json.dumps(
        {
            "tableReference": reference,
            "numRows": "1",
            "numBytes": "1",
            "schema": {"fields": []},
        }
    )


@pytest.mark.parametrize(
    "answer",
    [
        "not json",
        json.dumps({"numRows": "1"}),
        shown(datasetId="payments", tableId="chicago_contracts"),
        shown(datasetId=DATASET, tableId="chicago_payments"),
        shown(projectId="another-lab", datasetId=DATASET, tableId="chicago_contracts"),
        json.dumps(
            {
                "tableReference": {"datasetId": DATASET, "tableId": "chicago_contracts"},
                "numRows": "1",
                "schema": {"fields": []},
            }
        ),
        *(
            json.dumps(
                {
                    "tableReference": {"datasetId": DATASET, "tableId": "chicago_contracts"},
                    "numRows": rows,
                    "numBytes": size,
                    "schema": {"fields": []},
                }
            )
            for rows, size in (("-1", "1"), ("1", "-1"))
        ),
    ],
    ids=[
        "not json",
        "no table reference",
        "another dataset",
        "another table",
        "another project",
        "no byte count",
        "a negative row count",
        "a negative byte count",
    ],
)
def test_a_table_answer_of_another_shape_project_dataset_or_table_is_refused(answer: str) -> None:
    with pytest.raises(LoadError):
        table_of("chicago-contracts", answer, PROJECT)
    with pytest.raises(LoadError):
        load(["chicago-contracts"], ENV, lambda c: answer, bucket_holding("chicago-contracts"))


def counted(rows: Any, size: Any) -> str:
    """A `bq show` answer for `landed.chicago_contracts` with these JSON values as its counts."""
    return json.dumps(
        {
            "tableReference": {"datasetId": DATASET, "tableId": "chicago_contracts"},
            "numRows": rows,
            "numBytes": size,
            "schema": {"fields": []},
        }
    )


# Values that are no JSON string of 1 to 19 ASCII digits, each tried at every count the loader
# reads. `float("inf")` goes out as JSON's `Infinity`, which `json.loads` reads back.
NOT_A_COUNT = [
    True,
    False,
    None,
    0,
    5,
    0.9,
    -0.5,
    -0.9,
    float("inf"),
    float("-inf"),
    float("nan"),
    "１２",
    "١",
    "-1",
    "+1",
    " 1",
    "1 ",
    "1\n",
    "1_000",
    "1.0",
    "",
    "1" * 20,
    "1" * 5000,
    ["1"],
]


@pytest.mark.parametrize("value", NOT_A_COUNT, ids=[repr(v) for v in NOT_A_COUNT])
@pytest.mark.parametrize("field", ["numRows", "numBytes"])
def test_a_count_that_is_no_ascii_digit_string_is_an_answer_of_unexpected_shape(
    field: str, value: Any
) -> None:
    answer = counted(value, "1") if field == "numRows" else counted("1", value)
    with pytest.raises(LoadError, match="unexpected shape"):
        table_of("chicago-contracts", answer, PROJECT)


@pytest.mark.parametrize("value", NOT_A_COUNT, ids=[repr(v)[:20] for v in NOT_A_COUNT])
def test_count_refuses_with_a_value_error(value: Any) -> None:
    with pytest.raises(ValueError, match="no string of ASCII digits"):
        count(value)


def test_count_bounds_the_digits_without_pythons_integer_string_limit() -> None:
    limit = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(0)
    try:
        for value in ("1" * 20, "1" * 5000):
            with pytest.raises(ValueError, match="no string of ASCII digits"):
                count(value)
        assert count("9223372036854775807") == 2**63 - 1
    finally:
        sys.set_int_max_str_digits(limit)


def test_an_ascii_digit_string_is_a_count() -> None:
    table = table_of("chicago-contracts", counted("0", "007"), PROJECT)
    assert (table.rows, table.bytes) == (0, 7)
    table = table_of("chicago-contracts", counted("12", str(STORAGE_CAP)), PROJECT)
    assert (table.rows, table.bytes) == (12, STORAGE_CAP)


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


def test_the_free_regions_are_the_ones_the_lab_projects_definitions_allow() -> None:
    variables = hcl2.loads((REPO / "platform/gcp/variables.tf").read_text())
    (region,) = [v['"region"'] for v in variables["variable"] if '"region"' in v]
    condition = str(region["validation"][0]["condition"])
    assert set(free_regions()) == set(re.findall(r"us-[a-z]+[0-9]", condition))
    assert free_regions() == ("us-west1", "us-central1", "us-east1")


# The queries.


def queries(bq: Bq) -> list[str]:
    return [c[-1] for c in bq.calls if verb_of(c) == "query"]


def test_a_query_is_dry_run_first_and_capped() -> None:
    bq = Bq()
    bq.rows_answered = [{"n": "3", "c0": "2"}]
    assert counts("chicago-contracts", ENV, bq) == (3, [2])
    show, dry, real = bq.calls
    assert verb_of(show) == "show"
    assert "--dry_run" in dry and "--dry_run" not in real
    for command in (dry, real):
        assert f"--maximum_bytes_billed={MAX_BYTES_BILLED}" in command
        assert "--nouse_legacy_sql" in command


def test_a_query_that_would_scan_above_the_cap_is_not_run() -> None:
    bq = Bq(scanned=MAX_BYTES_BILLED + 1)
    with pytest.raises(LoadError, match="not run"):
        counts("chicago-contracts", ENV, bq)
    assert bq.verbs() == ["show", "query"]


@pytest.mark.parametrize("dry", ["not json", "{}", json.dumps({"statistics": {}}), "[]"])
def test_a_dry_run_answer_of_unexpected_shape_is_refused_and_nothing_runs(dry: str) -> None:
    bq = Bq()
    bq.dry_answer = dry
    with pytest.raises(LoadError, match="dry run answered unexpectedly"):
        counts("chicago-contracts", ENV, bq)
    assert bq.verbs() == ["show", "query"]


@pytest.mark.parametrize("value", NOT_A_COUNT, ids=[repr(v)[:20] for v in NOT_A_COUNT])
def test_a_dry_run_byte_count_that_is_no_ascii_digit_string_is_refused_and_nothing_runs(
    value: Any,
) -> None:
    bq = Bq()
    bq.dry_answer = json.dumps({"statistics": {"totalBytesProcessed": value}})
    with pytest.raises(LoadError, match="dry run answered unexpectedly"):
        counts("chicago-contracts", ENV, bq)
    assert bq.verbs() == ["show", "query"]


@pytest.mark.parametrize("value", NOT_A_COUNT, ids=[repr(v)[:20] for v in NOT_A_COUNT])
@pytest.mark.parametrize("cell", ["n", "c0"])
def test_a_count_cell_that_is_no_ascii_digit_string_is_refused(cell: str, value: Any) -> None:
    bq = Bq()
    bq.answer = json.dumps([{"n": "3", "c0": "2", cell: value}])
    with pytest.raises(LoadError, match="^the counts answered unexpectedly$"):
        counts("chicago-contracts", ENV, bq)


NOT_A_SUM = [v for v in NOT_A_COUNT if v is not None]


@pytest.mark.parametrize("value", NOT_A_SUM, ids=[repr(v)[:20] for v in NOT_A_SUM])
@pytest.mark.parametrize("cell", ["n", "s0", "s7"])
def test_a_digest_cell_that_is_no_ascii_digit_string_is_refused(cell: str, value: Any) -> None:
    bq = Bq()
    bq.answer = json.dumps([{"n": "3", **{f"s{i}": "2" for i in range(8)}, cell: value}])
    with pytest.raises(LoadError, match="^the digest answered unexpectedly$"):
        digest("chicago-contracts", ENV, bq)


def test_a_digest_reads_a_null_sum_as_zero_and_refuses_a_null_row_count() -> None:
    bq = Bq()
    bq.answer = json.dumps([{"n": "0", **{f"s{i}": None for i in range(8)}}])
    assert digest("chicago-contracts", ENV, bq) == Digest(0, (0,) * 8)
    bq.answer = json.dumps([{"n": None, **{f"s{i}": "1" for i in range(8)}}])
    with pytest.raises(LoadError, match="^the digest answered unexpectedly$"):
        digest("chicago-contracts", ENV, bq)


@pytest.mark.parametrize("answer", ["not json", json.dumps({"n": "1"}), json.dumps(["1"])])
def test_a_query_answer_of_unexpected_shape_is_refused(answer: str) -> None:
    bq = Bq()
    bq.answer = answer
    with pytest.raises(LoadError, match="^the query answered unexpectedly$"):
        counts("chicago-contracts", ENV, bq)


@pytest.mark.parametrize(
    "env",
    [
        {"GCP_PROJECT": "another-lab", "LEDGER_LAB_BQ_LOCATION": "us-central1"},
        {**ENV, "GCP_PROJECT": "another-lab"},
    ],
    ids=["foreign-only", "foreign-and-lab"],
)
def test_a_query_outside_the_lab_project_is_refused_before_any_call(env: dict[str, str]) -> None:
    bq = Bq()
    for ask in (counts, digest, lambda n, e, r: distinct_values(n, "vendor_name", e, r)):
        with pytest.raises(LoadError, match="belongs to another lab"):
            ask("chicago-contracts", env, bq)
    assert bq.calls == []


@pytest.mark.parametrize("where", ["", "US", "europe-west2", "us-east4"])
def test_a_query_in_a_location_outside_the_free_regions_is_refused_before_any_call(
    where: str,
) -> None:
    bq = Bq()
    env = {**ENV, "LEDGER_LAB_BQ_LOCATION": where}
    for ask in (counts, digest, lambda n, e, r: distinct_values(n, "vendor_name", e, r)):
        with pytest.raises(LoadError, match="LEDGER_LAB_BQ_LOCATION must be"):
            ask("chicago-contracts", env, bq)
    assert bq.calls == []


@pytest.mark.parametrize(
    "name", ["another-lab.billing.export", "billing", "payments", "chicago_contracts", ""]
)
def test_a_query_names_no_table_but_a_recorded_landed_files(name: str) -> None:
    bq = Bq()
    for ask in (counts, digest, lambda n, e, r: distinct_values(n, "vendor_name", e, r)):
        with pytest.raises(LoadError, match="not a landed file"):
            ask(name, ENV, bq)
    assert bq.calls == []


@pytest.mark.parametrize(
    "column",
    [
        "x` FROM `another-lab.billing.export` --",
        "vendor_name` FROM `another-lab.billing.export",
        "vendor_name`",
        "VENDOR_NAME",
        "*",
        "",
    ],
)
def test_a_column_the_tables_schema_does_not_name_is_refused_before_any_query(
    column: str,
) -> None:
    bq = Bq()
    with pytest.raises(LoadError, match="has no column"):
        distinct_values("chicago-contracts", column, ENV, bq)
    assert bq.verbs() == ["show"]


def landed_table(tmp_path: Path, rows: list[tuple[Any, ...]], name: str = "local") -> Path:
    path = tmp_path / name
    con = duckdb.connect()
    con.execute("CREATE TABLE t (vendor_name VARCHAR, amount BIGINT, paid BOOLEAN, note VARCHAR)")
    con.executemany("INSERT INTO t VALUES (?, ?, ?, ?)", rows)
    con.execute(f"COPY t TO '{path}' (FORMAT parquet)")
    return path


ROWS = [
    ("ACME LLC", 10, True, 'a, "quoted"\nline'),
    ("person:" + "a" * 64, 20, None, None),
    ("X LTD", None, False, ""),
    ("Échelle SA", -5, True, "tab\there"),
]


def test_every_query_names_only_the_lab_projects_landed_table(tmp_path: Path) -> None:
    bq = Bq()
    bq.tables["chicago_contracts"] = landed_table(tmp_path, ROWS)
    counts("chicago-contracts", ENV, bq)
    distinct_values("chicago-contracts", "note", ENV, bq)
    digest("chicago-contracts", ENV, bq)
    asked = queries(bq)
    assert len(asked) == 6
    for sql in asked:
        tables = {
            (t.catalog, t.db, t.name)
            for t in sqlglot.parse_one(sql, read="bigquery").find_all(exp.Table)
        }
        assert tables == {(PROJECT, DATASET, "chicago_contracts")}, tables


def test_the_bigquery_digest_is_the_local_digest_of_the_same_rows(tmp_path: Path) -> None:
    """The BigQuery digest's SQL, read by sqlglot and run on DuckDB, gives the local digest of the
    same file: one rule on both sides (nulls, empty text, quotes, new lines, accents, negative
    numbers, booleans)."""
    path = landed_table(tmp_path, ROWS)
    bq = Bq()
    bq.tables["chicago_contracts"] = path
    assert digest("chicago-contracts", ENV, bq) == local_digest(path)


def test_the_digest_holds_no_column_type_without_a_rule() -> None:
    with pytest.raises(LoadError, match="no digest rule for a FLOAT column"):
        bigquery_digest_sql("`t`", [("`x`", "FLOAT")])


def guarded[T](what: str, env: Mapping[str, str], call: Callable[[], T]) -> T | str:
    """`call()`'s answer, or on an `Exception` fixed text: `what`, the exception's type and, for a
    `LoadError`, its message with each value of `env` but the location, stripped of surrounding
    whitespace, replaced by `<hidden>`; one pattern holds the values, longest first, so a value
    that holds another (a session project that holds the lab's) is replaced whole. An
    interrupt (a `BaseException` that is no `Exception`) is raised again as a new exception of its
    type with no arguments, after the `except` block, so its traceback starts in this frame and
    holds no frame of `call` and no exception context; this frame's locals are then `what`, `env`,
    `call`, the type and the new exception."""
    interrupted: type[BaseException] | None = None
    try:
        return call()
    except Exception as error:
        message = str(error) if isinstance(error, LoadError) else ""
        values = {v.strip() for k, v in env.items() if k != LOCATION_VARIABLE} - {""}
        if values:
            longest = sorted(values, key=len, reverse=True)
            message = re.sub("|".join(map(re.escape, longest)), "<hidden>", message)
        return f"{what}: {type(error).__name__}" + (f": {message}" if message else "")
    except BaseException as error:
        interrupted = type(error)
    try:
        again = interrupted()
    except TypeError:
        again = BaseException(interrupted.__name__)
    raise again


def shown_table(name: str, env: Mapping[str, str], run: Any) -> Any:
    """The loaded table's metadata, in the project and location `lab` gives, so another lab's
    session is refused before the call."""
    project, where = lab(env)
    return table_of(name, run(show_command(project, where, table_name(name))), project)


def check_table(name: str, path: Path, env: Mapping[str, str], run: Any) -> str | None:
    """Why the loaded table of a landed file differs from the local file, or None: its rows from
    the table's metadata, its columns, each column's non-null cells, and its content digest. The
    three BigQuery calls run through `guarded`, so a failed one comes back as its fixed text, and
    this frame holds no project of its own; `env` is the caller's (`lab_env` in the live test)."""
    loaded = guarded("not loaded", env, lambda: shown_table(name, env, run))
    if isinstance(loaded, str):
        return loaded
    con = duckdb.connect()
    local_columns = [
        (str(c), BIGQUERY_TYPE[str(t)])
        for c, t, *_ in con.execute(
            "DESCRIBE SELECT * FROM read_parquet(?)", [str(path)]
        ).fetchall()
    ]
    if list(loaded.columns) != local_columns:
        return "its columns differ"
    cells = ", ".join(f'count("{c}")' for c, _ in local_columns)
    local = con.execute(f"SELECT count(*), {cells} FROM read_parquet(?)", [str(path)]).fetchone()  # noqa: S608
    assert local is not None
    asked = guarded("its counts failed", env, lambda: counts(name, env, run))
    if isinstance(asked, str):
        return asked
    rows, cells_loaded = asked
    if loaded.rows != local[0] or rows != local[0]:
        return "its row count differs"
    if cells_loaded != list(local[1:]):
        return "a column's non-null count differs"
    found = guarded("its digest failed", env, lambda: digest(name, env, run))
    if isinstance(found, str):
        return found
    if found != local_digest(path):
        return "its content digest differs"
    return None


@pytest.mark.parametrize(
    ("mutate", "why"),
    [
        (lambda rows: rows, None),
        (lambda rows: random.Random(7).sample(rows, len(rows)), None),  # noqa: S311
        (lambda rows: [("ACME LLX", *rows[0][1:]), *rows[1:]], "its content digest differs"),
        (
            lambda rows: [
                (rows[0][0], rows[2][1], *rows[0][2:]),
                rows[1],
                (rows[2][0], rows[0][1], *rows[2][2:]),
                rows[3],
            ],
            "its content digest differs",
        ),
        (
            lambda rows: [
                (rows[0][0], rows[3][1], *rows[0][2:]),
                rows[1],
                rows[2],
                (rows[3][0], rows[0][1], *rows[3][2:]),
            ],
            "its content digest differs",
        ),
        (lambda rows: [*rows[:3], rows[0]], "its content digest differs"),
        (lambda rows: rows[:-1], "its row count differs"),
    ],
    ids=[
        "the same rows",
        "the same rows shuffled",
        "one changed cell",
        "a null swapped between rows",
        "a pair of cells swapped between rows",
        "a row dropped and another doubled",
        "a row dropped",
    ],
)
def test_a_loaded_table_matches_its_local_file_only_when_it_holds_the_same_rows(
    tmp_path: Path,
    mutate: Callable[[list[tuple[Any, ...]]], list[tuple[Any, ...]]],
    why: str | None,
) -> None:
    local = landed_table(tmp_path, ROWS)
    bq = Bq()
    bq.tables["chicago_contracts"] = landed_table(tmp_path, mutate(list(ROWS)), "loaded")
    assert check_table("chicago-contracts", local, ENV, bq) == why


def test_a_digest_tells_a_cell_from_its_neighbour_and_a_null_from_text(tmp_path: Path) -> None:
    """Each cell's text carries its length and a null its own mark, so text moved across a column
    boundary, or a null for the text `N`, changes the row."""
    digests = [
        local_digest(landed_table(tmp_path, [row], f"f{i}"))
        for i, row in enumerate(
            [("ab", 1, True, "c"), ("a", 1, True, "bc"), ("N", 1, True, None), (None, 1, True, "N")]
        )
    ]
    assert len(set(digests)) == 4
    assert all(isinstance(d, Digest) and d.rows == 1 for d in digests)


def test_a_digest_leaves_out_the_column_names_and_types(tmp_path: Path) -> None:
    """The stated limit: an integer 10 and the text `10`, under another column name, give one
    digest, so the live check compares the schema's names and types beside it."""
    paths = []
    for i, (kind, value) in enumerate([("BIGINT", 10), ("VARCHAR", "10")]):
        path = tmp_path / f"t{i}"
        con = duckdb.connect()
        con.execute(f"CREATE TABLE t (c{i} {kind})")
        con.execute("INSERT INTO t VALUES (?)", [value])
        con.execute(f"COPY t TO '{path}' (FORMAT parquet)")
        paths.append(path)
    assert local_digest(paths[0]) == local_digest(paths[1])


# The live comparisons: the loaded tables against the local landing.

# The landed tables' columns that hold a payee's or supplier's name, hashed as a person's where
# the landing step found no evidence of an organisation.
HASHED_NAMES = {
    "chicago-contracts": "vendor_name",
    "chicago-payments": "vendor_name",
    "uk-transport-spend-2025": "supplier",
}
HASH = re.compile(r"^person:[0-9a-f]{64}$")


class LabEnv(Mapping[str, str]):
    """The variables the live comparisons read (the lab's project, the session's project, the
    location and the hash key), copied from `source` and nothing else, with a `repr` that names
    the variables present and shows no value."""

    KEYS = ("LEDGER_LAB_GCP_PROJECT", "GCP_PROJECT", LOCATION_VARIABLE, KEY_VARIABLE)

    def __init__(self, source: Mapping[str, str]) -> None:
        self._values = {key: source[key] for key in self.KEYS if key in source}

    def __getitem__(self, key: str) -> str:
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __repr__(self) -> str:
        return f"LabEnv({', '.join(self._values)}; values hidden)"


def lab_env() -> LabEnv:
    return LabEnv(os.environ)


def test_the_lab_env_holds_only_the_variables_read_and_shows_no_value() -> None:
    source = {**ENV_KEYED, "GCP_PROJECT": "another-lab", "SOME_TOKEN": "an-invented-token"}
    env = LabEnv(source)
    assert dict(env) == {k: v for k, v in source.items() if k != "SOME_TOKEN"}
    shown = repr(env) + str(env)
    assert all(value not in shown for value in source.values())


def shows_no_value(text: str, key: str, token: str) -> bool:
    """Whether `text` holds neither invented value nor this process's value of any of
    `LabEnv.KEYS`; the values are read inside the expression, by name, so no frame of this
    function binds one."""
    return (
        key not in text
        and token not in text
        and not any(
            os.environ.get(name, "").strip() and os.environ[name] in text for name in LabEnv.KEYS
        )
    )


def lab_env_shape(key: str, token: str) -> tuple[str, bool, bool, bool]:
    """What `lab_env()` gives, as its type's name, whether it reads the hash key, whether it leaves
    the token out and whether its `repr` and `str` pass `shows_no_value`. Anything but a `LabEnv`
    comes back at once as its type's name and three falses, before any other use of it, and no
    local of this function holds a value of this process's environment."""
    env = lab_env()
    if not isinstance(env, LabEnv):
        return (type(env).__name__, False, False, False)
    return (
        type(env).__name__,
        env.get(KEY_VARIABLE) == key,
        "SOME_TOKEN" not in env,
        shows_no_value(repr(env) + str(env), key, token),
    )


def test_the_live_tests_env_is_a_lab_env_of_the_process_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`lab_env()` itself, which the live tests call: a `LabEnv` of this process's environment, so
    its `repr` shows no value whichever frame holds it. The leak probe of `test_names` does not
    show this: that test passes `lab_env()` straight into `hashed_name_counts`, so under a
    `LoadError` no frame pytest prints holds it."""
    key, token = f"invented-key-{secrets.token_hex(16)}", f"invented-{secrets.token_hex(8)}"
    monkeypatch.setenv(KEY_VARIABLE, key)
    monkeypatch.setenv("SOME_TOKEN", token)
    assert lab_env_shape(key, token) == ("LabEnv", True, True, True)


def planted_hits(planted: Sequence[str], key: str, token: str) -> int:
    """How many of `planted` the outcome of `lab_env_shape` shows: its answer's `repr`, or, when it
    raises, its failure as pytest prints it under `-l` (long style, with each frame's locals).
    Only the count comes back."""
    try:
        text = repr(lab_env_shape(key, token))
    except Exception:  # noqa: BLE001
        text = str(pytest.ExceptionInfo.from_current().getrepr(showlocals=True, style="long"))
    return sum(value in text for value in planted)


@pytest.mark.parametrize(
    "regression",
    [lambda: None, object, lambda: list(os.environ.items()), lambda: tuple(os.environ)],
    ids=["none", "an object", "the environment's items", "the environment's names"],
)
def test_a_lab_env_regression_shows_no_value_through_the_shape_helper(
    monkeypatch: pytest.MonkeyPatch, regression: Callable[[], object]
) -> None:
    """With `lab_env` broken to give something other than a `LabEnv`, `lab_env_shape` shows no
    value of the environment: neither in its answer nor, should it raise, in the failure pytest
    prints with each frame's locals. The planted values are invented and set on two of
    `LabEnv.KEYS`, the hash key and the location."""
    key, token = f"invented-key-{secrets.token_hex(16)}", f"invented-{secrets.token_hex(8)}"
    location = f"invented-region-{secrets.token_hex(6)}"
    monkeypatch.setenv(KEY_VARIABLE, key)
    monkeypatch.setenv(LOCATION_VARIABLE, location)
    monkeypatch.setenv("SOME_TOKEN", token)
    monkeypatch.setitem(globals(), "lab_env", regression)
    assert planted_hits([key, location, token], key, token) == 0


def local_files() -> list[str]:
    names = sorted(p.name for p in landing_path().iterdir() if p.is_file())
    assert names, "the local landing is empty"
    return names


@pytest.mark.warehouse
def test_each_landed_table_holds_the_same_rows_as_its_local_file() -> None:
    """Every file of the local landing is a `landed` table with as many rows, the same columns in
    the same order and of the matching types, as many non-null cells in each column, and the same
    content digest, so the same rows in some order. Prints each file's result, then fails on any
    that differs or is not loaded."""
    env = lab_env()
    found = {
        name: check_table(name, landing_path() / name, env, run_tool) for name in local_files()
    }
    print("landed file, result:")
    for name, why in found.items():
        print(f"{name} {why or 'the same rows'}")
    assert [name for name, why in found.items() if why] == []


def hashed_name_counts(
    name: str, column: str, path: Path, env: Mapping[str, str], run: Any
) -> dict[str, int] | str:
    """How the loaded column of names compares with the local file's: values missing and extra,
    hashes and clear values, malformed hashes, and clear values whose keyed hash as a person is one
    of the column's hashes. Only counts come back, so the caller's frame holds no value; the work
    runs through `guarded`, so a failure comes back as its fixed text and the traceback of an
    interrupt holds no frame that holds the values."""
    return guarded("hashed names", env, lambda: _hashed_name_counts(name, column, path, env, run))


def _hashed_name_counts(
    name: str, column: str, path: Path, env: Mapping[str, str], run: Any
) -> dict[str, int]:
    local = {
        None if v is None else str(v)
        for (v,) in duckdb.connect()
        .execute(f'SELECT DISTINCT "{column}" FROM read_parquet(?)', [str(path)])  # noqa: S608
        .fetchall()
    }
    loaded = distinct_values(name, column, env, run)
    hashed = {v for v in loaded if v is not None and v.startswith("person:")}
    clear = {v for v in loaded if v is not None and not v.startswith("person:")}
    as_person = person_hasher(env)
    return {
        "missing": len(local - loaded),
        "extra": len(loaded - local),
        "hashed": len(hashed),
        "clear": len(clear),
        "malformed": sum(1 for v in hashed if not HASH.match(v)),
        "in_clear": len({as_person(v) for v in clear} & hashed),
    }


def test_the_hashed_name_counts_are_counts_and_an_error_is_its_type(tmp_path: Path) -> None:
    """Four tables against one local file: the same rows; one more row holding a hash of a clear
    name; one row fewer and a malformed hash; and two failures, a refused query and a missing
    file. Each answer is integers or the failure's fixed text."""
    path = landed_table(tmp_path, ROWS)
    bq = Bq()
    as_person = person_hasher(ENV_KEYED)
    expected = [
        (ROWS, (0, 0, 1, 3, 0, 0)),
        ([*ROWS, (as_person("ACME LLC"), 1, True, None)], (0, 1, 2, 3, 0, 1)),
        ([*ROWS[:3], ("person:" + "z" * 64, 1, True, None)], (1, 1, 2, 2, 1, 0)),
    ]
    for i, (rows, (missing, extra, hashed, clear, malformed, in_clear)) in enumerate(expected):
        bq.tables["chicago_contracts"] = landed_table(tmp_path, rows, f"loaded{i}")
        found = hashed_name_counts("chicago-contracts", "vendor_name", path, ENV_KEYED, bq)
        assert isinstance(found, dict)
        assert all(type(n) is int for n in found.values())
        assert found == {
            "missing": missing,
            "extra": extra,
            "hashed": hashed,
            "clear": clear,
            "malformed": malformed,
            "in_clear": in_clear,
        }
    bq.fail_on = "query"
    assert hashed_name_counts("chicago-contracts", "vendor_name", path, ENV_KEYED, bq) == (
        "hashed names: LoadError: cannot dry-run the query (bq exit 2)"
    )
    absent = tmp_path / "absent"
    assert hashed_name_counts("chicago-contracts", "vendor_name", absent, ENV_KEYED, bq) == (
        "hashed names: IOException"
    )


def test_a_load_error_message_has_the_project_and_key_hidden() -> None:
    def refuse() -> None:
        raise LoadError(f"{PROJECT} {ENV_KEYED[KEY_VARIABLE]} us-central1")

    assert guarded("it", ENV_KEYED, refuse) == "it: LoadError: <hidden> <hidden> us-central1"


@pytest.mark.parametrize(
    ("env", "message", "seen"),
    [
        (
            {"LEDGER_LAB_GCP_PROJECT": "lab", "GCP_PROJECT": "lab-session-7"},
            "x lab-session-7 y lab",
            "x <hidden> y <hidden>",
        ),
        (
            {"GCP_PROJECT": "lab-session-7", "LEDGER_LAB_GCP_PROJECT": "lab"},
            "x lab-session-7 y lab",
            "x <hidden> y <hidden>",
        ),
        (
            {"LEDGER_LAB_GCP_PROJECT": "invented-lab", KEY_VARIABLE: "invented-lab-k3y-0123abcd"},
            "key invented-lab-k3y-0123abcd",
            "key <hidden>",
        ),
        ({"LEDGER_LAB_GCP_PROJECT": "invented-proj\n"}, "x invented-proj y", "x <hidden> y"),
        ({"LEDGER_LAB_GCP_PROJECT": " invented-proj "}, "x invented-proj y", "x <hidden> y"),
        ({"GCP_PROJECT": "another-lab-42"}, "in another-lab-42", "in <hidden>"),
        ({"LEDGER_LAB_GCP_PROJECT": "a.b"}, "axb a.b", "axb <hidden>"),
        ({"LEDGER_LAB_GCP_PROJECT": "  ", LOCATION_VARIABLE: "us-central1"}, "us-central1", None),
    ],
    ids=[
        "the session project holds the lab's",
        "the same, the lab's first",
        "the key holds the project",
        "a project with a new line",
        "a project padded with spaces",
        "the session project alone",
        "a value with a pattern character",
        "a blank value and the location",
    ],
)
def test_a_padded_or_nested_value_is_hidden_whole(
    env: dict[str, str], message: str, seen: str | None
) -> None:
    def refuse() -> None:
        raise LoadError(message)

    assert guarded("it", LabEnv({LOCATION_VARIABLE: "us-central1", **env}), refuse) == (
        f"it: LoadError: {seen or message}"
    )
    assert guarded("it", env, refuse) == f"it: LoadError: {seen or message}"


# A probe of the live tests' path, run by pytest in a subprocess. The bq stand-in is `Bq` over the
# probe's landed file, so each call answers as a loaded table that holds the file's rows would, but
# one: the `show`, the counts query, the digest query or the distinct-values query (each the run,
# not its dry run). That call raises a `LoadError` whose message holds the invented project and key,
# or interrupts, after writing its name to the file `PROBE_REACHED` names. The invented project and
# key come from the subprocess's environment, so the probe's source holds neither.
PROBE = """
import os
import sys
from pathlib import Path

sys.path[:0] = [{paths}]

import duckdb
import ingest.fetch
import pytest
import test_load as T
from ingest.load import LoadError

FAIL_AT = {fail_at!r}
INTERRUPT = {interrupt}
BQ = []


def call_of(command):
    verb = T.verb_of(command)
    if verb != "query" or "--dry_run" in command:
        return verb
    if "SHA256" in command[-1]:
        return "digest"
    return "names" if "SELECT DISTINCT" in command[-1] else "counts"


def answer(command):
    if not BQ:
        T.PROJECT = os.environ["LEDGER_LAB_GCP_PROJECT"]
        BQ.append(T.Bq())
        BQ[0].tables["chicago_contracts"] = T.landing_path() / "chicago-contracts"
    if call_of(command) == FAIL_AT:
        Path(os.environ["PROBE_REACHED"]).write_text(FAIL_AT)
        if INTERRUPT:
            raise KeyboardInterrupt
        project, key = os.environ["LEDGER_LAB_GCP_PROJECT"], os.environ["LEDGER_LAB_HASH_KEY"]
        raise LoadError(f"denied in {{project}} for {{key}}")
    return BQ[0](command)


@pytest.fixture
def landing(tmp_path, monkeypatch):
    con = duckdb.connect()
    con.execute("CREATE TABLE t AS SELECT 'ACME LLC' AS vendor_name")
    con.execute(f"COPY t TO '{{tmp_path / 'chicago-contracts'}}' (FORMAT parquet)")
    monkeypatch.setattr(T, "run_tool", answer)
    monkeypatch.setattr(T, "landing_path", lambda: tmp_path)
    monkeypatch.setattr(ingest.fetch, "landing_path", lambda: tmp_path)


def test_rows(landing):
    T.test_each_landed_table_holds_the_same_rows_as_its_local_file()


def test_names(landing):
    T.test_no_name_the_landing_hashes_is_in_clear_in_the_warehouse("chicago-contracts")
"""

# The stand-in's message as `guarded` reports it with the project and key hidden.
HIDDEN = "LoadError: denied in <hidden> for <hidden>"


ALL = ["--tb=long", "-l", "--full-trace"]


# An interrupt runs only under `ALL`: without `--full-trace` pytest prints a `KeyboardInterrupt` as
# one line with no frame and no local, so the other flag sets would show the hiding nothing.
@pytest.mark.parametrize(
    ("interrupt", "flags"),
    [(False, []), (False, ALL), (False, ["-vv", "-l"]), (True, ALL)],
    ids=["load error-default", "load error-all", "load error-vvl", "interrupt-all"],
)
@pytest.mark.parametrize(
    ("test", "fail_at", "what"),
    [
        ("test_rows", "show", "not loaded"),
        ("test_rows", "counts", "its counts failed"),
        ("test_rows", "digest", "its digest failed"),
        ("test_names", "names", "hashed names"),
    ],
)
def test_a_failing_live_test_prints_no_project_and_no_key(
    tmp_path: Path, flags: list[str], interrupt: bool, test: str, fail_at: str, what: str
) -> None:
    """Each live test's path, made to fail at its first `show`, its counts query, its digest query
    or its distinct-values query (the run, not the dry run), by a `LoadError` whose message holds
    the invented project and key under pytest's default traceback, under `--tb=long -l
    --full-trace` and under `-vv -l`, and by an interrupt under `--tb=long -l --full-trace` only
    (without `--full-trace` pytest prints no frame of an interrupt): the failing call is reached,
    the output shows the failure (for a `LoadError`, its message with both hidden) and holds
    neither the invented project nor the invented key."""
    project = f"invented-{secrets.token_hex(6)}"
    key = f"invented-key-{secrets.token_hex(16)}"
    paths = ", ".join(repr(str(p)) for p in (REPO, REPO / "tests", REPO / "tests" / "ingest"))
    probe = tmp_path / "probe_live.py"
    probe.write_text(PROBE.format(paths=paths, fail_at=fail_at, interrupt=interrupt))
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    reached = tmp_path / "reached"
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "LEDGER_LAB_GCP_PROJECT": project,
        LOCATION_VARIABLE: "us-central1",
        KEY_VARIABLE: key,
        "PROBE_REACHED": str(reached),
    }
    ran = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-c",
            str(tmp_path / "pytest.ini"),
            "--rootdir",
            str(tmp_path),
            f"{probe}::{test}",
            *flags,
        ],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
        timeout=120,
        check=False,
    )
    out = ran.stdout + ran.stderr
    seen = "KeyboardInterrupt" if interrupt else f"{what}: {HIDDEN}"
    assert reached.exists() and reached.read_text() == fail_at, out[-2000:]
    assert ran.returncode != 0 and seen in out, out[-2000:]
    assert (out.count(project), out.count(key)) == (0, 0)


@pytest.mark.warehouse
@pytest.mark.parametrize("name", sorted(HASHED_NAMES))
def test_no_name_the_landing_hashes_is_in_clear_in_the_warehouse(name: str) -> None:
    """The loaded column of names holds the local landing's distinct values, none missing and none
    extra, holds hashes and clear values, every hash well formed, and no clear value whose keyed
    hash as a person is one of the column's hashes: the landing's leak hunts (`pytest -m landed`)
    hold for the table. The comparison runs in `hashed_name_counts`, so this test's frame and its
    failure messages hold counts only."""
    column = HASHED_NAMES[name]
    found = hashed_name_counts(name, column, landed_file(name), lab_env(), run_tool)
    assert isinstance(found, dict), found
    assert (found["missing"], found["extra"]) == (0, 0), (
        f"{found['missing']} values missing, {found['extra']} extra"
    )
    assert found["hashed"] > 0 and found["clear"] > 0, (
        f"{found['hashed']} hashed, {found['clear']} clear"
    )
    assert found["malformed"] == 0, f"{found['malformed']} malformed hashes"
    assert found["in_clear"] == 0, f"{found['in_clear']} hashed names also in clear"
    hashed, clear = found["hashed"], found["clear"]
    print(f"{table_name(name)}.{column}: {hashed} hashed, {clear} clear, none a hash")
