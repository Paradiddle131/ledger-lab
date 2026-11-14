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
import yaml

from access.compile import (
    Aggregate,
    CompileError,
    Plan,
    Rows,
    compile_persona,
    open_persona,
    schema_of,
    source_schema,
    statements,
)
from tests.conftest import REPO, landed_file, load_purposes

AsPersona = Callable[[str], duckdb.DuckDBPyConnection]
Doc = dict[str, Any]
PURPOSES = load_purposes()


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


def plan_of(
    access: Doc,
    columns: Doc,
    aggregates: Doc,
    persona: str,
    warehouse: Path,
    purposes: Doc = PURPOSES,
) -> Plan:
    return compile_persona(access, columns, aggregates, purposes, persona, source_schema(warehouse))


def held_as(persona: str, table: str) -> str:
    """Where the persona reads a product table: under its first purpose that lists the table."""
    return next(
        f"{schema_of(p['id'])}.{table}"
        for p in PURPOSES["purposes"]
        if persona in p["personas"] and table in p["tables"]
    )


# The row rule.


def test_an_entity_accountant_sees_no_other_entitys_row(
    as_persona: AsPersona, access: Doc, warehouse: Path
) -> None:
    for persona, entity in accountants(access).items():
        db = as_persona(persona)
        mine = held_as(persona, "payments")
        seen = db.execute(f"select entity_id, count(*) from {mine} group by entity_id").fetchall()  # noqa: S608
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
        assert db.execute(f"select * from {mine} where payment_key = ?", [key]).fetchall() == []  # noqa: S608


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
            persona = reader(columns, tag, kind)
            db = as_persona(persona)
            mine = held_as(persona, "payments")
            seen = dict(db.execute(f"select payment_key, {column} from {mine}").fetchall())  # noqa: S608
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
    persona = reader(columns, "individual-id", "masked")
    db = as_persona(persona)
    mine = held_as(persona, "payments")
    (entity,) = {e for (e,) in db.execute(f"select distinct entity_id from {mine}").fetchall()}  # noqa: S608
    ((name, payments),) = builder(
        warehouse,
        "select vendor_name, count(*) from payments where entity_id = ?"
        " group by 1 order by 2 desc, 1 limit 1",
        entity,
    )
    count = f"select count(*) from {mine} where vendor_name = ?"  # noqa: S608
    assert db.execute(count, [name]).fetchone() == (0,)
    assert db.execute(count, [sha256_rendered(name)]).fetchone() == (payments,)
    # Equal stored values mask alike, so a masked column still joins and counts.
    assert (
        db.execute(f"select count(distinct vendor_name) from {mine}").fetchone()  # noqa: S608
        == builder(
            warehouse,
            "select count(distinct vendor_name) from payments where entity_id = ?",
            entity,
        )[0]
    )


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
    db = open_persona(access, invented, aggregates, PURPOSES, persona, warehouse, tmp_path)
    try:
        mine = held_as(persona, "payments")
        seen = dict(db.execute(f"select payment_key, contract_number from {mine}").fetchall())  # noqa: S608
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

# The spans an all-entity group-totals figure is read by, and its figures in euros.
SPAN_FIGURES = "period_start, period_end, sum(figures), sum(amount_at_constant_rates), max(payees)"


def aggregate_readers(access: Doc, product: str) -> list[str]:
    return sorted(
        {
            g["persona"]
            for g in access["grants"]
            if g["product"] == product and g["access"] == "aggregate"
        }
    )


def test_a_group_totals_query_over_fewer_than_the_minimum_group_returns_nothing(
    as_persona: AsPersona, access: Doc, aggregates: Doc, warehouse: Path
) -> None:
    minimum = aggregates["minimum_group"]
    small = builder(
        warehouse,
        "select period_start, period_end from group_totals group by all having max(payees) < ?",
        minimum,
    )
    assert small, "the samples hold a span of fewer payees than the minimum"
    clear = next(
        g["persona"]
        for g in access["grants"]
        if g["product"] == "group-totals" and g["access"] == "read"
    )
    readers = aggregate_readers(access, "group-totals")
    assert readers
    for persona in [*readers, clear]:
        db = as_persona(persona)
        table = held_as(persona, "group_totals") if persona == clear else "group_totals"
        span = f"select * from {table} where period_start = ? and period_end = ?"  # noqa: S608
        for start, end in small:
            got = db.execute(span, [start, end]).fetchall()
            assert (got == []) == (persona != clear), (persona, start)
        if persona != clear:
            for table in ("group_totals", "group_totals_left_out"):
                (smallest,) = db.execute(f"select min(payees) from {table}").fetchone() or (None,)  # noqa: S608
                assert smallest is None or smallest >= minimum, (persona, table)


