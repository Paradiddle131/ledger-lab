"""Probe (reviewer's, not committed): objects other than tables and views carrying source values
into a persona's database, and whether `breaches` sees them."""
import duckdb
import pytest

from access.compile import SOURCE, compile_persona, connect, literal, materialise, source_schema
from tests.access.test_purposes import aggregate_columns, breaches, product_columns

PLANTS = {
    # every contract number of the source, stored as the values of an enum type
    "enum-type": [
        f"create type main.codes as enum (select distinct contract_number from {SOURCE}.main.payments where contract_number is not null)"
    ],
    # one document number as a scalar macro's literal body
    "macro": [
        f"create macro main.m() as (select '" + "{doc}" + "')"
    ],
    # document numbers in a table comment
    "comment": [
        "comment on table group_close.payments is '{doc}'"
    ],
    # document number as a column's default
    "column-default": [
        "alter table group_close.payments alter column source_table set default '{doc}'"
    ],
    # a sequence whose value is a count the persona may not see
    "sequence": [
        "create sequence main.s start with 424242"
    ],
}


@pytest.mark.parametrize("plant", list(PLANTS))
def test_probe(access, columns, aggregates, purposes, warehouse, tmp_path, plant):
    persona = "group-controller"
    schema = source_schema(warehouse)
    plan = compile_persona(access, columns, aggregates, purposes, persona, schema)
    path = materialise(plan, warehouse, tmp_path)
    with duckdb.connect(str(path)) as b:
        b.execute(f"attach {literal(str(warehouse))} as {SOURCE} (read_only)")
        (doc,) = b.execute(f"select max(document_number) from {SOURCE}.main.payments").fetchone()
        for s in PLANTS[plant]:
            b.execute(s.replace("{doc}", str(doc)))
        b.execute(f"detach {SOURCE}")
    db = connect(path)
    base = product_columns(access, warehouse)
    found = breaches(db, persona, purposes, aggregate_columns(access, aggregates, persona, base))
    if plant == "enum-type":
        got = db.execute("select enum_range(null::main.codes)").fetchone()[0]
    elif plant == "macro":
        got = db.execute("select main.m()").fetchone()[0]
    elif plant == "comment":
        got = db.execute("select comment from duckdb_tables() where table_name='payments'").fetchone()[0]
    elif plant == "column-default":
        got = db.execute("select column_default from duckdb_columns() where table_name='payments' and column_name='source_table'").fetchone()[0]
    else:
        got = db.execute("select start_value from duckdb_sequences() where sequence_name='s'").fetchone()[0]
    print(f"\nPROBE {plant}: breaches={found} persona-reads-len={len(str(got))} sample={str(got)[:4]}***")
    assert found == []
