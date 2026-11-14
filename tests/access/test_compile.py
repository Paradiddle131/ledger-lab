"""The compiled personas on the local engine: rows, masked columns and minimum groups, and the
routes a persona's connection might take around them.

Every test acts as a persona through `as_persona` over the dbt project built from the committed
samples, and checks what the connection returns against the warehouse read as the builder, with
the masking computed here in Python apart from the compiler's SQL. The refusals act on copies of
the real catalogues with one line changed, or on an invented source schema, labelled as such.
"""

import base64
import copy
import hashlib
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import duckdb
import pytest

from access.compile import (
    CompileError,
    Plan,
    Rows,
    compile_persona,
    open_persona,
    source_schema,
    statements,
)
from tests.conftest import landed_file

AsPersona = Callable[[str], duckdb.DuckDBPyConnection]
Doc = dict[str, Any]


def builder(warehouse: Path, sql: str, *params: object) -> list[tuple[Any, ...]]:
    """The warehouse as the account that builds the models reads it."""
    with duckdb.connect(str(warehouse), read_only=True) as db:
        return db.execute(sql, list(params)).fetchall()


def sha256_rendered(value: str) -> str:
    """BigQuery's hash masking rule on a STRING: SHA-256 of its UTF-8 bytes, in Base64."""
    return base64.b64encode(hashlib.sha256(value.encode()).digest()).decode()


def accountants(access: Doc) -> dict[str, str]:
    """Each persona that reads one entity's payments, with that entity."""
    entities: dict[str, set[str]] = {}
    for grant in access["grants"]:
        if grant["product"] == "payments" and grant["access"] == "read":
            entities.setdefault(grant["persona"], set()).add(grant["entity"])
    return {persona: next(iter(e)) for persona, e in entities.items() if len(e) == 1}


def tagged(columns: Doc) -> dict[str, str]:
    """The payments mart's tagged columns, each with its tag."""
    return {
        c.partition(".")[2]: tag for c, tag in columns["products"]["payments"]["columns"].items()
    }


def reader(columns: Doc, tag: str, kind: str) -> str:
    return next(t[kind][0] for t in columns["tags"] if t["id"] == tag)


def plan_of(access: Doc, columns: Doc, aggregates: Doc, persona: str, warehouse: Path) -> Plan:
    return compile_persona(access, columns, aggregates, persona, source_schema(warehouse))


# The row rule.


def test_an_entity_accountant_sees_no_other_entitys_row(
    as_persona: AsPersona, access: Doc, warehouse: Path
) -> None:
    for persona, entity in accountants(access).items():
        db = as_persona(persona)
        seen = db.execute("select entity_id, count(*) from payments group by entity_id").fetchall()
        own = builder(
            warehouse,
            "select entity_id, count(*) from payments where entity_id = ? group by 1",
            entity,
        )
        assert seen == own, persona
        others = builder(
            warehouse, "select payment_key from payments where entity_id <> ? limit 1", entity
        )
        assert others, "the samples hold another entity's payment"
        (key,) = others[0]
        assert db.execute("select * from payments where payment_key = ?", [key]).fetchall() == []


def test_the_row_rule_follows_the_grants_with_no_code_edit(
    access: Doc, columns: Doc, aggregates: Doc, warehouse: Path
) -> None:
    catalogue = copy.deepcopy(access)
    catalogue["grants"].append(
        {
            "persona": "accountant-chicago",
            "entity": "uk-transport",
            "product": "payments",
            "access": "read",
        }
    )
    (rows,) = [
        t
        for t in plan_of(catalogue, columns, aggregates, "accountant-chicago", warehouse).tables
        if isinstance(t, Rows) and t.table == "payments"
    ]
    assert rows.entities == {"chicago", "uk-transport"}


# The column rules.


