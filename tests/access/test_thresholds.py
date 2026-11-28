"""BigQuery's aggregate views (`access/thresholds.py`), generated from `access/aggregates.yaml` and
the persona mapping.

Offline: one view per aggregate table with readers, in its product's aggregates dataset, reading
an entity-scoped product's mart (never its governed copy) or a group product's own table; each
table grouped at its grain with the minimum in a HAVING over its units (`count_distinct` or
`max`) bound to the source's column, and no analysis rule; the payments view, run on DuckDB over
invented rows, holding only its kept group and no row-level column, so no two of a reader's
answers subtract to a group under the minimum; the readers' entities as the filter, and readers
who differ refused; the command refusing a session with no lab project.
Live, on the owner's machine (`bigquery_persona`, `make bigquery-thresholds`): each aggregate
reader's answer holds exactly the groups of at least the minimum, checked against the source as
an identity that reads it; a column finer than the payments view's grain (the payee, a payment's
day) is refused as one the view lacks; and a persona with no aggregate grant on the product reads
none of its views.
"""

import copy
import json
import os
import re
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from access import thresholds
from access.bigquery import PersonaQueryError, PersonaSession, chain, mint, service_account
from access.row_policies import PUBLISHER, RowPolicyError
from access.thresholds import ThresholdError, View, aggregates_dataset, main, plan, views
from tests.conftest import load_aggregates
from tests.platform.test_access_rules import expected_aggregate_datasets

PROJECT = "the-lab"


@pytest.fixture
def aggregates() -> dict[str, Any]:
    return load_aggregates()


def aggregate_readers(access: dict[str, Any], product: str) -> set[str]:
    """Who holds an aggregate grant on the product, read independently of the module."""
    return {
        g["persona"]
        for g in access["grants"]
        if g["product"] == product and g["access"] == "aggregate"
    }


def by_name(found: list[View]) -> dict[str, View]:
    return {v.name: v for v in found}


def test_each_aggregate_table_with_readers_is_one_view_in_its_products_dataset(
    access: dict[str, Any], aggregates: dict[str, Any]
) -> None:
    found = views(access, aggregates)
    expected = {
        (aggregates_dataset(product), table["name"], table["from"])
        for product, entry in aggregates["products"].items()
        if aggregate_readers(access, product)
        for table in entry["tables"]
    }
    assert {(v.dataset, v.name, v.source) for v in found} == expected
    assert len(found) == len(expected)
    # The datasets platform/gcp declares and grants are the module's.
    assert {v.dataset for v in found} <= set(expected_aggregate_datasets().values())


def test_an_entity_scoped_products_views_read_its_mart_and_a_group_products_its_own_table(
    access: dict[str, Any], aggregates: dict[str, Any]
) -> None:
    found = by_name(views(access, aggregates))
    assert found["payments_by_period"].source_dataset == "intermediate"
    assert found["group_totals"].source_dataset == "group_totals"
    assert found["group_totals_left_out"].source_dataset == "group_totals"


def test_a_count_distinct_table_is_grouped_at_its_grain_with_the_minimum_in_a_having(
    access: dict[str, Any], aggregates: dict[str, Any]
) -> None:
    table = aggregates["products"]["payments"]["tables"][0]
    view = by_name(views(access, aggregates))[table["name"]]
    keys = [*table["group_by"], *table["entity_group_by"]]
    of, units = table["units"]["count_distinct"], table["units"]["name"]
    listed = ", ".join(f"'{e}'" for e in sorted(view.entities or ()))
    assert view.sql(PROJECT) == (
        f"create or replace view `{PROJECT}.payments_aggregates.payments_by_period` as select "  # noqa: S608
        + ", ".join(keys)
        + "".join(f", sum({c}) as {c}" for c in table["sum"])
        + f", count(distinct {of}) as {units} from `{PROJECT}.intermediate.payments` as source"
        + f" where entity_id in ({listed}) group by {', '.join(keys)}"
        + f" having count(distinct source.{of}) >= {aggregates['minimum_group']}"
    )


