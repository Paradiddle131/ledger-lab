"""`uv run python -m ingest.load <landed file>...`: load landed files from the landing bucket into
the warehouse's `landed` dataset, one BigQuery load job per file.

Each name must be one a landing step writes a file under (`landed_names`: a source's own step in
`fetch.STEPS`, a set whose step returns one file, or a member of a set whose step returns files by
member name) and an object at the root of the lab's landing bucket (`bucket.py`). A member of a set
whose step returns one file is refused: a quarter of the securities filings, which the fetch kit
would land alone as the publisher's file, and a month of the transport spend, which the fetch kit
refuses to land alone because it names people and has no step of its own. What is checked is the
name; the object's bytes are whatever the bucket holds under it. Each landed file is Parquet its
step wrote: text columns as the publishers' text, typed columns only where the step typed them; the
load keeps the file's own schema, so no delimiter, quoting, header or encoding rule can split a row
differently from the landing. Column names are kept as they are; BigQuery refuses a Parquet column
whose name holds a dot, so the steps land none (the legal-entity register's `Entity.LegalName` lands
as `Entity_LegalName`). A file lands as the table named after it with underscores for its hyphens
(`chicago-contracts` as `landed.chicago_contracts`), replaced whole (WRITE_TRUNCATE) so a re-run is
idempotent, and a job that meets a single bad record fails and leaves the table as it was. Each
file's table is loaded in turn; a failure stops before the next.

Refused before any call: a project other than the one `LEDGER_LAB_GCP_PROJECT` names (read through
`platform/gcp_project.py`, never the session's `GCP_PROJECT`), every project in a session that
carries a `GCP_PROJECT`, whose credential is another lab's, a location in
`LEDGER_LAB_BQ_LOCATION` outside the free-tier regions `platform/gcp/variables.tf` allows, and a
name that is not in `landed_names` (hidden objects and the lock among them). Refused before any
load: a landing bucket whose lock another landing or load holds, and a name the bucket does not
hold. The load holds the landing's own lock (`Bucket.lock`) for its whole run, so no landing
replaces an object while the set loads and no load starts during a landing; it releases the lock
when it ends, on a failure too, and a killed load leaves it behind, as a killed landing does, for an
operator to delete by hand once nothing runs.

Load jobs from Cloud Storage bill nothing, and the loader runs no streaming insert. Before the first
load and after each load it lists `landed` (`bq ls`) and describes each listed table (`bq show`),
which bill nothing, and refuses to go on when the listing leaves out a table this run has loaded (an
empty listing included), when a `bq show` answer gives a row or byte count that is not a JSON
string of ASCII digits (`count`; a negative count is one such), or when the stored total is above
`STORAGE_CAP` bytes, far inside BigQuery's free 10 GiB; a run that starts above the cap loads
nothing. A table this run loaded counts at the larger of the bytes its `bq show`
gave right after its load and the bytes the later `bq show` gives. A table this run did not load
counts only as the listing and its `bq show` report it: the cap trusts `bq ls` for those.

The live comparisons ask BigQuery three things of one landed table, each built here from the file's
name and, for a column, its name as the table's own schema gives it, so a caller names no project,
dataset or table: the row and non-null counts (`counts`), one column's distinct values
(`distinct_values`) and a content digest (`digest`). Each query is dry-run first and refused above
`MAX_BYTES_BILLED`, which also caps the job.

The digest (`digest` on the BigQuery table, `local_digest` on the local file) covers the cells'
values in column order and the row count, and not the order of the rows, the column names or the
column types: an INTEGER 10 and a STRING '10' give one text. The live `warehouse` test
(`check_table` in its tests) compares the column names and types from the table's schema separately.
Each cell becomes text by one rule (a null is `N`; any other value is `V`, its UTF-8 length in
bytes, `:` and its text, where an integer is its decimal digits and a boolean `true` or `false`), a
row is the SHA-256 of its cells' texts in column order, and the digest is the row count with the sum
of each of the eight 32-bit slices of every row's hash, so a changed cell, two cells swapped between
rows, or a row dropped and another doubled changes it, the cases the tests run. It is a hash: equal
digests are strong evidence of the same rows, not proof. Only the types the landing writes have a
rule (text, integers, booleans); a column of another type is refused.

The calls go through `bq`, whose credential the session's network proxy adds; a failure is reported
by the file and `bq`'s exit status only, since its messages name the project.
"""

import json
import re
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, get_origin, get_type_hints

from fsspec import AbstractFileSystem