def test_a_masked_column_returns_masked_values(
    as_persona: AsPersona, columns: Doc, warehouse: Path
) -> None:
    for column, tag in tagged(columns).items():
        rule = next(t["masking"] for t in columns["tags"] if t["id"] == tag)
        assert rule == "hash", (
            "the payments mart's tags all hash today; another rule needs its case"
        )
        stored = dict(
            builder(warehouse, f"select payment_key, {column} from payments")  # noqa: S608
        )
        assert any(stored.values()), f"the samples hold no {column}"
        for kind in ("masked", "clear"):
            db = as_persona(reader(columns, tag, kind))
            seen = dict(db.execute(f"select payment_key, {column} from payments").fetchall())  # noqa: S608
            assert seen, kind
            for key, value in seen.items():
                if kind == "clear" or stored[key] is None:
                    assert value == stored[key], (kind, column, key)
                else:
                    assert value == sha256_rendered(stored[key]), (column, key)
                    assert value != stored[key]


def test_a_filter_on_a_masked_column_sees_the_masked_value(
    as_persona: AsPersona, columns: Doc, warehouse: Path
) -> None:
    db = as_persona(reader(columns, "individual-id", "masked"))
    (entity,) = {e for (e,) in db.execute("select distinct entity_id from payments").fetchall()}
    ((name, payments),) = builder(
        warehouse,
        "select vendor_name, count(*) from payments where entity_id = ?"
        " group by 1 order by 2 desc, 1 limit 1",
        entity,
    )
    count = "select count(*) from payments where vendor_name = ?"
    assert db.execute(count, [name]).fetchone() == (0,)
    assert db.execute(count, [sha256_rendered(name)]).fetchone() == (payments,)
    # Equal stored values mask alike, so a masked column still joins and counts.
    assert (
        db.execute("select count(distinct vendor_name) from payments").fetchone()
        == builder(
            warehouse,
            "select count(distinct vendor_name) from payments where entity_id = ?",
            entity,
        )[0]
    )


def test_a_persona_that_reads_a_tag_neither_way_gets_no_column(
    access: Doc, columns: Doc, aggregates: Doc, warehouse: Path, tmp_path: Path
) -> None:
    persona = reader(columns, "individual-id", "masked")
    neither = copy.deepcopy(columns)
    for tag in neither["tags"]:
        tag["masked"].remove(persona)
    db = open_persona(access, neither, aggregates, persona, warehouse, tmp_path)
    try:
        held = {
            c
            for (c,) in db.execute(
                "select column_name from duckdb_columns() where table_name = 'payments'"
            ).fetchall()
        }
        assert held and held.isdisjoint(tagged(columns))
        for column in tagged(columns):
            with pytest.raises(duckdb.BinderException):
                db.execute(f"select {column} from payments")  # noqa: S608
    finally:
        db.close()


@pytest.mark.parametrize("rule", ["last-four", "nullify"])
def test_last_four_and_nullify_mask_as_bigquerys_rules(
    access: Doc, columns: Doc, aggregates: Doc, warehouse: Path, tmp_path: Path, rule: str
) -> None:
    # Invented: the contract number tagged with the rule, which no catalogue line does.
    persona = reader(columns, "individual-id", "masked")
    invented = copy.deepcopy(columns)
    invented["tags"][0]["masking"] = rule
    invented["products"]["payments"]["columns"]["payments.contract_number"] = invented["tags"][0][
        "id"
    ]
    if persona not in invented["tags"][0]["masked"]:
        pytest.fail("the first tag's masked readers lack the persona")
    stored = dict(builder(warehouse, "select payment_key, contract_number from payments"))
    db = open_persona(access, invented, aggregates, persona, warehouse, tmp_path)
    try:
        seen = dict(db.execute("select payment_key, contract_number from payments").fetchall())
    finally:
        db.close()
    assert any(stored[k] for k in seen)
    for key, value in seen.items():
        if rule == "nullify" or stored[key] is None:
            assert value is None
        elif len(stored[key]) > 4:
            assert value == "XXXXX" + stored[key][-4:]
        else:
            assert value == sha256_rendered(stored[key])


def test_a_tag_reader_change_changes_what_is_compiled_with_no_code_edit(
    access: Doc, columns: Doc, aggregates: Doc, warehouse: Path
) -> None:
    persona = reader(columns, "individual-id", "clear")
    moved = copy.deepcopy(columns)
    for tag in moved["tags"]:
        tag["clear"].remove(persona)
        tag["masked"].append(persona)

    def rules(doc: Doc) -> dict[str, str]:
        (rows,) = [
            t
            for t in plan_of(access, doc, aggregates, persona, warehouse).tables
            if isinstance(t, Rows) and t.table == "payments"
        ]
        return {c.name: c.rule for c in rows.columns if c.name in tagged(columns)}

    assert set(rules(columns).values()) == {"clear"}
    assert set(rules(moved).values()) == {"hash"}