def kept_spans(warehouse: Path, own: str, minimum: int, entities: int) -> list[tuple[Any, ...]]:
    """The all-entity spans a reader of `own`'s rows is given, counted here apart from the compiler:
    the span's rows of other entities hold `minimum` payees and `entities` entities."""
    return builder(
        warehouse,
        f"select {SPAN_FIGURES} from group_totals group by all"  # noqa: S608
        " having max(case when entity_id <> ? then payees end) >= ?"
        " and count(distinct case when entity_id <> ? then entity_id end) >= ? order by all",
        own,
        minimum,
        own,
        entities,
    )


def test_the_minimum_is_one_catalogue_line(
    access: Doc, columns: Doc, aggregates: Doc, warehouse: Path, tmp_path: Path
) -> None:
    lower = copy.deepcopy(aggregates)
    lower["minimum_group"] = 2
    lower["minimum_entities"] = 1
    own = accountants(access)
    persona = aggregate_readers(access, "group-totals")[0]
    expected = kept_spans(warehouse, own[persona], 2, 1)
    assert expected, "the samples hold a span of at least two payees of another entity"
    db = open_persona(access, columns, lower, PURPOSES, persona, warehouse, tmp_path)
    try:
        seen = db.execute(
            "select period_start, period_end, figures,"
            " amount_at_constant_rates, payees from group_totals order by all"
        ).fetchall()
    finally:
        db.close()
    assert seen == expected


def test_aggregate_group_totals_are_all_entity_figures_until_the_owner_chooses(
    access: Doc, columns: Doc, aggregates: Doc, warehouse: Path
) -> None:
    persona = aggregate_readers(access, "group-totals")[0]

    def grain(doc: Doc) -> set[str]:
        (table,) = [
            t
            for t in plan_of(access, columns, doc, persona, warehouse).tables
            if isinstance(t, Aggregate) and t.name == "group_totals"
        ]
        return set(table.group_by) | set(table.sums)

    entity = {
        "entity_id",
        "basis",
        "currency",
        "amount",
        "group_total_key",
        "amount_at_actual_rates",
    }
    assert grain(aggregates).isdisjoint(entity)
    per_entity = copy.deepcopy(aggregates)
    per_entity["products"]["group-totals"]["entity_rows"] = True
    assert entity <= grain(per_entity)


# The entities apart: an all-entity figure is never one entity's, nor the persona's own less.


def test_an_all_entity_figure_holds_two_entities_the_persona_reads_no_rows_of(
    access: Doc, columns: Doc, aggregates: Doc, warehouse: Path, tmp_path: Path
) -> None:
    assert aggregates["minimum_entities"] == 2
    lower = copy.deepcopy(aggregates)
    lower["minimum_group"] = 2
    own = accountants(access)
    seen: dict[str, list[tuple[Any, ...]]] = {}
    for persona in aggregate_readers(access, "group-totals"):
        directory = tmp_path / persona
        directory.mkdir()
        db = open_persona(access, columns, lower, PURPOSES, persona, warehouse, directory)
        try:
            seen[persona] = db.execute(
                "select period_start, period_end, figures,"
                " amount_at_constant_rates, payees from group_totals order by all"
            ).fetchall()
        finally:
            db.close()
        assert seen[persona] == kept_spans(warehouse, own[persona], 2, 2), persona
    # Over the samples a span of two other entities exists for one reader and not for another,
    # and at one entity apart the reader of Chicago would be given a span less its own.
    assert any(seen.values()) and not all(seen.values())
    assert kept_spans(warehouse, "chicago", 2, 1) != kept_spans(warehouse, "chicago", 2, 2)


