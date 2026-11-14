"""The load step runs one Parquet load job per landed file into the `landed` dataset of the lab's
own project while it holds the landing's lock, against a stand-in for `bq` and an in-memory bucket;
it refuses another project, a location outside the free regions and a name that is no recorded
landed file before any call, and a held lock or a missing object before any load. Its queries name
only `<lab project>.landed.<table>`, built from a landed file's name and the table's own columns.
The `warehouse` tests read the loaded tables live and hold them to the local landing: the same
rows, columns and content digest, and no name the landing hashes in clear."""

import json
import random
import re
import subprocess
from collections.abc import Callable, Sequence
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
from ingest.individuals import person_hasher
from ingest.load import (
    DATASET,
    MAX_BYTES_BILLED,
    STORAGE_CAP,
    Digest,
    LoadError,
    bigquery_digest_sql,
    counts,
    digest,
    distinct_values,
    free_regions,
    load,
    load_command,
    local_digest,
    main,
    recorded_names,
    run_tool,
    show_command,
    table_name,
    table_of,
)

PROJECT = "the-lab-project"
BUCKET = f"{PROJECT}-landing"
ENV = {"LEDGER_LAB_GCP_PROJECT": PROJECT, "LEDGER_LAB_BQ_LOCATION": "us-central1"}
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
    assert bq.verbs() == ["load", "show", "ls", "show", "load", "show", "ls", "show", "show"]
    first = bq.calls[0]
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
    ],
)
def test_a_name_that_is_no_recorded_landed_file_is_refused_before_any_call(name: str) -> None:
    bq, fs = Bq(), bucket_holding(*LANDED)
    if name and "/" not in name:
        fs.pipe_file(f"{BUCKET}/{name}", b"PAR1")
    with pytest.raises(LoadError, match="not a landed file"):
        load([name], ENV, bq, fs)
    assert bq.calls == []
    assert not locked(fs) or name == LOCK


def test_the_recorded_names_are_the_fetch_kits_sources_and_sets() -> None:
    names = recorded_names()
    landed = {
        "chicago-contracts",
        "chicago-payments",
        "ecb-rates",
        "gleif-legal-entities",
        "nl-government-budget",
        "sec-annual-filings",
        "uk-transport-spend-2025",
    }
    assert landed <= names
    assert "chicago" in names and "not-a-source" not in names


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
    assert len(bq.calls) == 1
    assert PROJECT not in str(raised.value)
    assert not locked(fs)


def test_what_landed_already_holds_counts_against_the_storage_cap() -> None:
    bq = Bq(size=STORAGE_CAP // 2 + 1)
    bq.listed = {"from_an_earlier_run"}
    with pytest.raises(LoadError, match="cap: stop"):
        load(["chicago-contracts"], ENV, bq, bucket_holding(*LANDED))


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
    ],
)
def test_a_table_answer_of_another_shape_project_dataset_or_table_is_refused(answer: str) -> None:
    with pytest.raises(LoadError):
        table_of("chicago-contracts", answer, PROJECT)
    with pytest.raises(LoadError):
        load(["chicago-contracts"], ENV, lambda c: answer, bucket_holding("chicago-contracts"))


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


@pytest.mark.parametrize("answer", ["not json", json.dumps({"n": "1"}), json.dumps(["1"])])
def test_a_query_answer_of_unexpected_shape_is_refused(answer: str) -> None:
    bq = Bq()
    bq.answer = answer
    with pytest.raises(LoadError, match="^the query answered unexpectedly$"):
        counts("chicago-contracts", ENV, bq)


def test_a_query_outside_the_lab_project_is_refused_before_any_call() -> None:
    bq = Bq()
    env = {"GCP_PROJECT": "another-lab", "LEDGER_LAB_BQ_LOCATION": "us-central1"}
    for ask in (counts, digest, lambda n, e, r: distinct_values(n, "vendor_name", e, r)):
        with pytest.raises(LoadError, match="belongs to another lab"):
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


def check_table(name: str, path: Path, env: dict[str, str], run: Any) -> str | None:
    """Why the loaded table of a landed file differs from the local file, or None: its rows from
    the table's metadata, its columns, each column's non-null cells, and its content digest."""
    project, where = env["LEDGER_LAB_GCP_PROJECT"], env["LEDGER_LAB_BQ_LOCATION"]
    try:
        loaded = table_of(name, run(show_command(project, where, table_name(name))), project)
    except (subprocess.SubprocessError, LoadError):
        return "not loaded"
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
    rows, cells_loaded = counts(name, env, run)
    if loaded.rows != local[0] or rows != local[0]:
        return "its row count differs"
    if cells_loaded != list(local[1:]):
        return "a column's non-null count differs"
    if digest(name, env, run) != local_digest(path):
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


# The live comparisons: the loaded tables against the local landing.

# The landed tables' columns that hold a payee's or supplier's name, hashed as a person's where
# the landing step found no evidence of an organisation.
HASHED_NAMES = {
    "chicago-contracts": "vendor_name",
    "chicago-payments": "vendor_name",
    "uk-transport-spend-2025": "supplier",
}
HASH = re.compile(r"^person:[0-9a-f]{64}$")


def lab_env() -> dict[str, str]:
    import os

    return dict(os.environ)


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


@pytest.mark.warehouse
@pytest.mark.parametrize("name", sorted(HASHED_NAMES))
def test_no_name_the_landing_hashes_is_in_clear_in_the_warehouse(name: str) -> None:
    """The loaded column of names holds exactly the local landing's values, every hash well
    formed, and no clear value whose keyed hash as a person is one of the column's hashes: the
    landing's leak hunts (`pytest -m landed`) hold for the table. Values stay in memory: a
    failure says how many, never which."""
    column, env = HASHED_NAMES[name], lab_env()
    path = str(landed_file(name))
    local = {
        None if v is None else str(v)
        for (v,) in duckdb.connect()
        .execute(f'SELECT DISTINCT "{column}" FROM read_parquet(?)', [path])  # noqa: S608
        .fetchall()
    }
    loaded = distinct_values(name, column, env)
    missing, extra = len(local - loaded), len(loaded - local)
    assert (missing, extra) == (0, 0), f"{missing} values missing, {extra} extra"
    hashed = {v for v in loaded if v is not None and v.startswith("person:")}
    clear = {v for v in loaded if v is not None and not v.startswith("person:")}
    assert hashed and clear
    malformed = sum(1 for v in hashed if not HASH.match(v))
    assert malformed == 0, f"{malformed} malformed hashes"
    as_person = person_hasher()
    in_clear = len({as_person(v) for v in clear} & hashed)
    assert in_clear == 0, f"{in_clear} hashed names also in clear"
    print(f"{table_name(name)}.{column}: {len(hashed)} hashed, {len(clear)} clear, none a hash")