from ingest.bucket import LOCK, SCHEME, Bucket, BucketError, bucket_name, cloud_storage, lab_project

DATASET = "landed"
LOCATION_VARIABLE = "LEDGER_LAB_BQ_LOCATION"
VARIABLES_TF = Path(__file__).resolve().parent.parent / "platform" / "gcp" / "variables.tf"
# A landed file's name: lower-case words and digits joined by hyphens, as the fetch kit names them.
LANDED_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
# The landing is about 52 MB of Parquet; the `landed` dataset stays far below this.
STORAGE_CAP = 2 * 1024**3
# Every query's billed bytes, far inside the free 1 TiB a month and the daily query cap.
MAX_BYTES_BILLED = 1_000_000_000
# The digest: a row hash's 64 hex digits as eight slices of eight (32 bits each); a sum of one
# slice over a million rows stays below 2**52, inside BigQuery's INT64 and DuckDB's BIGINT.
SLICES = 8
SLICE = 8

Runner = Callable[[Sequence[str]], str]


class LoadError(Exception):
    """A load was refused or failed; the message names files and tables, never the project."""


def run_tool(command: Sequence[str]) -> str:
    return subprocess.run(command, capture_output=True, text=True, check=True).stdout  # noqa: S603


def free_regions() -> tuple[str, ...]:
    """The free-tier regions `platform/gcp/variables.tf`'s `region` validation allows."""
    found = re.search(r"contains\(\[([^\]]*)\],\s*var\.region\)", VARIABLES_TF.read_text())
    if found is None:
        raise LoadError(f"no region validation in {VARIABLES_TF.name}")
    return tuple(re.findall(r'"([a-z0-9-]+)"', found.group(1)))


def location(env: Mapping[str, str]) -> str:
    """Where the jobs run: `LEDGER_LAB_BQ_LOCATION`, one of the free-tier regions."""
    where = env.get(LOCATION_VARIABLE, "").strip()
    regions = free_regions()
    if where not in regions:
        raise LoadError(
            f"{LOCATION_VARIABLE} must be the warehouse's region, one of {', '.join(regions)}"
        )
    return where


def landed_names() -> frozenset[str]:
    """The names a landing step writes a file under: each source with its own step (`STEPS`), each
    set whose step is declared to return one file (`bytes`), and the recorded members of each set
    whose step is declared to return files by member name (a `dict`). A set whose step declares
    anything else adds no name."""
    from ingest.fetch import SET_STEPS, SOURCES, STEPS, load_sources

    sources = load_sources(SOURCES)
    names = set(STEPS)
    for name, step in SET_STEPS.items():
        returns = get_type_hints(step).get("return")
        if returns is bytes:
            names.add(name)
        elif get_origin(returns) is dict:
            names |= {member for member in sources if member.startswith(f"{name}-")}
    return frozenset(names)


def table_name(name: str, landed: frozenset[str] | None = None) -> str:
    """The `landed` table a landed file loads into."""
    if not LANDED_NAME.fullmatch(name) or name not in (landed or landed_names()):
        raise LoadError(f"{name!r} is not a landed file's name")
    return name.replace("-", "_")


def bq(project: str, where: str, *command: str) -> list[str]:
    return ["bq", f"--project_id={project}", f"--location={where}", "--headless", *command]


def load_command(project: str, where: str, bucket: str, name: str) -> list[str]:
    """One load job: the landed object, as Parquet, replacing its table, with no bad record."""
    return bq(
        project,
        where,
        "--quiet",
        "load",
        "--source_format=PARQUET",
        "--replace",
        "--max_bad_records=0",
        f"{project}:{DATASET}.{table_name(name)}",
        f"{SCHEME}{bucket}/{name}",
    )


def show_command(project: str, where: str, table: str) -> list[str]:
    return bq(project, where, "--format=json", "show", f"{project}:{DATASET}.{table}")


def list_command(project: str, where: str) -> list[str]:
    return bq(project, where, "--format=json", "ls", "--max_results=10000", f"{project}:{DATASET}")


@dataclass(frozen=True)
class Table:
    """A loaded table's metadata: its rows, its stored bytes and its columns with their types."""

    name: str
    table: str
    rows: int
    bytes: int
    columns: tuple[tuple[str, str], ...]


# A count as `bq show` gives one: a JSON string of ASCII digits.
COUNT = re.compile(r"[0-9]+")