def test_the_entities_apart_count_the_entities_a_persona_reads_in_any_product(
    access: Doc, columns: Doc, aggregates: Doc, warehouse: Path
) -> None:
    persona = aggregate_readers(access, "group-totals")[0]
    # Invented: the persona also reads the UK's payments, so it knows the UK's share too.
    wider = copy.deepcopy(access)
    wider["grants"].append(
        {"persona": persona, "entity": "uk-transport", "product": "payments", "access": "read"}
    )

    def known(catalogue: Doc) -> frozenset[str] | None:
        (table,) = [
            t
            for t in plan_of(catalogue, columns, aggregates, persona, warehouse).tables
            if isinstance(t, Aggregate) and t.name == "group_totals"
        ]
        assert table.minimum_entities == aggregates["minimum_entities"]
        return table.known

    assert known(access) == {accountants(access)[persona]}
    assert known(wider) == {accountants(access)[persona], "uk-transport"}
    # Each entity's own rows need no entity apart: the metric owner's payments by period.
    (by_period,) = [
        t
        for t in plan_of(access, columns, aggregates, "metric-owner", warehouse).tables
        if isinstance(t, Aggregate) and t.name == "payments_by_period"
    ]
    assert (by_period.known, by_period.minimum_entities) == (None, 0)


def test_an_entity_read_per_entity_in_aggregate_counts_as_known(
    access: Doc, columns: Doc, aggregates: Doc, warehouse: Path
) -> None:
    # Invented: the metric owner reads the group totals in aggregate too, beside its payments by
    # entity and period, whose per-entity sums it could take away from a figure across entities.
    catalogue = copy.deepcopy(access)
    catalogue["grants"] = [
        g
        for g in catalogue["grants"]
        if not (g["persona"] == "metric-owner" and g["product"] == "group-totals")
    ]
    catalogue["grants"].append(
        {"persona": "metric-owner", "product": "group-totals", "access": "aggregate"}
    )
    purposes = copy.deepcopy(PURPOSES)
    purposes["purposes"] = [p for p in purposes["purposes"] if "metric-owner" not in p["personas"]]
    (totals,) = [
        t
        for t in plan_of(catalogue, columns, aggregates, "metric-owner", warehouse, purposes).tables
        if isinstance(t, Aggregate) and t.name == "group_totals"
    ]
    per_entity = {
        g["entity"]
        for g in access["grants"]
        if g["persona"] == "metric-owner" and g["product"] == "payments"
    }
    assert per_entity and totals.known == per_entity


def test_an_all_entity_table_shows_one_translated_amount(
    access: Doc, aggregates: Doc, warehouse: Path
) -> None:
    """The same figures at two sets of rates are two equations in the entities' own-currency
    amounts: with two entities and the public rates they split a group (shown over the landed
    build below). One amount and a count of figures are one equation in amounts per group."""
    schema = {t: dict(c) for t, c in source_schema(warehouse).items()}
    for entry in aggregates["products"].values():
        if entry["entity_rows"]:
            continue
        for table in entry["tables"]:
            amounts = [s for s in table["sum"] if schema[table["from"]][s].startswith("DECIMAL")]
            assert len(amounts) <= 1, (table["name"], amounts)


# The kept groups over the samples at the minimum of ten, per aggregate reader and table: a change
# to the minimum, the entities apart, a grain or a units count moves them. The readers of one
# entity's payments differ on the all-entity tables, since each takes away its own entity.
GROUP = ("group_totals", "group_totals_left_out")
SAMPLE_KEPT = {
    "accountant-chicago": dict(zip(GROUP, (0, 1), strict=True)),
    "accountant-nl-government": dict(zip(GROUP, (0, 1), strict=True)),
    "accountant-uk-transport": dict(zip(GROUP, (0, 0), strict=True)),
    "approver": dict(zip(GROUP, (0, 1), strict=True)),
    "metric-owner": {"payments_by_period": 1},
}
SAMPLE_KEPT_AT_TWO = {
    "accountant-chicago": dict(zip(GROUP, (0, 1), strict=True)),
    "accountant-nl-government": dict(zip(GROUP, (1, 1), strict=True)),
    "accountant-uk-transport": dict(zip(GROUP, (0, 3), strict=True)),
    "approver": dict(zip(GROUP, (0, 1), strict=True)),
    "metric-owner": {"payments_by_period": 25},
}


