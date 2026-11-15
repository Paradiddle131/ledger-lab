import duckdb, pytest
from pathlib import Path
from access.compile import SOURCE, compile_persona, connect, literal, materialise, source_schema
from tests.access.test_purposes import breaches, expected

PROBES = {
  "schema-comment": ("group-controller", ["comment on schema group_close is 'leaked-doc-number'"]),
  "main-schema-comment": ("group-controller", ["comment on schema main is 'leaked'"]),
  "generated-col": ("group-controller", [
      "create table main.k as select * from group_close.payments",
      "drop table group_close.payments",
      "create table group_close.payments as select * exclude (source_table) , source_table from main.k where false",
      ]),
  "other-entity-rows": ("accountant-chicago", [
      f"insert into payment_review.payments by name select * from {SOURCE}.main.payments where entity_id <> 'chicago'"]),
  "true-generated": ("group-controller", [
      "create table main.k as select * exclude (source_table) from group_close.payments",
      "drop table group_close.payments",
      "create table group_close.payments as select * from main.k",
      "drop table main.k",
      "alter table group_close.payments add column source_table varchar generated always as ('leak')",
      ]),
  "true-generated-rebuild": ("group-controller", [
      "create table main.k as select * from group_close.payments",
      "create table main.def as select 1",
      ]),
  "table-tags": ("group-controller", ["alter table group_close.payments set tags (x='y')"]),
}

@pytest.mark.parametrize("probe", list(PROBES))
def test_probe(access, columns, aggregates, purposes, warehouse: Path, tmp_path: Path, probe):
    persona, stmts = PROBES[probe]
    schema = source_schema(warehouse)
    plan = compile_persona(access, columns, aggregates, purposes, persona, schema)
    path = materialise(plan, warehouse, tmp_path)
    with duckdb.connect(str(path)) as b:
        b.execute(f"attach {literal(str(warehouse))} as {SOURCE} (read_only)")
        for s in stmts:
            try:
                b.execute(s); print("OK  ", s)
            except Exception as e:
                print("ERR ", s, type(e).__name__, str(e)[:150])
        b.execute(f"detach {SOURCE}")
    want = expected(access, columns, aggregates, purposes, persona, warehouse)
    db = connect(path)
    try:
        print("BREACHES", breaches(db, persona, want))
        if probe=="schema-comment":
            print(db.execute("select schema_name, comment from duckdb_schemas() where comment is not null").fetchall())
        if probe=="other-entity-rows":
            print(db.execute("select entity_id,count(*) from payment_review.payments group by 1").fetchall())
    finally:
        db.close()