# The minimum group.


def test_a_group_totals_query_over_fewer_than_the_minimum_group_returns_nothing(
    as_persona: AsPersona, access: Doc, aggregates: Doc, warehouse: Path
) -> None:
    minimum = aggregates["minimum_group"]
    small = builder(
        warehouse, "select group_total_key from group_totals where figures < ?", minimum
    )
    assert small, "the samples hold a group-totals row of fewer figures than the minimum"
    aggregate = sorted(
        g["persona"]
        for g in access["grants"]
        if g["product"] == "group-totals" and g["access"] == "aggregate"
    )
    clear = next(
        g["persona"]
        for g in access["grants"]
        if g["product"] == "group-totals" and g["access"] == "read"
    )
    assert aggregate
    for persona in [*aggregate, clear]:
        db = as_persona(persona)
        for (key,) in small:
            got = db.execute(
                "select * from group_totals where group_total_key = ?", [key]
            ).fetchall()
            assert (got == []) == (persona != clear), (persona, key)
        if persona != clear:
            for table in ("group_totals", "group_totals_left_out"):
                (smallest,) = db.execute(f"select min(figures) from {table}").fetchone() or (None,)  # noqa: S608
                assert smallest is None or smallest >= minimum, (persona, table)


def test_the_minimum_is_one_catalogue_line(
    as_persona: AsPersona,
    access: Doc,
    columns: Doc,
    aggregates: Doc,
    warehouse: Path,
    tmp_path: Path,
) -> None:
    persona = next(g["persona"] for g in access["grants"] if g["access"] == "aggregate")
    lower = copy.deepcopy(aggregates)
    lower["minimum_group"] = 2
    expected = builder(
        warehouse, "select group_total_key from group_totals where figures >= 2 order by 1"
    )
    assert expected, "the samples hold a group-totals row of at least two figures"
    db = open_persona(access, columns, lower, persona, warehouse, tmp_path)
    try:
        seen = db.execute("select group_total_key from group_totals order by 1").fetchall()
    finally:
        db.close()
    assert seen == expected


def test_an_aggregate_reader_of_payments_gets_payee_counts_above_the_minimum_and_no_rows(
    as_persona: AsPersona, access: Doc, aggregates: Doc, warehouse: Path
) -> None:
    minimum = aggregates["minimum_group"]
    persona = next(
        g["persona"]
        for g in access["grants"]
        if g["product"] == "payments" and g["access"] == "aggregate"
    )
    db = as_persona(persona)
    with pytest.raises(duckdb.CatalogException):
        db.execute("select * from payments")
    grain = "entity_id, currency, fiscal_year, fiscal_period, is_repeat"
    expected = builder(
        warehouse,
        f"select {grain}, sum(amount), count(distinct vendor_name) from payments"  # noqa: S608
        f" group by all having count(distinct vendor_name) >= ? order by all",
        minimum,
    )
    seen = f"select {grain}, amount, payees from payments_by_period order by all"  # noqa: S608
    assert db.execute(seen).fetchall() == expected
    # The grain holds no day and no payee: a narrower question has no column to filter on.
    held = {
        c
        for (c,) in db.execute(
            "select column_name from duckdb_columns() where table_name = 'payments_by_period'"
        ).fetchall()
    }
    assert held.isdisjoint({"payment_date", "vendor_name", "payment_key"})


# Generated from the catalogues, refusing what it cannot express.


def test_every_aggregate_table_sums_a_table_of_a_known_product(
    access: Doc, aggregates: Doc
) -> None:
    tables = {p["id"]: p.get("tables", []) for p in access["data_products"]}
    names = [v["name"] for views in aggregates["products"].values() for v in views]
    assert len(names) == len(set(names))
    for product, views in aggregates["products"].items():
        assert product in tables, product
        assert {v["from"] for v in views} <= set(tables[product]), product
    granted = {g["product"] for g in access["grants"] if g["access"] == "aggregate"}
    assert {p for p in granted if tables[p]} <= set(aggregates["products"])