@pytest.mark.parametrize("minimum", [None, 2])
def test_the_kept_groups_over_the_samples_are_pinned(
    access: Doc,
    columns: Doc,
    aggregates: Doc,
    warehouse: Path,
    tmp_path: Path,
    minimum: int | None,
) -> None:
    # The pins are counted at ten. A minimum of eight or less keeps more groups over the samples;
    # they hold no group of exactly nine payees, so this line is what a minimum of nine turns red.
    assert aggregates["minimum_group"] == 10
    doc = copy.deepcopy(aggregates)
    if minimum is not None:
        doc["minimum_group"] = minimum
    kept: dict[str, dict[str, int]] = {}
    tables = {v["name"] for entry in doc["products"].values() for v in entry["tables"]}
    for persona in sorted(
        {*aggregate_readers(access, "group-totals"), *aggregate_readers(access, "payments")}
    ):
        directory = tmp_path / persona
        directory.mkdir()
        db = open_persona(access, columns, doc, PURPOSES, persona, warehouse, directory)
        try:
            for table in sorted(tables):
                if reads_aggregate(access, doc, persona, table):
                    (n,) = db.execute(f"select count(*) from {table}").fetchone() or (0,)  # noqa: S608
                    kept.setdefault(persona, {})[table] = n
        finally:
            db.close()
            shutil.rmtree(directory)
    assert kept == (SAMPLE_KEPT if minimum is None else SAMPLE_KEPT_AT_TWO)


def test_the_evals_measure_against_the_access_rules_minimum(aggregates: Doc) -> None:
    evals = yaml.safe_load((REPO / "agent/evals/adversarial.yaml").read_text())
    assert evals["minimum_group"] == aggregates["minimum_group"]


def test_an_aggregate_reader_of_payments_gets_payee_counts_above_the_minimum_and_no_rows(
    as_persona: AsPersona, access: Doc, aggregates: Doc, warehouse: Path
) -> None:
    minimum = aggregates["minimum_group"]
    persona = aggregate_readers(access, "payments")[0]
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


def test_an_aggregate_reader_sees_only_its_granted_entities(
    access: Doc, columns: Doc, aggregates: Doc, warehouse: Path, tmp_path: Path
) -> None:
    persona = aggregate_readers(access, "payments")[0]
    lower = copy.deepcopy(aggregates)
    lower["minimum_group"] = 2
    one = copy.deepcopy(access)
    one["grants"] = [
        g
        for g in one["grants"]
        if not (
            g["persona"] == persona
            and g["product"] == "payments"
            and g.get("entity") not in (None, "chicago")
        )
    ]
    seen: dict[str, set[str]] = {}
    for name, catalogue in (("all", access), ("chicago", one)):
        directory = tmp_path / name
        directory.mkdir()
        db = open_persona(catalogue, columns, lower, PURPOSES, persona, warehouse, directory)
        try:
            rows = db.execute("select distinct entity_id from payments_by_period").fetchall()
            seen[name] = {e for (e,) in rows}
        finally:
            db.close()
            shutil.rmtree(directory)
    assert seen == {"all": {"chicago", "uk-transport"}, "chicago": {"chicago"}}


# Generated from the catalogues, refusing what it cannot express.


def test_every_aggregate_table_sums_a_table_of_a_known_product(
    access: Doc, aggregates: Doc
) -> None:
    tables = {p["id"]: p.get("tables", []) for p in access["data_products"]}
    entries = aggregates["products"]
    names = [v["name"] for entry in entries.values() for v in entry["tables"]]
    assert len(names) == len(set(names))
    for product, entry in entries.items():
        assert product in tables, product
        assert {v["from"] for v in entry["tables"]} <= set(tables[product]), product
    granted = {g["product"] for g in access["grants"] if g["access"] == "aggregate"}
    assert {p for p in granted if tables[p]} <= set(entries)


def test_the_compile_is_deterministic(
    access: Doc, columns: Doc, aggregates: Doc, warehouse: Path, persona: str
) -> None:
    first = statements(plan_of(access, columns, aggregates, persona, warehouse))
    assert first == statements(
        plan_of(copy.deepcopy(access), columns, aggregates, persona, warehouse)
    )


