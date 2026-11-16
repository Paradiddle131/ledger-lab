"""The BigQuery build against a local build on the same landing, relation by relation.

For each relation in the local DuckDB file's `main` schema or in the BigQuery dataset the profile
builds every model and seed in (`intermediate`), each engine is asked, by one query apiece, for the
row count and a digest: the MD5 of every row's canonical text, sorted and joined, then hashed
again, so two relations with the same digest hold the same multiset of canonical rows whatever
their order, short of an MD5 collision. The column names, in order, and each column's pair of type
families are compared from each engine's own schema (DuckDB's catalog, `bq show`, which bills
nothing) before the digest is asked for.

A row's canonical text is its cells in column order, joined by the unit separator (U+001F), a
null as the record separator (U+001E). A cell is written by its type family: text as stored; an
integer in decimal digits; an exact decimal (DuckDB DECIMAL, BigQuery NUMERIC or BIGNUMERIC) in its
shortest form, with no trailing zeros after the point and no point for a whole number, so DuckDB's
`12.500000` and BigQuery's `12.5` are meant to be one text; a float rounded to six decimal places,
then written as a decimal, since dbt's float type is DuckDB's 32-bit FLOAT and BigQuery's 64-bit
FLOAT64 (a score of 0.9 is 0.8999999761... in the first); a boolean as `true` or `false`; a date as
`YYYY-MM-DD`; a BigQuery TIMESTAMP in UTC, and a DuckDB TIMESTAMP or BigQuery DATETIME as it is
stored, as `YYYY-MM-DD HH:MM:SS.ffffff`. A type outside these stops the comparison, naming the
engine and type.

A relation matches when it is on both sides, its column names are the same in the same order,
every column's pair of families is one of `SAME_FAMILY`, and its row count and digest are equal.
So types are compared by family, not by width, precision or scale (DuckDB `DECIMAL(28,6)` and
BigQuery `NUMERIC` match), and floats are compared to six decimal places. The run fails when any
relation differs or is on one side only, and when no relation is on both sides.

Every BigQuery query is capped by `maximum_bytes_billed` (1,000,000,000 bytes); the project comes
from `platform/gcp_project.py` (through `ingest/bucket.py`) and the region from
`ingest.load.location`, which refuses a region outside the free tier's before any call. What is
printed is relation names, row counts, digests, column types and whether they match; a failed
`bq` call is reported by its exit status only, since its messages name the project. No row is
printed.

`uv run python -m tests.models.bigquery_parity <local duckdb file>`, after `dbt build` on both
targets over the same landing.
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
from ingest.load import location

# The one dataset models/profiles.yml builds every model and seed in on BigQuery.
DATASET = "intermediate"
# The pairs of cell families (local, BigQuery) a column may have and still match: the same family,
# and DuckDB's TIMESTAMP against BigQuery's TIMESTAMP, the two spellings the build produces for the
# UK files' publication times, whose local value holds the time in UTC. Any other pair, a DuckDB
# DECIMAL(p,0) (`whole`) and a DATETIME on BigQuery among them, differs.
SAME_FAMILY = {
    ("text", "text"),
    ("integer", "integer"),
    ("decimal", "decimal"),
    ("float", "float"),
    ("boolean", "boolean"),
    ("date", "date"),
    ("datetime", "timestamp"),
}
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


def bq(project: str, where: str, *command: str) -> str:
    """One `bq` call in the lab project, in the region `where`; on failure only its exit status is
    reported, since its messages name the project."""
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


def bigquery_relations(project: str, where: str) -> list[str]:
    """The relations `bq ls` lists in the dataset the profile builds in."""
    answer = bq(project, where, "--format=json", "ls", "--max_results=1000", DATASET)
    return [str(table["tableReference"]["tableId"]) for table in json.loads(answer or "[]")]


def bigquery_columns(project: str, where: str, relation: str) -> list[Column]:
    shown = json.loads(bq(project, where, "--format=json", "show", f"{DATASET}.{relation}"))
    return [Column(str(f["name"]), str(f["type"])) for f in shown["schema"]["fields"]]


def bigquery_digest(
    project: str, where: str, relation: str, columns: Sequence[Column]
) -> tuple[int, str]:
    answer = bq(
        project,
        where,
        "--format=json",
        "--quiet",
        "query",
        "--use_legacy_sql=false",
        f"--maximum_bytes_billed={CAP}",
        bigquery_digest_sql(DATASET, relation, columns),
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


def type_differences(local: Sequence[Column], remote: Sequence[Column]) -> list[str]:
    """Each column whose two types are not one of the `SAME_FAMILY` pairs."""
    return [
        f"{a.name} {a.type}~{b.type}"
        for a, b in zip(local, remote, strict=True)
        if (family("duckdb", a.type), family("bigquery", b.type)) not in SAME_FAMILY
    ]


def main(argv: Sequence[str]) -> int:
    if len(argv) != 1:
        print("usage: python -m tests.models.bigquery_parity <local duckdb file>", file=sys.stderr)
        return 2
    where = location(os.environ)
    project = lab_project(os.environ)
    differ = compared = 0
    with duckdb.connect(str(Path(argv[0])), read_only=True) as db:
        local = local_relations(db)
        remote = bigquery_relations(project, where)
        for name in sorted(set(local) ^ set(remote)):
            differ += 1
            print(f"{name}: only {'local' if name in local else 'on BigQuery'}; DIFFER")
        for name in sorted(set(local) & set(remote)):
            columns = bigquery_columns(project, where, name)
            reasons = []
            if [c.name for c in columns] != [c.name for c in local[name]]:
                reasons.append("column names or their order")
            else:
                reasons += [f"type families of {t}" for t in type_differences(local[name], columns)]
            here = db.execute(duckdb_digest_sql(name, local[name])).fetchone()
            assert here is not None
            mine = (int(here[0]), str(here[1]))
            there = bigquery_digest(project, where, name, columns) if not reasons else None
            if there is not None and mine[0] != there[0]:
                reasons.append("row count")
            if there is not None and mine[1] != there[1]:
                reasons.append("digest")
            compared += 1
            differ += bool(reasons)
            types = sorted(
                {f"{a.type}~{b.type}" for a, b in zip(local[name], columns, strict=False)}
            )
            print(
                f"{DATASET}.{name}: rows {mine[0]} local, {there[0] if there else '-'} BigQuery;"
                f" digest {mine[1]} local, {there[1] if there else '-'} BigQuery;"
                f" {'DIFFER (' + '; '.join(reasons) + ')' if reasons else 'MATCH'}"
                f" (types {', '.join(types)})"
            )
    print(f"{len(set(local) | set(remote))} relations, {compared} compared, {differ} differ")
    if not compared:
        print("no relation is on both sides, so nothing was compared", file=sys.stderr)
        return 1
    return 1 if differ else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