def test_the_compile_is_deterministic(
    access: Doc, columns: Doc, aggregates: Doc, warehouse: Path, persona: str
) -> None:
    first = statements(plan_of(access, columns, aggregates, persona, warehouse))
    assert first == statements(
        plan_of(copy.deepcopy(access), columns, aggregates, persona, warehouse)
    )


def refusal(change: str, access: Doc, columns: Doc, aggregates: Doc) -> tuple[Doc, Doc, Doc, str]:
    """The three catalogues with one line changed, and the persona to compile."""
    access, columns, aggregates = (copy.deepcopy(d) for d in (access, columns, aggregates))
    persona = "accountant-chicago"
    if change == "unknown-access":
        access["grants"].append({"persona": persona, "product": "audit-log", "access": "export"})
    elif change == "unknown-masking":
        columns["tags"][0]["masking"] = "redact"
    elif change == "unknown-tag":
        columns["products"]["payments"]["columns"]["payments.amount"] = "no-such-tag"
    elif change == "missing-column":
        columns["products"]["payments"]["columns"]["payments.no_such_column"] = "individual-id"
    elif change == "tag-outside-the-product":
        columns["products"]["payments"]["columns"]["group_totals.basis"] = "individual-id"
    elif change == "missing-table":
        access["data_products"][0]["tables"] = ["payments", "no_such_table"]
    elif change == "aggregate-of-a-tagged-column":
        aggregates["products"]["payments"][0]["group_by"].append("vendor_name")
        persona = "metric-owner"
    elif change == "aggregate-with-no-table":
        del aggregates["products"]["group-totals"]
    elif change == "minimum-of-one":
        aggregates["minimum_group"] = 1
    elif change == "clear-and-masked":
        columns["tags"][0]["clear"].append(persona)
    elif change == "rows-and-aggregate":
        access["grants"].append({"persona": persona, "product": "group-totals", "access": "read"})
    elif change == "hash-on-a-number":
        columns["products"]["payments"]["columns"]["payments.amount"] = "individual-id"
    return access, columns, aggregates, persona


@pytest.mark.parametrize(
    "change",
    [
        "unknown-access",
        "unknown-masking",
        "unknown-tag",
        "missing-column",
        "tag-outside-the-product",
        "missing-table",
        "aggregate-of-a-tagged-column",
        "aggregate-with-no-table",
        "minimum-of-one",
        "clear-and-masked",
        "rows-and-aggregate",
        "hash-on-a-number",
    ],
)
def test_the_compile_refuses_what_it_cannot_express(
    access: Doc, columns: Doc, aggregates: Doc, warehouse: Path, change: str
) -> None:
    changed = refusal(change, access, columns, aggregates)
    with pytest.raises(CompileError):
        plan_of(*changed, warehouse)


def test_an_entity_scoped_table_without_an_entity_column_is_refused(
    access: Doc, columns: Doc, aggregates: Doc, warehouse: Path
) -> None:
    # Invented: the payments table as a source schema without its entity column.
    schema = source_schema(warehouse)
    schema["payments"] = [c for c in schema["payments"] if c[0] != "entity_id"]
    with pytest.raises(CompileError, match="entity_id"):
        compile_persona(access, columns, aggregates, "accountant-chicago", schema)


# The routes around the rules: none reaches the warehouse, a file or a setting.


def test_no_query_a_persona_writes_reaches_past_its_database(
    as_persona: AsPersona, warehouse: Path, persona: str
) -> None:
    from ingest.fetch import landing_path

    db = as_persona(persona)
    landing = landing_path().resolve()
    for reach in (
        f"attach '{warehouse}' as source (read_only)",
        f"select * from '{warehouse}'",  # noqa: S608
        f"select * from read_blob('{warehouse}')",  # noqa: S608
        f"select * from read_parquet('{landing}/*/*.parquet')",  # noqa: S608
        f"select * from read_parquet('{warehouse.parent}/*.parquet')",  # noqa: S608
        f"select * from glob('{landing}/*')",  # noqa: S608
        "select * from read_csv('/etc/passwd')",
        "copy (select 1) to 'out.csv'",
        "export database 'out'",
        "install httpfs",
        "set enable_external_access = true",
        "reset enable_external_access",
        "set lock_configuration = false",
        "set allowed_directories = ['/']",
        "attach ':memory:' as scratch",
        "create table forged as select 1 as a",
    ):
        with pytest.raises(duckdb.Error):
            db.execute(reach)
    # What the catalogue shows is the persona's own database and nothing of the source.
    assert {
        d
        for (d,) in db.execute(
            "select database_name from duckdb_databases() where not internal"
        ).fetchall()
    } == {persona}
    assert db.execute("select sql from duckdb_views() where not internal").fetchall() == []
    sources = db.execute(
        "select count(*) from duckdb_tables()"
        " where sql ilike '%warehouse%' or sql ilike '%ledger_lab%'"
    ).fetchone()
    assert sources == (0,)