def count(value: object) -> int:
    """A `bq show` count, refused unless it is a JSON string that `COUNT` matches whole, so a JSON
    number or boolean, a sign, a fraction, whitespace, an underscore and a digit outside ASCII
    are refused."""
    if not isinstance(value, str) or not COUNT.fullmatch(value):
        raise ValueError("a count that is no string of ASCII digits")
    return int(value)


def described(
    project: str, table: str, answer: str
) -> tuple[int, int, tuple[tuple[str, str], ...]]:
    """Rows, bytes and columns of the table `bq show` describes, refused unless it is
    `<lab project>.landed.<table>` and each count passes `count`."""
    try:
        shown: Any = json.loads(answer)
        reference = shown["tableReference"]
        if (
            reference.get("projectId", project) != project
            or reference["datasetId"] != DATASET
            or reference["tableId"] != table
        ):
            raise LoadError(f"the table shown is not {DATASET}.{table}")
        columns = tuple((str(f["name"]), str(f["type"])) for f in shown["schema"]["fields"])
        rows, size = count(shown["numRows"]), count(shown["numBytes"])
        return rows, size, columns
    except (json.JSONDecodeError, KeyError, TypeError, ValueError, AttributeError) as error:
        if isinstance(error, LoadError):
            raise
        raise LoadError(
            f"{DATASET}.{table}: an answer of unexpected shape ({type(error).__name__})"
        ) from None


def table_of(name: str, answer: str, project: str) -> Table:
    table = table_name(name)
    rows, size, columns = described(project, table, answer)
    return Table(name, f"{DATASET}.{table}", rows, size, columns)


def failure(failed: Exception) -> str:
    if isinstance(failed, subprocess.CalledProcessError):
        return f"bq exit {failed.returncode}"
    if isinstance(failed, FileNotFoundError):
        return "bq not installed"
    return type(failed).__name__


def call(run: Runner, doing: str, command: Sequence[str]) -> str:
    try:
        return run(command)
    except (OSError, subprocess.SubprocessError) as failed:
        raise LoadError(f"cannot {doing} ({failure(failed)})") from None


def lab(env: Mapping[str, str]) -> tuple[str, str]:
    """The lab's project and the jobs' location, or a refusal."""
    try:
        project = lab_project(env)
    except BucketError as error:
        raise LoadError(str(error)) from None
    return project, location(env)


def stored(project: str, where: str, run: Runner, loaded: Mapping[str, int]) -> int:
    """The bytes the tables `bq ls` lists in `landed` hold, from free metadata, each table listed
    once counted once. `loaded` is the bytes each table this run loaded held right after its load,
    by table: a listing that leaves one of them out is refused, and each counts at the larger of
    that and its bytes now."""
    answer = call(run, "list the landed tables", list_command(project, where))
    try:
        listed: Any = json.loads(answer) if answer.strip() else []
        tables = {str(t["tableReference"]["tableId"]) for t in listed}
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        raise LoadError(
            f"the landed tables' list has an unexpected shape ({type(error).__name__})"
        ) from None
    left_out = sorted(set(loaded) - tables)
    if left_out:
        raise LoadError(
            f"the list of {DATASET} leaves out {', '.join(left_out)}, loaded in this run: stop"
        )
    total = 0
    for table in sorted(tables):
        shown = call(run, f"describe {DATASET}.{table}", show_command(project, where, table))
        total += max(described(project, table, shown)[1], loaded.get(table, 0))
    return total


def load(
    names: Sequence[str],
    env: Mapping[str, str],
    run: Runner = run_tool,
    fs: AbstractFileSystem | None = None,
) -> list[Table]:
    """Load each landed file into its `landed` table, in turn, holding the landing's lock, and
    describe the tables."""
    if not names:
        raise LoadError("name at least one landed file")
    if len(set(names)) != len(names):
        raise LoadError("a landed file is named twice")
    landed = landed_names()
    for name in names:
        table_name(name, landed)
    project, where = lab(env)
    try:
        root = bucket_name(f"{SCHEME}{project}-landing", env)
        landing = Bucket(fs if fs is not None else cloud_storage(), root)
        landing.lock()
    except BucketError as error:
        raise LoadError(str(error)) from None
    try:
        held = landing.names() - {LOCK}
        missing = [name for name in names if name not in held]
        if missing:
            raise LoadError(f"the landing bucket holds no {', '.join(missing)}: land it first")
        tables: list[Table] = []
        sizes: dict[str, int] = {}
        if (total := stored(project, where, run, sizes)) > STORAGE_CAP:
            raise LoadError(
                f"{DATASET} holds {total} bytes, above the {STORAGE_CAP}-byte cap"
                " before any load: stop"
            )
        for name in names:
            call(run, f"load {name}", load_command(project, where, root, name))
            shown = call(
                run, f"describe {name}'s table", show_command(project, where, table_name(name))
            )
            tables.append(table_of(name, shown, project))
            sizes[table_name(name)] = tables[-1].bytes
            total = stored(project, where, run, sizes)
            if total > STORAGE_CAP:
                raise LoadError(
                    f"{DATASET} holds {total} bytes, above the {STORAGE_CAP}-byte cap: stop"
                )
        return tables
    except BucketError as error:
        raise LoadError(str(error)) from None
    finally:
        try:
            landing.unlock()
        except BucketError as error:
            raise LoadError(f"{error}; delete {LOCK} by hand once nothing runs") from None


