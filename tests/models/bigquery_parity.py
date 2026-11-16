"""The BigQuery build against a local build on the same landing, relation by relation.

For each relation in the local DuckDB file's `main` schema or in the lab's BigQuery datasets
`intermediate` (where the profile builds every model and seed) and `group_totals` (the group
totals product's, which a relation built there would be in), each engine is asked, by one query
apiece, for the row count and a digest: the MD5 of every row's canonical text, sorted and joined,
then hashed again, so two relations with the same digest hold the same multiset of canonical rows
whatever their order, short of an MD5 collision. The column names, in order, are compared from
each engine's own schema (DuckDB's catalog, `bq show`, which bills nothing).

A row's canonical text is its cells in column order, joined by the unit separator (U+001F), a
null as the record separator (U+001E). A cell is written the same way on both engines by its type
family: text as stored; an integer in decimal digits; an exact decimal (DuckDB DECIMAL, BigQuery
NUMERIC or BIGNUMERIC) in its shortest form, with no trailing zeros after the point and no point
for a whole number, so DuckDB's `12.500000` and BigQuery's `12.5` are one text; a float rounded to
six decimal places, then written as a decimal, since dbt's float type is DuckDB's 32-bit FLOAT and
BigQuery's 64-bit FLOAT64 (a score of 0.9 is 0.8999999761... in the first); a boolean as `true` or
`false`; a date as `YYYY-MM-DD`; a timestamp in UTC, and a DuckDB TIMESTAMP or BigQuery DATETIME,
as `YYYY-MM-DD HH:MM:SS.ffffff`. A type outside these stops the comparison, naming the engine and
type. The digest therefore does not compare the types beyond that family (DuckDB `DECIMAL(28,6)`
and BigQuery `NUMERIC` read alike, and a decimal and a text cell holding the same characters read
alike), nor floats past six decimal places; each relation's pairs of types are printed beside it.

Every BigQuery query is capped by `maximum_bytes_billed` (1,000,000,000 bytes, the profile's cap);
the project comes from `platform/gcp_project.py` (through `ingest/bucket.py`). What is printed is
relation names, row counts, digests, column types and whether they match; a failed `bq` call is
reported by its exit status only, since its messages name the project. No row is printed.

`uv run python -m tests.models.bigquery_parity <local duckdb file>`, after `dbt build` on both
targets over the same landing; it exits 1 when any relation differs or is missing on one side.
"""

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import duckdb

from ingest.bucket import lab_project

DATASETS = ("intermediate", "group_totals")
CAP = 1_000_000_000


@dataclass(frozen=True)
class Column:
    name: str
    type: str


def family(engine: str, type_: str) -> str:
    """The cell family a column's type is written by, or a ValueError for one not handled."""
    t = type_.upper()
    if engine == "duckdb":
        if t == "VARCHAR":
            return "text"
        if t in {"TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT"}:
            return "integer"
        if t.startswith("DECIMAL"):
            return "whole" if t.endswith(",0)") else "decimal"
        if t in {"DOUBLE", "FLOAT"}:
            return "float"
        if t == "BOOLEAN":
            return "boolean"
        if t == "DATE":
            return "date"
        if t == "TIMESTAMP":
            return "datetime"
    else:
        if t == "STRING":
            return "text"
        if t in {"INTEGER", "INT64"}:
            return "integer"
        if t in {"NUMERIC", "BIGNUMERIC"}:
            return "decimal"
        if t in {"FLOAT", "FLOAT64"}:
            return "float"
        if t in {"BOOLEAN", "BOOL"}:
            return "boolean"
        if t == "DATE":
            return "date"
        if t == "DATETIME":
            return "datetime"
        if t == "TIMESTAMP":
            return "timestamp"
    raise ValueError(f"no canonical text for {engine} type {type_}")


def duckdb_cell(column: Column) -> str:
    c = '"' + column.name.replace('"', '""') + '"'
    kind = family("duckdb", column.type)
    if kind == "decimal":
        text = f"rtrim(rtrim(cast({c} as varchar), '0'), '.')"
        return f"case when {c} = 0 then '0' else {text} end"
    if kind == "float":
        text = f"rtrim(rtrim(cast(cast(round({c}, 6) as decimal(38, 6)) as varchar), '0'), '.')"
        return f"case when {c} = 0 then '0' else {text} end"
    if kind == "datetime":
        return f"strftime({c}, '%Y-%m-%d %H:%M:%S.%f')"
    return f"cast({c} as varchar)"


def bigquery_cell(column: Column) -> str:
    c = "`" + column.name + "`"
    kind = family("bigquery", column.type)
    if kind == "float":
        return f"cast(cast(round({c}, 6) as numeric) as string)"
    if kind == "datetime":
        return f"format_datetime('%Y-%m-%d %H:%M:%E6S', {c})"
    if kind == "timestamp":
        return f"format_timestamp('%Y-%m-%d %H:%M:%E6S', {c}, 'UTC')"
    if kind == "boolean":
        return f"lower(cast({c} as string))"
    return f"cast({c} as string)"