# Invented payments as the payments mart holds them, for the reader's differencing attacks: one
# entity's group of twelve payees, beside two small groups the minimum drops (another entity's
# two payees, the first entity's next period of three).
DIFFERENCING_COLUMNS = (
    "currency text, fiscal_year int, fiscal_period int, is_repeat bool, entity_id text,"
    " amount double, vendor_name text, payment_date date"
)
DIFFERENCING_ROWS = [
    *(("USD", 2025, 1, False, "chicago", 100.0, f"payee-c{i:02}", "2025-01-15") for i in range(12)),
    *(("USD", 2025, 2, False, "chicago", 50.0, f"payee-d{i:02}", "2025-02-15") for i in range(3)),
    ("GBP", 2025, 1, False, "uk-transport", 7777.0, "payee-u00", "2025-01-20"),
    ("GBP", 2025, 1, False, "uk-transport", 1234.0, "payee-u01", "2025-01-21"),
]
# What the minimum withholds: each small group's total, and each of its payments.
WITHHELD = {150.0, 50.0, 9011.0, 7777.0, 1234.0}


def emulated(sql: str) -> tuple[Any, Callable[[str], float]]:
    """The view's statement run on DuckDB over the invented payments, and a reader's answer to
    `select sum(amount) from <view> where <filter>`; a view under an aggregation threshold policy
    answers only when the answer's one group holds the threshold's distinct privacy units, as
    BigQuery's `SELECT WITH AGGREGATION_THRESHOLD` checks each query's groups."""
    import duckdb

    statement = re.sub(rf"`{PROJECT}\.([a-z_]+)\.([a-z_]+)`", r"\1.\2", sql)
    body = statement.split(" as select ", 1)[1]
    db = duckdb.connect()
    db.execute("create schema intermediate; create schema payments_aggregates")
    db.execute(f"create table intermediate.payments ({DIFFERENCING_COLUMNS})")
    db.executemany(
        "insert into intermediate.payments values (?, ?, ?, ?, ?, ?, ?, ?)", DIFFERENCING_ROWS
    )
    db.execute(f"create view payments_aggregates.payments_by_period as select {body}")
    policy = re.search(r"privacy_policy = '([^']+)'", sql)
    having = ""
    if policy is not None:
        rule = json.loads(policy[1])["aggregation_threshold_policy"]
        having = f" having count(distinct {rule['privacy_unit_column']}) >= {rule['threshold']}"

    def answer(where: str) -> float:
        found = db.execute(
            "select sum(amount) from payments_aggregates.payments_by_period"  # noqa: S608
            f" where {where}{having}"
        ).fetchall()
        return float(found[0][0] or 0) if found else 0.0

    return db, answer


def test_no_two_answers_of_the_payments_view_subtract_to_a_group_under_the_minimum(
    access: dict[str, Any], aggregates: dict[str, Any]
) -> None:
    """A reader's filters on the view's own columns, each answer passing any check BigQuery
    makes, and the differences of each pair: none is a withheld group's total or payment, and the
    view's rows are the one kept group, with no row-level column to filter or read."""
    assert aggregates["minimum_group"] == 10  # the invented groups are sized against it
    view = by_name(views(access, aggregates))["payments_by_period"]
    db, answer = emulated(view.sql(PROJECT))
    filters = [
        "fiscal_year = 2025",
        "entity_id = 'chicago'",
        "entity_id <> 'uk-transport'",
        "fiscal_period = 1",
        "currency = 'USD'",
        "amount < 5000",
        "amount >= 5000",
    ]
    answers = {f: answer(f) for f in filters}
    for first, a in answers.items():
        assert a not in WITHHELD, first
        for second, b in answers.items():
            assert a - b not in WITHHELD, (first, second)
    table = aggregates["products"]["payments"]["tables"][0]
    shown = [
        c
        for (c,) in db.execute(
            "select column_name from duckdb_columns() where table_name = 'payments_by_period'"
            " order by column_index"
        ).fetchall()
    ]
    assert shown == [
        *table["group_by"],
        *table["entity_group_by"],
        *table["sum"],
        table["units"]["name"],
    ]
    assert "vendor_name" not in shown and "payment_date" not in shown
    rows = db.execute(
        "select entity_id, fiscal_period, amount, payees"
        " from payments_aggregates.payments_by_period"
    ).fetchall()
    assert rows == [("chicago", 1, 1200.0, 12)]