# Over the whole landed build.

# What the compiled personas read over the landed files today, counted independently below.
LANDED_PAYMENTS = {"chicago": 475609, "uk-transport": 31103}
# Group-totals rows of at least the minimum group's figures, of 108; payments groups of at least
# the minimum group's payees, of 108.
LANDED_GROUP_TOTALS_KEPT = 88
LANDED_PAYMENTS_BY_PERIOD_KEPT = 98
LANDED_SOURCES = (
    "chicago-contracts",
    "chicago-payments",
    "gleif-legal-entities",
    "uk-transport-spend-2025",
    "ecb-rates",
    "nl-government-budget",
)


@pytest.fixture(scope="module")
def landed_warehouse(tmp_path_factory: pytest.TempPathFactory) -> Path:
    from ingest.fetch import landing_path
    from tests.models.dbt_build import built

    for name in LANDED_SOURCES:
        landed_file(name)
    return built(tmp_path_factory.mktemp("dbt-landed"), "landed", landing_path().resolve()).database


def reads_aggregate(access: Doc, aggregates: Doc, persona: str, table: str) -> bool:
    return any(
        g["persona"] == persona
        and g["access"] == "aggregate"
        and table in {v["name"] for v in aggregates["products"][g["product"]]}
        for g in access["grants"]
    )


@pytest.mark.landed
def test_every_persona_reads_what_its_rules_give_over_the_landed_build(
    access: Doc, columns: Doc, aggregates: Doc, landed_warehouse: Path, tmp_path: Path
) -> None:
    minimum = aggregates["minimum_group"]
    personal = "vendor_name, document_number, vendor_person_key"
    stored = {
        key: list(row)
        for key, *row in builder(landed_warehouse, f"select payment_key, {personal} from payments")  # noqa: S608
    }
    counted = builder(landed_warehouse, "select entity_id, count(*) from payments group by 1")
    assert dict(counted) == LANDED_PAYMENTS
    for persona in (p["id"] for p in access["personas"]):
        directory = tmp_path / persona
        directory.mkdir()
        db = open_persona(access, columns, aggregates, persona, landed_warehouse, directory)
        try:
            held = {t for (t,) in db.execute("select table_name from duckdb_tables()").fetchall()}
            entities = {
                g["entity"]
                for g in access["grants"]
                if g["persona"] == persona and g["product"] == "payments" and g["access"] == "read"
            }
            assert ("payments" in held) == bool(entities), persona
            if entities:
                seen = db.execute("select entity_id, count(*) from payments group by 1").fetchall()
                assert dict(seen) == {e: n for e, n in LANDED_PAYMENTS.items() if e in entities}
                masked = persona not in columns["tags"][0]["clear"]
                for key, *values in db.execute(
                    f"select payment_key, {personal} from payments"  # noqa: S608
                ).fetchall():
                    want = stored[key]
                    if masked:
                        want = [None if v is None else sha256_rendered(v) for v in want]
                    assert values == want, (persona, key)
            for table, units, kept in (
                ("group_totals", "figures", LANDED_GROUP_TOTALS_KEPT),
                ("payments_by_period", "payees", LANDED_PAYMENTS_BY_PERIOD_KEPT),
            ):
                if reads_aggregate(access, aggregates, persona, table):
                    count, smallest = db.execute(
                        f"select count(*), min({units}) from {table}"  # noqa: S608
                    ).fetchone() or (0, 0)
                    assert (count, smallest >= minimum) == (kept, True), (persona, table)
        finally:
            db.close()
            shutil.rmtree(directory)