def row_text(cells: Sequence[str], code_point: str) -> str:
    """The cells joined by the unit separator, a null as the record separator; `code_point` is
    the engine's spelling of the character with a code point, `{}` where the number goes."""
    separator, null = code_point.format(31), code_point.format(30)
    parts = [f"coalesce({cell}, {null})" for cell in cells]
    return f"concat({f', {separator}, '.join(parts)})" if len(parts) > 1 else parts[0]


def duckdb_digest_sql(relation: str, columns: Sequence[Column]) -> str:
    row = row_text([duckdb_cell(c) for c in columns], "chr({})")
    # The relation and column names come from the build's own catalog, not from input.
    hashed = f'select md5({row}) as h from main."{relation}"'  # noqa: S608
    return f"select count(*), md5(coalesce(string_agg(h, '' order by h), '')) from ({hashed})"  # noqa: S608


def bigquery_digest_sql(dataset: str, relation: str, columns: Sequence[Column]) -> str:
    row = row_text([bigquery_cell(c) for c in columns], "code_points_to_string([{}])")
    # The relation and column names come from `bq ls` and `bq show`, not from input.
    hashed = f"select to_hex(md5({row})) as h from `{dataset}.{relation}`"  # noqa: S608
    digest = "to_hex(md5(coalesce(string_agg(h, '' order by h), '')))"
    return f"select count(*) as n, {digest} as d from ({hashed})"  # noqa: S608


def bq(project: str, *command: str) -> str:
    """One `bq` call in the lab project; on failure only its exit status is reported, since its
    messages name the project."""
    where = os.environ.get("LEDGER_LAB_BQ_LOCATION", "US")
    run = subprocess.run(
        [
            shutil.which("bq") or "bq",
            f"--project_id={project}",
            f"--location={where}",
            "--headless",
            *command,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if run.returncode != 0:
        raise RuntimeError(f"bq {command[0]} failed with status {run.returncode}")
    return run.stdout


def bigquery_relations(project: str) -> dict[str, str]:
    """Each relation's name and the dataset holding it."""
    found: dict[str, str] = {}
    for dataset in DATASETS:
        answer = bq(project, "--format=json", "ls", "--max_results=1000", dataset)
        listed = json.loads(answer or "[]")
        for table in listed:
            name = str(table["tableReference"]["tableId"])
            if name in found:
                raise RuntimeError(f"{name} is in both {found[name]} and {dataset}")
            found[name] = dataset
    return found


def bigquery_columns(project: str, dataset: str, relation: str) -> list[Column]:
    shown = json.loads(bq(project, "--format=json", "show", f"{dataset}.{relation}"))
    return [Column(str(f["name"]), str(f["type"])) for f in shown["schema"]["fields"]]


def bigquery_digest(project: str, dataset: str, columns: list[Column], relation: str) -> tuple:
    answer = bq(
        project,
        "--format=json",
        "--quiet",
        "query",
        "--use_legacy_sql=false",
        f"--maximum_bytes_billed={CAP}",
        bigquery_digest_sql(dataset, relation, columns),
    )
    row = json.loads(answer)[0]
    return int(row["n"]), str(row["d"])


def local_relations(db: duckdb.DuckDBPyConnection) -> dict[str, list[Column]]:
    rows = db.execute(
        "select table_name, column_name, data_type from duckdb_columns()"
        " where schema_name = 'main' and database_name = current_database()"
        " order by table_name, column_index"
    ).fetchall()
    relations: dict[str, list[Column]] = {}
    for table, name, type_ in rows:
        relations.setdefault(table, []).append(Column(name, type_))
    return relations


def main(argv: Sequence[str]) -> int:
    if len(argv) != 1:
        print("usage: python -m tests.models.bigquery_parity <local duckdb file>", file=sys.stderr)
        return 2
    project = lab_project(os.environ)
    differ = 0
    with duckdb.connect(str(Path(argv[0])), read_only=True) as db:
        local = local_relations(db)
        remote = bigquery_relations(project)
        for name in sorted(set(local) ^ set(remote)):
            differ += 1
            print(f"{name}: only {'local' if name in local else 'on BigQuery'}")
        for name in sorted(set(local) & set(remote)):
            dataset = remote[name]
            columns = bigquery_columns(project, dataset, name)
            names_match = [c.name for c in columns] == [c.name for c in local[name]]
            here = db.execute(duckdb_digest_sql(name, local[name])).fetchone()
            assert here is not None
            there = bigquery_digest(project, dataset, columns, name) if names_match else None
            same = names_match and (int(here[0]), str(here[1])) == there
            differ += not same
            types = sorted(
                {f"{a.type}~{b.type}" for a, b in zip(local[name], columns, strict=False)}
            )
            print(
                f"{dataset}.{name}: rows {here[0]} local, {there[0] if there else '-'} BigQuery;"
                f" digest {here[1]} local, {there[1] if there else '-'} BigQuery;"
                f" columns {'same' if names_match else 'DIFFER'}; {'MATCH' if same else 'DIFFER'}"
                f" (types {', '.join(types)})"
            )
    print(f"{len(set(local) | set(remote))} relations, {differ} differ")
    return 1 if differ else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