def test_the_view_holds_its_readers_entities_and_no_other(
    access: dict[str, Any], aggregates: dict[str, Any]
) -> None:
    (reader,) = aggregate_readers(access, "payments")
    granted = {
        g["entity"]
        for g in access["grants"]
        if g["persona"] == reader and g["product"] == "payments"
    }
    view = by_name(views(access, aggregates))["payments_by_period"]
    assert view.entities == frozenset(granted)
    listed = re.search(r"entity_id in \(([^)]*)\)", view.sql(PROJECT))[1]  # type: ignore[index]
    assert {e.strip("'") for e in listed.split(", ")} == granted
    # A group product's views carry no entity filter.
    assert " where " not in by_name(views(access, aggregates))["group_totals"].sql(PROJECT)


@pytest.mark.parametrize("name", ["group_totals", "group_totals_left_out"])
def test_a_max_table_is_grouped_at_its_grain_with_the_minimum_in_a_having(
    access: dict[str, Any], aggregates: dict[str, Any], name: str
) -> None:
    (table,) = [t for t in aggregates["products"]["group-totals"]["tables"] if t["name"] == name]
    sql = by_name(views(access, aggregates))[name].sql(PROJECT)
    keys = [*table["group_by"], *table["entity_group_by"]]
    sums = [*table["sum"], *table["entity_sum"]]
    assert "privacy_policy" not in sql
    assert sql == (
        f"create or replace view `{PROJECT}.group_totals_aggregates.{name}` as select "  # noqa: S608
        + ", ".join(keys)
        + "".join(f", sum({c}) as {c}" for c in sums)
        + f", max(payees) as payees from `{PROJECT}.group_totals.{table['from']}` as source"
        + f" group by {', '.join(keys)} having max(source.payees) >= {aggregates['minimum_group']}"
    )


def test_a_new_minimum_moves_every_view(access: dict[str, Any], aggregates: dict[str, Any]) -> None:
    changed = {**aggregates, "minimum_group": 25}
    for statement in plan(access, changed, PROJECT):
        assert statement.endswith(
            (
                "having count(distinct source.vendor_name) >= 25",
                "having max(source.payees) >= 25",
            )
        )


def test_readers_of_different_entities_are_refused(
    access: dict[str, Any], aggregates: dict[str, Any]
) -> None:
    widened = copy.deepcopy(access)
    widened["grants"].append(
        {"persona": "approver", "product": "payments", "entity": "chicago", "access": "aggregate"}
    )
    with pytest.raises(ThresholdError, match="different entities"):
        views(widened, aggregates)


def test_figures_across_all_entities_are_refused(
    access: dict[str, Any], aggregates: dict[str, Any]
) -> None:
    switched = copy.deepcopy(aggregates)
    switched["products"]["group-totals"]["entity_rows"] = False
    with pytest.raises(ThresholdError, match="across all entities"):
        views(access, switched)


def test_a_product_with_no_aggregate_reader_gets_no_view(
    access: dict[str, Any], aggregates: dict[str, Any]
) -> None:
    none = copy.deepcopy(access)
    none["grants"] = [g for g in none["grants"] if g["product"] != "group-totals"]
    assert {v.dataset for v in views(none, aggregates)} == {"payments_aggregates"}