# Each change to the catalogues the compiler cannot express, with the refusal it must give.
REFUSALS = {
    "unknown-access": "no rule for the access",
    "unknown-masking": "no masking rule",
    "unknown-tag": "no tag",
    "missing-column": "has no no_such_column",
    "tag-outside-the-product": "is not a table of payments",
    "missing-table": "no table no_such_table",
    "aggregate-of-a-tagged-column": "shows a tagged column",
    "aggregate-with-no-table": "no aggregate table",
    "all-entity-grouped-by-entity": "all-entity figures grouped by",
    "unknown-units": "units count no column",
    "minimum-of-one": "minimum group",
    "no-entity-apart": "minimum entities",
    "clear-and-masked": "both in clear and masked",
    "rows-and-aggregate": "both by rows and in aggregate",
    "hash-on-a-number": "masks strings only",
    "purpose-for-an-aggregate-reader": "reads group-totals only in aggregate",
    "purpose-with-no-grant": "reads group-totals with no row grant",
    "purpose-column-the-product-lacks": "payments has no no_such_column",
    "purpose-table-of-no-product": "fx_rates is no product's table",
    "purpose-tag-read-neither-way": "neither in clear nor masked",
    "row-grant-with-no-purpose": "holds no purpose of it",
    "purpose-without-the-entity": "without its entity_id",
    "purpose-of-an-unknown-persona": "no persona nobody",
    "two-purposes-of-one-id": "share an id",
    "purpose-in-a-schema-the-engine-keeps": "a schema the engine keeps",
}


def refusal(
    change: str, access: Doc, columns: Doc, aggregates: Doc
) -> tuple[Doc, Doc, Doc, str, Doc]:
    """The four catalogues with one line changed, and the persona to compile."""
    access, columns, aggregates = (copy.deepcopy(d) for d in (access, columns, aggregates))
    purposes = copy.deepcopy(PURPOSES)
    persona = "accountant-chicago"
    by_id = {p["id"]: p for p in purposes["purposes"]}
    review = by_id["payment-review"]
    totals = aggregates["products"]["group-totals"]["tables"][0]
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
        aggregates["products"]["payments"]["tables"][0]["group_by"].append("vendor_name")
        persona = "metric-owner"
    elif change == "aggregate-with-no-table":
        del aggregates["products"]["group-totals"]
    elif change == "all-entity-grouped-by-entity":
        totals["group_by"].append("entity_id")
    elif change == "unknown-units":
        totals["units"] = {"name": "payees", "sum": "payees"}
    elif change == "minimum-of-one":
        aggregates["minimum_group"] = 1
    elif change == "no-entity-apart":
        aggregates["minimum_entities"] = 0
    elif change == "clear-and-masked":
        columns["tags"][0]["clear"].append(persona)
    elif change == "rows-and-aggregate":
        access["grants"].append({"persona": persona, "product": "group-totals", "access": "read"})
    elif change == "hash-on-a-number":
        columns["products"]["payments"]["columns"]["payments.amount"] = "individual-id"
    elif change == "purpose-for-an-aggregate-reader":
        by_id["group-close"]["personas"].append(persona)
    elif change == "purpose-with-no-grant":
        persona = "payables-clerk"
        by_id["group-close"]["personas"].append(persona)
    elif change == "purpose-column-the-product-lacks":
        review["tables"]["payments"].append("no_such_column")
    elif change == "purpose-table-of-no-product":
        review["tables"]["fx_rates"] = ["currency"]
    elif change == "purpose-tag-read-neither-way":
        for tag in columns["tags"]:
            tag["masked"].remove(persona)
    elif change == "row-grant-with-no-purpose":
        review["personas"].remove(persona)
    elif change == "purpose-without-the-entity":
        review["tables"]["payments"].remove("entity_id")
    elif change == "purpose-of-an-unknown-persona":
        by_id["audit"]["personas"].append("nobody")
    elif change == "two-purposes-of-one-id":
        purposes["purposes"].append(copy.deepcopy(review))
    elif change == "purpose-in-a-schema-the-engine-keeps":
        review["id"] = "main"
    return access, columns, aggregates, persona, purposes


@pytest.mark.parametrize("change", list(REFUSALS))
def test_the_compile_refuses_what_it_cannot_express(
    access: Doc, columns: Doc, aggregates: Doc, warehouse: Path, change: str
) -> None:
    *changed, purposes = refusal(change, access, columns, aggregates)
    with pytest.raises(CompileError, match=REFUSALS[change]):
        plan_of(*changed, warehouse, purposes)


