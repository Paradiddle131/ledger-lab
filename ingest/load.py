"""`uv run python -m ingest.load <landed file>...`: load landed files from the landing bucket into
the warehouse's `landed` dataset, one BigQuery load job per file.

Each name is an object the fetch kit landed at the root of the lab's landing bucket (`bucket.py`),
so a load reads only what a landing step produced, hashed names included, never a publisher's
file. Every landed file is Parquet its step wrote: text columns as the publishers' text, typed
columns only where the step typed them; the load keeps the file's own schema, so no delimiter,
quoting, header or encoding rule can split a row differently from the landing. A file lands as the
table named after it with underscores for its hyphens (`chicago-contracts` as
`landed.chicago_contracts`), replaced whole (WRITE_TRUNCATE) so a re-run is idempotent, and a
job that meets a single bad record fails and leaves the table as it was. Each file's table is
loaded in turn; a failure stops before the next.

Refused before any call: a project other than the one `LEDGER_LAB_GCP_PROJECT` names (read through
`platform/gcp_project.py`, never the session's `GCP_PROJECT`), a location in
`LEDGER_LAB_BQ_LOCATION` outside the free-tier regions the landing bucket may sit in, and a name
that is not a landed file's (hidden objects and the lock among them). Refused before any load: a
name the bucket does not hold, and a bucket holding the landing lock, since a landing in progress
may have emptied a name.

Load jobs from Cloud Storage bill nothing; the loader runs no query and no streaming insert. After
each load it reads the table's metadata (`bq show`, which bills nothing), and refuses to go on once
the loaded tables hold more than `STORAGE_CAP` bytes, far inside BigQuery's free 10 GiB. The live
comparisons run `query`, which dry-runs every query first and caps its billed bytes.

The calls go through `bq`, whose credential the session's network proxy adds; a failure is
reported by the file and `bq`'s exit status only, since its messages name the project.
"""

import json
import re
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from fsspec import AbstractFileSystem

from ingest.bucket import LOCK, SCHEME, Bucket, BucketError, bucket_name, cloud_storage, lab_project

DATASET = "landed"
LOCATION_VARIABLE = "LEDGER_LAB_BQ_LOCATION"
# The regions whose free tier covers the landing bucket and BigQuery storage, as platform/gcp's
# `region` variable allows them.
FREE_REGIONS = ("us-west1", "us-central1", "us-east1")
# A landed file's name: lower-case words and digits joined by hyphens, as the fetch kit names them.
LANDED_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
# The landing is about 52 MB of Parquet; the tables it loads into stay far below this.
STORAGE_CAP = 2 * 1024**3
# Every query's billed bytes, far inside the free 1 TiB a month and the daily query cap.
MAX_BYTES_BILLED = 1_000_000_000

Runner = Callable[[Sequence[str]], str]


class LoadError(Exception):
    """A load was refused or failed; the message names files and tables, never the project."""


def run_tool(command: Sequence[str]) -> str:
    return subprocess.run(command, capture_output=True, text=True, check=True).stdout  # noqa: S603


def location(env: Mapping[str, str]) -> str:
    """Where the jobs run: `LEDGER_LAB_BQ_LOCATION`, one of the free-tier regions."""
    where = env.get(LOCATION_VARIABLE, "").strip()
    if where not in FREE_REGIONS:
        raise LoadError(
            f"{LOCATION_VARIABLE} must be the warehouse's region, one of {', '.join(FREE_REGIONS)}"
        )
    return where


def table_name(name: str) -> str:
    """The `landed` table a landed file loads into."""
    if not LANDED_NAME.match(name):
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


def show_command(project: str, where: str, name: str) -> list[str]:
    return bq(project, where, "--format=json", "show", f"{project}:{DATASET}.{table_name(name)}")


@dataclass(frozen=True)
class Table:
    """A loaded table's metadata: its rows, its stored bytes and its columns with their types."""

    name: str
    table: str
    rows: int
    bytes: int
    columns: tuple[tuple[str, str], ...]


def table_of(name: str, answer: str) -> Table:
    """The table `bq show` describes, refused unless it is the `landed` table of `name`."""
    try:
        shown: Any = json.loads(answer)
        reference = shown["tableReference"]
        if reference["datasetId"] != DATASET or reference["tableId"] != table_name(name):
            raise LoadError(f"the table shown is not {DATASET}.{table_name(name)}")
        columns = tuple((str(f["name"]), str(f["type"])) for f in shown["schema"]["fields"])
        return Table(
            name,
            f"{DATASET}.{table_name(name)}",
            int(shown["numRows"]),
            int(shown["numBytes"]),
            columns,
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        if isinstance(error, LoadError):
            raise
        raise LoadError(
            f"{DATASET}.{table_name(name)}: an answer of unexpected shape ({type(error).__name__})"
        ) from None


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


def load(
    names: Sequence[str],
    env: Mapping[str, str],
    run: Runner = run_tool,
    fs: AbstractFileSystem | None = None,
) -> list[Table]:
    """Load each landed file into its `landed` table, in turn, and describe the tables."""
    if not names:
        raise LoadError("name at least one landed file")
    if len(set(names)) != len(names):
        raise LoadError("a landed file is named twice")
    for name in names:
        table_name(name)
    try:
        project = lab_project(env)
        root = bucket_name(f"{SCHEME}{project}-landing", env)
    except BucketError as error:
        raise LoadError(str(error)) from None
    where = location(env)
    try:
        held = Bucket(fs if fs is not None else cloud_storage(), root).names()
    except BucketError as error:
        raise LoadError(str(error)) from None
    if LOCK in held:
        raise LoadError(f"the landing bucket holds the lock {LOCK}: a landing runs or was killed")
    missing = [name for name in names if name not in held]
    if missing:
        raise LoadError(f"the landing bucket holds no {', '.join(missing)}: land it first")
    tables: list[Table] = []
    for name in names:
        call(run, f"load {name}", load_command(project, where, root, name))
        tables.append(
            table_of(
                name, call(run, f"describe {name}'s table", show_command(project, where, name))
            )
        )
        stored = sum(t.bytes for t in tables)
        if stored > STORAGE_CAP:
            raise LoadError(
                f"the loaded tables hold {stored} bytes, above the {STORAGE_CAP}-byte cap: stop"
            )
    return tables


def query(sql: str, env: Mapping[str, str], run: Runner = run_tool) -> list[dict[str, Any]]:
    """A standard SQL query's rows, after a dry run shows it bills at most `MAX_BYTES_BILLED`, and
    under that cap; the SQL names its tables in full (`<lab project>.landed.<table>`)."""
    try:
        project = lab_project(env)
    except BucketError as error:
        raise LoadError(str(error)) from None
    where = location(env)
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