def test_a_name_outside_its_shape_is_refused(
    access: dict[str, Any], aggregates: dict[str, Any]
) -> None:
    odd = copy.deepcopy(aggregates)
    odd["products"]["payments"]["tables"][0]["name"] = "by period; drop"
    with pytest.raises(RowPolicyError, match="dataset or table name"):
        plan(access, odd, PROJECT)
    quoted = copy.deepcopy(access)
    for g in quoted["grants"]:
        if g["persona"] == "metric-owner" and g.get("entity") == "chicago":
            g["entity"] = "chi'cago"
    with pytest.raises(ThresholdError, match="entity"):
        plan(quoted, aggregates, PROJECT)


def test_the_command_refuses_a_session_without_the_lab_project(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_gcp(_: object) -> str:
        raise AssertionError("no GCP call outside the lab's environment")

    monkeypatch.setattr(thresholds, "run_tool", no_gcp)
    assert main(["apply"], {"GCP_PROJECT": "another-lab"}) == 1
    assert main(["plan"], {}) == 1
    assert "thresholds:" in capsys.readouterr().err
    assert main(["nothing"], {}) == 2


# Live, as each persona, on the owner's machine.

PAYMENTS_KEYS = ["entity_id", "currency", "fiscal_year", "fiscal_period", "is_repeat"]
REFERENCE: dict[tuple[str, str], list[tuple[Any, ...]]] = {}


def as_publisher(project: str, sql: str) -> list[tuple[Any, ...]]:
    """The publisher reads the marts in `intermediate`: the source the payments views read."""
    if (project, sql) not in REFERENCE:
        accounts = chain(service_account(PUBLISHER, project), project, os.environ)
        with httpx.Client(timeout=60) as client:
            REFERENCE[(project, sql)] = (
                PersonaSession(project, mint(accounts), client).query(sql).rows
            )
    return REFERENCE[(project, sql)]


def payments_groups(project: str, keys: list[str], minimum: int) -> set[tuple[Any, ...]]:
    """The groups of the metric owner's entities' payments at `keys` of at least the minimum
    distinct payees."""
    entities = ", ".join(f"'{e}'" for e in ("chicago", "nl-government", "uk-transport"))
    grouped = (
        f"SELECT {', '.join(keys)}, COUNT(DISTINCT vendor_name) AS payees FROM"  # noqa: S608
        f" `{project}.intermediate.payments` WHERE entity_id IN ({entities})"
        f" GROUP BY {', '.join(keys)}"
    )
    kept = as_publisher(
        project,
        f"SELECT {', '.join(keys)} FROM ({grouped}) WHERE payees >= {minimum}",  # noqa: S608
    )
    return set(kept)


@pytest.mark.bigquery_persona
def test_the_payments_view_returns_exactly_the_groups_of_at_least_the_minimum(
    as_persona_on_bigquery: Callable[[str], PersonaSession],
    access: dict[str, Any],
    aggregates: dict[str, Any],
) -> None:
    """Its rows, read whole, are the source's groups at the grain of at least the minimum, each
    with its distinct payees: every answer a reader writes over the view is one of these rows'."""
    from gcp_project import lab_project

    project = lab_project()
    minimum = aggregates["minimum_group"]
    kept = payments_groups(project, PAYMENTS_KEYS, minimum)
    if not kept:
        pytest.fail("no group holds the minimum, so the view's answer proves nothing")
    sql = (
        f"SELECT {', '.join(PAYMENTS_KEYS)}, payees"  # noqa: S608
        f" FROM `{project}.payments_aggregates.payments_by_period`"
    )
    for reader in sorted(aggregate_readers(access, "payments")):
        rows = as_persona_on_bigquery(reader).query(sql).rows
        if any(int(row[-1]) < minimum for row in rows) or {row[:-1] for row in rows} != kept:
            pytest.fail(f"{reader}: the view returned other groups than those of the minimum")


@pytest.mark.bigquery_persona
@pytest.mark.parametrize("column", ["vendor_name", "payment_date"])
def test_a_column_finer_than_the_payments_views_grain_is_refused(
    as_persona_on_bigquery: Callable[[str], PersonaSession], access: dict[str, Any], column: str
) -> None:
    """The payee and a payment's day are the source's, not the view's: a read of either, or a
    grouping finer than the grain by it, names a column the view does not have."""
    from gcp_project import lab_project

    view = f"`{lab_project()}.payments_aggregates.payments_by_period`"
    keys = ", ".join([*PAYMENTS_KEYS, column])
    queries = [
        f"SELECT {column} FROM {view}",  # noqa: S608
        f"SELECT {keys}, SUM(amount) FROM {view} GROUP BY {keys}",  # noqa: S608
    ]
    for reader in sorted(aggregate_readers(access, "payments")):
        for sql in queries:
            with pytest.raises(PersonaQueryError) as refused:
                as_persona_on_bigquery(reader).query(sql)
            if refused.value.status != 400:
                pytest.fail(f"{reader}: {column} was not refused as a name the view lacks")


@pytest.mark.bigquery_persona
@pytest.mark.parametrize("name", ["group_totals", "group_totals_left_out"])
def test_each_group_totals_view_returns_exactly_the_rows_of_at_least_the_minimum(
    as_persona_on_bigquery: Callable[[str], PersonaSession],
    access: dict[str, Any],
    aggregates: dict[str, Any],
    name: str,
) -> None:
    """Checked against the table as the auditor reads it, through its audit purpose's view of it
    (`access/purpose_views.py`), which lists every column of both tables and holds exactly the
    table's rows (`make bigquery-purposes`); the session's typed rows on both sides."""
    from gcp_project import lab_project

    project = lab_project()
    minimum = aggregates["minimum_group"]
    (table,) = [t for t in aggregates["products"]["group-totals"]["tables"] if t["name"] == name]
    keys = ", ".join([*table["group_by"], *table["entity_group_by"]])
    source = (
        as_persona_on_bigquery("auditor")
        .query(
            f"SELECT {keys}, MAX(payees) FROM `{project}.audit.{table['from']}`"  # noqa: S608
            f" GROUP BY {keys}"
        )
        .rows
    )
    kept = {row[:-1] for row in source if int(row[-1]) >= minimum}
    # The left-out table holds no row under the minimum in today's build, so only the group totals
    # show the filter at work; both views are still held to exactly the kept rows.
    if name == "group_totals" and len(kept) == len(source):
        pytest.fail("no group totals row is under the minimum: the view's filter proves nothing")
    for reader in sorted(aggregate_readers(access, "group-totals")):
        rows = (
            as_persona_on_bigquery(reader)
            .query(
                f"SELECT {keys}, payees FROM `{project}.group_totals_aggregates.{name}`"  # noqa: S608
            )
            .rows
        )
        if any(int(row[-1]) < minimum for row in rows) or {row[:-1] for row in rows} != kept:
            pytest.fail(f"{reader}: the view returned other rows than those of the minimum")


@pytest.mark.bigquery_persona
def test_a_persona_with_no_aggregate_grant_reads_none_of_a_products_views(
    as_persona_on_bigquery: Callable[[str], PersonaSession],
    access: dict[str, Any],
    aggregates: dict[str, Any],
    persona: str,
) -> None:
    from gcp_project import lab_project

    project = lab_project()
    for view in views(access, aggregates):
        product = expected_product(view.dataset)
        if persona in aggregate_readers(access, product):
            continue
        with pytest.raises(PersonaQueryError) as refused:
            as_persona_on_bigquery(persona).query(
                f"SELECT COUNT(*) FROM `{project}.{view.dataset}.{view.name}`"  # noqa: S608
            )
        if refused.value.status != 403:
            pytest.fail(f"{persona} was not refused {view.dataset}.{view.name} as unauthorized")


def expected_product(dataset: str) -> str:
    (product,) = [p for p, d in expected_aggregate_datasets().items() if d == dataset]
    return product