def _query(project: str, where: str, sql: str, run: Runner) -> list[dict[str, Any]]:
    """A standard SQL query's rows, after a dry run shows it bills at most `MAX_BYTES_BILLED`, and
    under that cap."""
    flags = ["--nouse_legacy_sql", f"--maximum_bytes_billed={MAX_BYTES_BILLED}"]
    dry = call(
        run,
        "dry-run the query",
        bq(project, where, "--format=json", "query", "--dry_run", *flags, sql),
    )
    try:
        scanned = int(json.loads(dry)["statistics"]["totalBytesProcessed"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        raise LoadError("the dry run answered unexpectedly") from None
    if scanned > MAX_BYTES_BILLED:
        raise LoadError(f"the query would scan {scanned} bytes, above the cap; not run")
    answer = call(
        run,
        "run the query",
        bq(project, where, "--format=json", "query", "--max_rows=10000000", *flags, sql),
    )
    try:
        rows = json.loads(answer) if answer.strip() else []
    except json.JSONDecodeError:
        raise LoadError("the query answered unexpectedly") from None
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise LoadError("the query answered unexpectedly")
    return rows


def _one_row(project: str, where: str, sql: str, run: Runner) -> dict[str, Any]:
    """The one row of a query that answers one, refused when the answer holds another number."""
    rows = _query(project, where, sql, run)
    if len(rows) != 1:
        raise LoadError(f"the query answered {len(rows)} rows, not one")
    return rows[0]


@dataclass(frozen=True)
class Asked:
    """One landed table to ask of: where it is and its columns, from its own `bq show`."""

    project: str
    where: str
    table: str
    columns: tuple[tuple[str, str], ...]

    @property
    def reference(self) -> str:
        return f"`{self.project}.{DATASET}.{self.table}`"

    def column(self, name: str) -> str:
        """A column of the table, quoted; refused unless the table's schema names it."""
        if name not in {c for c, _ in self.columns} or "`" in name or "\\" in name:
            raise LoadError(f"{DATASET}.{self.table} has no column {name!r}")
        return f"`{name}`"


def asked(name: str, env: Mapping[str, str], run: Runner) -> Asked:
    project, where = lab(env)
    table = table_name(name)
    shown = call(run, f"describe {name}'s table", show_command(project, where, table))
    return Asked(project, where, table, described(project, table, shown)[2])


def counts(name: str, env: Mapping[str, str], run: Runner = run_tool) -> tuple[int, list[int]]:
    """The loaded table's rows and each column's non-null cells, in column order."""
    t = asked(name, env, run)
    cells = ", ".join(f"COUNT({t.column(c)}) AS c{i}" for i, (c, _) in enumerate(t.columns))
    row = _one_row(t.project, t.where, f"SELECT COUNT(*) AS n, {cells} FROM {t.reference}", run)  # noqa: S608
    try:
        return int(row["n"]), [int(row[f"c{i}"]) for i in range(len(t.columns))]
    except (KeyError, TypeError, ValueError):
        raise LoadError("the counts answered unexpectedly") from None


def distinct_values(
    name: str, column: str, env: Mapping[str, str], run: Runner = run_tool
) -> set[str | None]:
    """One column's distinct values in the loaded table."""
    t = asked(name, env, run)
    sql = f"SELECT DISTINCT {t.column(column)} AS v FROM {t.reference}"  # noqa: S608
    try:
        return {
            None if r["v"] is None else str(r["v"]) for r in _query(t.project, t.where, sql, run)
        }
    except KeyError:
        raise LoadError("the distinct values answered unexpectedly") from None


@dataclass(frozen=True)
class Digest:
    """A table's rows, and the sum of each 32-bit slice of every row's hash."""

    rows: int
    sums: tuple[int, ...]


# The text of a cell by its type, in each engine; the cell is `{c}`, already quoted.
BIGQUERY_TEXT = {
    "STRING": "{c}",
    "INTEGER": "CAST({c} AS STRING)",
    "BOOLEAN": "CAST({c} AS STRING)",
}
DUCKDB_TEXT = {
    "VARCHAR": "{c}",
    "BIGINT": "CAST({c} AS VARCHAR)",
    "BOOLEAN": "CAST({c} AS VARCHAR)",
}


def bigquery_digest_sql(reference: str, columns: Sequence[tuple[str, str]]) -> str:
    """The digest over `reference` in BigQuery's dialect; columns are (quoted name, type)."""
    cells = []
    for c, kind in columns:
        if kind not in BIGQUERY_TEXT:
            raise LoadError(f"no digest rule for a {kind} column")
        text = BIGQUERY_TEXT[kind].format(c=c)
        cells.append(
            f"IF({c} IS NULL, 'N', CONCAT('V', CAST(BYTE_LENGTH({text}) AS STRING), ':', {text}))"
        )
    row = f"TO_HEX(SHA256(CONCAT({', '.join(cells)})))"
    sums = ", ".join(
        f"SUM(CAST(CONCAT('0x', SUBSTR(h, {i * SLICE + 1}, {SLICE})) AS INT64)) AS s{i}"
        for i in range(SLICES)
    )
    return f"SELECT COUNT(*) AS n, {sums} FROM (SELECT {row} AS h FROM {reference})"  # noqa: S608


def duckdb_digest_sql(source: str, columns: Sequence[tuple[str, str]]) -> str:
    """The same digest over `source` in DuckDB's dialect; columns are (quoted name, type)."""
    cells = []
    for c, kind in columns:
        if kind not in DUCKDB_TEXT:
            raise LoadError(f"no digest rule for a {kind} column")
        text = DUCKDB_TEXT[kind].format(c=c)
        cells.append(
            f"CASE WHEN {c} IS NULL THEN 'N'"
            f" ELSE 'V' || CAST(strlen({text}) AS VARCHAR) || ':' || {text} END"
        )
    row = f"sha256({' || '.join(cells)})"
    sums = ", ".join(
        f"SUM(CAST('0x' || substr(h, {i * SLICE + 1}, {SLICE}) AS BIGINT)) AS s{i}"
        for i in range(SLICES)
    )
    return f"SELECT COUNT(*) AS n, {sums} FROM (SELECT {row} AS h FROM {source})"  # noqa: S608


def digest_of(row: Mapping[str, Any]) -> Digest:
    try:
        return Digest(int(row["n"] or 0), tuple(int(row[f"s{i}"] or 0) for i in range(SLICES)))
    except (KeyError, TypeError, ValueError):
        raise LoadError("the digest answered unexpectedly") from None


def digest(name: str, env: Mapping[str, str], run: Runner = run_tool) -> Digest:
    """The loaded table's digest, in BigQuery."""
    t = asked(name, env, run)
    sql = bigquery_digest_sql(t.reference, [(t.column(c), kind) for c, kind in t.columns])
    return digest_of(_one_row(t.project, t.where, sql, run))


def local_digest(path: Path) -> Digest:
    """A local landed Parquet file's digest, in DuckDB, by the same rule."""
    import duckdb

    con = duckdb.connect()
    kinds = con.execute("DESCRIBE SELECT * FROM read_parquet(?)", [str(path)]).fetchall()
    columns = [('"' + str(c).replace('"', '""') + '"', str(k)) for c, k, *_ in kinds]
    cursor = con.execute(duckdb_digest_sql("read_parquet(?)", columns), [str(path)])
    names = [d[0] for d in cursor.description or ()]
    values = cursor.fetchone()
    if values is None:
        raise LoadError("the digest answered unexpectedly")
    return digest_of(dict(zip(names, values, strict=True)))


def main(
    argv: list[str],
    env: Mapping[str, str],
    run: Runner = run_tool,
    fs: AbstractFileSystem | None = None,
) -> int:
    if not argv:
        print("usage: python -m ingest.load <landed file>...", file=sys.stderr)
        return 2
    try:
        tables = load(argv, env, run, fs)
    except LoadError as error:
        print(f"load: {error}", file=sys.stderr)
        return 1
    for t in tables:
        print(f"{t.table}: {t.rows} rows, {t.bytes} bytes, {len(t.columns)} columns, from {t.name}")
    return 0


if __name__ == "__main__":
    import os

    sys.exit(main(sys.argv[1:], os.environ))
