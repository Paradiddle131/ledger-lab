"""BigQuery's aggregate views (`access/thresholds.py`), generated from `access/aggregates.yaml` and
the persona mapping.

Offline: one view per aggregate table with readers, in its product's aggregates dataset, reading
an entity-scoped product's mart (never its governed copy) or a group product's own table; a
`count_distinct` table under an aggregation threshold policy on its units column at the minimum
group, a `max` table grouped at its grain with the minimum in a HAVING; the readers' entities as
the filter, and readers who differ refused; the command refusing a session with no lab project.
Live, on the owner's machine (`bigquery_persona`, `make bigquery-thresholds`): each aggregate
reader's answer holds exactly the groups of at least the minimum, checked against the source as
an identity that reads it; a finer grain drops its small groups; a raw read of the privacy unit
is refused; and a persona with no aggregate grant on the product reads none of its views.
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


def test_a_count_distinct_table_is_an_aggregation_threshold_view_on_its_units(
    access: dict[str, Any], aggregates: dict[str, Any]
) -> None:
    table = aggregates["products"]["payments"]["tables"][0]
    view = by_name(views(access, aggregates))[table["name"]]
    sql = view.sql(PROJECT)
    rule = json.loads(re.search(r"privacy_policy = '([^']+)'", sql)[1])  # type: ignore[index]
    assert rule == {
        "aggregation_threshold_policy": {
            "threshold": aggregates["minimum_group"],
            "privacy_unit_column": table["units"]["count_distinct"],
        }
    }
    shown = re.search(r" as select (.+) from ", sql)[1].split(", ")  # type: ignore[index]
    assert shown == [
        *table["group_by"],
        *table["entity_group_by"],
        *table["sum"],
        table["units"]["count_distinct"],
    ]
    assert " group by " not in sql and " having " not in sql
    assert sql.startswith(
        f"create or replace view `{PROJECT}.payments_aggregates.payments_by_period`"
    )
    assert f"from `{PROJECT}.intermediate.payments` where entity_id in (" in sql


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
        assert ('"threshold": 25' in statement) or ("having max(source.payees) >= 25" in statement)


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


def payments_groups(
    project: str, keys: list[str], minimum: int
) -> tuple[set[tuple[Any, ...]], int]:
    """The groups of the metric owner's entities' payments at `keys` of at least the minimum
    distinct payees, and how many groups there are in all: a finer grain has far more groups than
    one page of an answer, so only the kept ones are listed."""
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
    ((total,),) = as_publisher(project, f"SELECT COUNT(*) FROM ({grouped})")  # noqa: S608
    return set(kept), int(total)


def threshold_query(project: str, keys: list[str]) -> str:
    return (
        f"SELECT WITH AGGREGATION_THRESHOLD {', '.join(keys)}, SUM(amount)"  # noqa: S608
        f" FROM `{project}.payments_aggregates.payments_by_period` GROUP BY {', '.join(keys)}"
    )


@pytest.mark.bigquery_persona
@pytest.mark.parametrize("finer", [False, True], ids=["its grain", "a finer grain"])
def test_the_payments_view_returns_exactly_the_groups_of_at_least_the_minimum(
    as_persona_on_bigquery: Callable[[str], PersonaSession],
    access: dict[str, Any],
    aggregates: dict[str, Any],
    finer: bool,
) -> None:
    from gcp_project import lab_project

    project = lab_project()
    keys = [*PAYMENTS_KEYS, "amount"] if finer else PAYMENTS_KEYS
    kept, total = payments_groups(project, keys, aggregates["minimum_group"])
    if not kept:
        pytest.fail("no group holds the minimum, so the view's answer proves nothing")
    if finer and len(kept) == total:
        pytest.fail(
            "no group at the finer grain is under the minimum: the small-cell probe is idle"
        )
    for reader in sorted(aggregate_readers(access, "payments")):
        rows = as_persona_on_bigquery(reader).query(threshold_query(project, keys)).rows
        if {row[:-1] for row in rows} != kept:
            pytest.fail(f"{reader}: the view returned other groups than those of the minimum")


@pytest.mark.bigquery_persona
def test_a_raw_read_of_the_privacy_unit_is_refused(
    as_persona_on_bigquery: Callable[[str], PersonaSession], access: dict[str, Any]
) -> None:
    from gcp_project import lab_project

    sql = f"SELECT vendor_name FROM `{lab_project()}.payments_aggregates.payments_by_period`"  # noqa: S608
    for reader in sorted(aggregate_readers(access, "payments")):
        with pytest.raises(PersonaQueryError):
            as_persona_on_bigquery(reader).query(sql)


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