def test_an_entity_scoped_table_without_an_entity_column_is_refused(
    access: Doc, columns: Doc, aggregates: Doc, warehouse: Path
) -> None:
    # Invented: the payments table as a source schema without its entity column.
    schema = source_schema(warehouse)
    schema["payments"] = [c for c in schema["payments"] if c[0] != "entity_id"]
    with pytest.raises(CompileError, match="entity_id"):
        compile_persona(access, columns, aggregates, PURPOSES, "accountant-chicago", schema)


def test_an_all_entity_table_without_an_entity_column_is_refused(
    access: Doc, columns: Doc, aggregates: Doc, warehouse: Path
) -> None:
    # Invented: the group totals as a source schema without its entity column, so the entities
    # apart cannot be counted.
    schema = source_schema(warehouse)
    schema["group_totals"] = [c for c in schema["group_totals"] if c[0] != "entity_id"]
    with pytest.raises(CompileError, match="no entity_id to keep apart"):
        compile_persona(access, columns, aggregates, PURPOSES, "accountant-chicago", schema)


# The products the models build no table of yet, acted out on invented tables.


def test_the_products_without_tables_yet_follow_their_grants_on_invented_tables(
    access: Doc, columns: Doc, aggregates: Doc, warehouse: Path, tmp_path: Path
) -> None:
    # Invented: a ledger, the metric definitions and the audit log as tables, one row per entity
    # for the ledger, in a copy of the sample build; the catalogue names them as the products'.
    source = tmp_path / "invented.duckdb"
    with duckdb.connect(str(source)) as db:
        db.execute(f"attach '{warehouse}' as sample (read_only)")
        for (table,) in db.execute(
            "select table_name from duckdb_tables() where database_name = 'sample'"
        ).fetchall():
            db.execute(f'create table "{table}" as select * from sample.main."{table}"')  # noqa: S608
        db.execute("detach sample")
        entities = [e["id"] for e in access["entities"]]
        db.execute("create table ledger (entity_id varchar, entry varchar)")
        db.executemany("insert into ledger values (?, 'invented')", [[e] for e in entities])
        db.execute("create table metric_definitions as select 'invented' as metric")
        db.execute("create table audit_log as select 'invented' as answer")
    catalogue = copy.deepcopy(access)
    invented = {
        "ledger": "ledger",
        "metric-definitions": "metric_definitions",
        "audit-log": "audit_log",
    }
    for product in catalogue["data_products"]:
        if product["id"] in invented:
            product["tables"] = [invented[product["id"]]]

    def reads(persona: str, product: str) -> set[str | None]:
        return {
            g.get("entity")
            for g in access["grants"]
            if g["persona"] == persona
            and g["product"] == product
            and g["access"] in {"read", "edit"}
        }

    # Invented: one purpose per invented product, of all its columns, for its row readers.
    whole = {
        "ledger": ["entity_id", "entry"],
        "metric_definitions": ["metric"],
        "audit_log": ["answer"],
    }
    wider = copy.deepcopy(PURPOSES)
    for product, table in invented.items():
        readers = sorted(p["id"] for p in access["personas"] if reads(p["id"], product))
        wider["purposes"].append(
            {
                "id": f"invented-{product}",
                "for": "invented",
                "personas": readers,
                "tables": {table: whole[table]},
            }
        )

    for persona in (p["id"] for p in access["personas"]):
        directory = tmp_path / persona
        directory.mkdir()
        db = open_persona(catalogue, columns, aggregates, wider, persona, source, directory)
        try:
            held = {t for (t,) in db.execute("select table_name from duckdb_tables()").fetchall()}
            for product, table in invented.items():
                assert (table in held) == bool(reads(persona, product)), (persona, table)
            if "ledger" in held:
                rows = db.execute(
                    "select distinct entity_id from invented_ledger.ledger"
                ).fetchall()
                assert {e for (e,) in rows} == reads(persona, "ledger"), persona
        finally:
            db.close()
            shutil.rmtree(directory)
    # Stated apart from the grants: the auditor reads Chicago's ledger only, the agent operator
    # the audit log and the definitions only, and drafting or posting an entry reads no ledger.
    expected = {
        "auditor": ({"chicago"}, True),
        "agent-operator": (set(), True),
        "approver": (set(), False),
        "proposer": (set(), False),
    }
    for persona, (ledger, audit) in expected.items():
        assert reads(persona, "ledger") == ledger, persona
        assert bool(reads(persona, "audit-log")) == audit, persona
    readers = {g["persona"] for g in access["grants"] if g["product"] == "metric-definitions"}
    assert readers == {p["id"] for p in access["personas"]} - {"proposer"}


# The routes around the rules: none reaches the warehouse, a file or a setting.


def escapes(warehouse: Path, data_file: Path) -> list[str]:
    return [
        f"attach '{warehouse}' as source (read_only)",
        f"select * from '{warehouse}'",  # noqa: S608
        f"select * from read_blob('{warehouse}')",  # noqa: S608
        f"select * from read_parquet('{data_file}')",  # noqa: S608
        f"select * from glob('{data_file.parent}/*')",  # noqa: S608
        "select * from read_csv('/etc/passwd')",
        "copy (select 1) to 'out.csv'",
        "export database 'out'",
        "install httpfs",
        "set enable_external_access = true",
        "reset enable_external_access",
        "set lock_configuration = false",
        "set autoload_known_extensions = true",
        "set allowed_directories = ['/']",
        "attach ':memory:' as scratch",
        "create table forged as select 1 as a",
    ]


def test_no_query_a_persona_writes_reaches_past_its_database(
    as_persona: AsPersona, warehouse: Path, tmp_path: Path, persona: str
) -> None:
    # Invented: a one-row Parquet file the builder writes, which exists, so reading it fails only
    # because the persona's connection reaches no file.
    data_file = tmp_path / "invented.parquet"
    with duckdb.connect() as db:
        db.execute(f"copy (select 'invented' as value) to '{data_file}' (format parquet)")
    db = as_persona(persona)
    for reach in escapes(warehouse, data_file):
        with pytest.raises(duckdb.Error):
            db.execute(reach)
    for read in escapes(warehouse, data_file)[:5]:
        with pytest.raises(duckdb.PermissionException):
            db.execute(read)
    for setting in (
        "enable_external_access",
        "autoload_known_extensions",
        "autoinstall_known_extensions",
    ):
        assert db.execute(f"select current_setting('{setting}')").fetchone() == (False,), setting
    assert db.execute("select current_setting('lock_configuration')").fetchone() == (True,)
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


@pytest.mark.landed
def test_no_persona_reads_the_landed_payments_file(
    as_persona: AsPersona, access: Doc, warehouse: Path
) -> None:
    landed = landed_file("chicago-payments")
    with duckdb.connect() as db:
        builder_reads = f"select count(*) > 0 from read_parquet('{landed}')"  # noqa: S608
        assert db.execute(builder_reads).fetchone() == (True,)
    for persona in (p["id"] for p in access["personas"]):
        db = as_persona(persona)
        with pytest.raises(duckdb.PermissionException):
            db.execute(f"select * from read_parquet('{landed}')")  # noqa: S608


# Over the whole landed build.

# What the compiled personas read over the landed files today, counted independently below.
LANDED_PAYMENTS = {"chicago": 475609, "uk-transport": 31103}
# The groups each aggregate reader's tables keep at the minimum. Of the 49 spans, 35 hold one
# entity's figures and 14 two entities', and each of the 7 years and rules left out holds one
# entity's: a reader of Chicago's payments (an accountant, the approver) keeps no span, since
# every span less Chicago is one entity's or none; the UK's accountant keeps the 2 spans of Chicago
# and the Dutch government, the Dutch accountant the 12 of Chicago and the UK. The metric owner
# keeps 98 of the 108 entity periods.
LANDED_KEPT = {
    "accountant-chicago": dict(zip(GROUP, (0, 0), strict=True)),
    "accountant-nl-government": dict(zip(GROUP, (12, 0), strict=True)),
    "accountant-uk-transport": dict(zip(GROUP, (2, 0), strict=True)),
    "approver": dict(zip(GROUP, (0, 0), strict=True)),
    "metric-owner": {"payments_by_period": 98},
}


@pytest.mark.landed
def test_two_amounts_at_two_rates_would_split_a_kept_span_over_the_landed_build(
    access: Doc, columns: Doc, aggregates: Doc, landed_warehouse: Path, tmp_path: Path
) -> None:
    """Why a figure across all entities shows one amount: the UK accountant's kept spans are the
    Dutch government's and Chicago's, and the Dutch figures are in euros, the reporting currency,
    so at both rates they are equal and the actual-rate total less the constant-rate one would be
    Chicago's alone, which with the public rates gives Chicago's figure."""
    persona = "accountant-uk-transport"
    db = open_persona(access, columns, aggregates, PURPOSES, persona, landed_warehouse, tmp_path)
    try:
        held = {
            c
            for (c,) in db.execute(
                "select column_name from duckdb_columns() where table_name = 'group_totals'"
            ).fetchall()
        }
        spans = db.execute("select period_start, period_end from group_totals").fetchall()
    finally:
        db.close()
    assert "amount_at_actual_rates" not in held and "amount_at_constant_rates" in held
    assert len(spans) == LANDED_KEPT[persona]["group_totals"]
    for start, end in spans:
        ((entities, euro_equal, whole, chicago),) = builder(
            landed_warehouse,
            "select string_agg(distinct entity_id, ',' order by entity_id),"
            " bool_and(currency <> reporting_currency"
            " or amount_at_actual_rates = amount_at_constant_rates),"
            " sum(amount_at_actual_rates - amount_at_constant_rates),"
            " sum(amount_at_actual_rates - amount_at_constant_rates)"
            " filter (where entity_id = 'chicago')"
            " from group_totals where period_start = ? and period_end = ?",
            start,
            end,
        )
        assert entities == "chicago,nl-government" and euro_equal
        assert whole == chicago and chicago != 0


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
        and table in {v["name"] for v in aggregates["products"][g["product"]]["tables"]}
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
    # Each group-totals row's payees, counted again from its figures' payments.
    assert builder(
        landed_warehouse,
        "select count(*) from group_totals as t join ("
        " select f.entity_id, f.basis, f.period_start, f.period_end,"
        " count(distinct coalesce(p.vendor_name, f.figure_key)) as payees"
        " from int_group_totals__figures as f left join payments as p"
        " on p.payment_key = f.figure_key where f.left_out_by is null group by all"
        ") as again using (entity_id, basis, period_start, period_end)"
        " where t.payees <> again.payees",
    ) == [(0,)]
    for persona in (p["id"] for p in access["personas"]):
        directory = tmp_path / persona
        directory.mkdir()
        db = open_persona(
            access, columns, aggregates, PURPOSES, persona, landed_warehouse, directory
        )
        try:
            held = {
                f"{schema}.{t}"
                for schema, t in db.execute(
                    "select schema_name, table_name from duckdb_tables()"
                ).fetchall()
            }
            entities = {
                g["entity"]
                for g in access["grants"]
                if g["persona"] == persona and g["product"] == "payments" and g["access"] == "read"
            }
            lists = {
                f"{schema_of(p['id'])}.payments": p["tables"]["payments"]
                for p in PURPOSES["purposes"]
                if persona in p["personas"] and "payments" in p["tables"]
            }
            assert bool(lists) == bool(entities) and "main.payments" not in held, persona
            for table, listed in lists.items():
                assert table in held, (persona, table)
                seen = db.execute(f"select entity_id, count(*) from {table} group by 1").fetchall()  # noqa: S608
                assert dict(seen) == {e: n for e, n in LANDED_PAYMENTS.items() if e in entities}
                masked = persona not in columns["tags"][0]["clear"]
                shown = [c for c in personal.split(", ") if c in listed]
                if not shown:
                    continue
                for key, *values in db.execute(
                    f"select payment_key, {', '.join(shown)} from {table}"  # noqa: S608
                ).fetchall():
                    want = [
                        v
                        for c, v in zip(personal.split(", "), stored[key], strict=True)
                        if c in shown
                    ]
                    if masked:
                        want = [None if v is None else sha256_rendered(v) for v in want]
                    assert values == want, (persona, key)
            for table, kept in LANDED_KEPT.get(persona, {}).items():
                assert reads_aggregate(access, aggregates, persona, table), (persona, table)
                count, smallest = db.execute(
                    f"select count(*), min(payees) from {table}"  # noqa: S608
                ).fetchone() or (0, None)
                assert count == kept, (persona, table)
                assert smallest is None or smallest >= minimum, (persona, table)
        finally:
            db.close()
            shutil.rmtree(directory)
