"""The row access policies on the entity-scoped products (`access/row_policies.py`), generated from
the persona mapping, and the governed copy they sit on.

Offline: the policies follow the grants, and a grant change moves them; the statements the owner
runs; a publish that checks the copy's policies and columns before it writes a row, and refuses a
copy whose policies or columns differ; the command refusing a session with no lab project. Live,
on the owner's machine (`bigquery_persona`, `make bigquery-personas`): each human persona reads
exactly its granted entities' rows of the published copy, and one with no row grant on it cannot
read it.
"""

import copy
import json
import re
from collections.abc import Callable, Sequence
from typing import Any

import pytest

from access import row_policies
from access.bigquery import PersonaQueryError, PersonaSession
from access.identities import identity
from access.row_policies import (
    AGENT_POLICY,
    Policy,
    RowPolicyError,
    check_policies,
    main,
    plan,
    policies,
    publish,
    publish_sql,
)
from tests.conftest import load_columns

PROJECT = "the-lab"
WHERE = "us-central1"
AGENT = "lab-agent"


def readers(access: dict[str, Any], product: str, entity: str) -> set[str]:
    """Who the grants give `entity`'s rows of `product`, read independently of the module."""
    return {
        g["persona"]
        for g in access["grants"]
        if g["product"] == product and g.get("entity") == entity and g["access"] in ("read", "edit")
    }


def clear(columns: dict[str, Any], product: str) -> set[str]:
    """Who reads every tag on the product's columns in clear."""
    tags = set(columns["products"][product]["columns"].values())
    return set.intersection(*(set(t["clear"]) for t in columns["tags"] if t["id"] in tags))


def test_each_entity_policy_grants_its_rows_to_the_personas_that_read_them(
    access: dict[str, Any],
    columns: dict[str, Any],
) -> None:
    found = [p for p in policies(access, columns) if p.table == "payments"]
    by_name = {p.name: p for p in found}
    for entity in (e["id"] for e in access["entities"]):
        want = readers(access, "payments", entity) & clear(columns, "payments")
        name = f"{entity.replace('-', '_')}_rows"
        if not want:
            assert name not in by_name
            continue
        policy = by_name[name]
        assert (policy.dataset, policy.table) == ("payments", "payments")
        assert policy.condition == f"entity_id = '{entity}'"
        assert set(policy.personas) == want
        assert not policy.agent
    agent = by_name[AGENT_POLICY]
    assert (agent.condition, agent.personas, agent.agent) == ("TRUE", (), True)
    assert [p.name for p in found][-1] == AGENT_POLICY


def test_an_aggregate_grant_gets_no_policy(access: dict[str, Any], columns: dict[str, Any]) -> None:
    aggregate = {g["persona"] for g in access["grants"] if g["product"] == "payments"} - {
        p for e in access["entities"] for p in readers(access, "payments", e["id"])
    }
    assert aggregate, "the catalogue has an aggregate-only reader of payments to hold"
    assert not aggregate & {p for policy in policies(access, columns) for p in policy.personas}


def test_a_new_grant_moves_the_policies(access: dict[str, Any], columns: dict[str, Any]) -> None:
    changed = copy.deepcopy(access)
    changed["grants"].append(
        {
            "persona": "payables-clerk",
            "entity": "nl-government",
            "product": "payments",
            "access": "read",
        }
    )
    nl = next(
        p
        for p in policies(changed, columns)
        if (p.table, p.name) == ("payments", "nl_government_rows")
    )
    assert "payables-clerk" in nl.personas
    before = next(
        p
        for p in policies(access, columns)
        if (p.table, p.name) == ("payments", "nl_government_rows")
    )
    assert "payables-clerk" not in before.personas


def test_the_masked_copy_grants_each_entitys_rows_to_its_masked_readers(
    access: dict[str, Any], columns: dict[str, Any]
) -> None:
    found = [p for p in policies(access, columns) if p.table == "payments_masked"]
    for entity in (e["id"] for e in access["entities"]):
        want = readers(access, "payments", entity) - clear(columns, "payments")
        named = [p for p in found if p.name == f"{entity.replace('-', '_')}_rows"]
        assert [set(p.personas) for p in named] == ([want] if want else [])
        assert all(p.condition == f"entity_id = '{entity}'" for p in named)
    assert found[-1].name == AGENT_POLICY and found[-1].agent


def test_every_row_reader_gets_one_copy_of_each_entity_it_reads(
    access: dict[str, Any], columns: dict[str, Any]
) -> None:
    granted: dict[tuple[str, str], list[str]] = {}
    for p in policies(access, columns):
        for persona in p.personas:
            granted.setdefault((persona, p.name), []).append(p.table)
    for entity in (e["id"] for e in access["entities"]):
        for persona in readers(access, "payments", entity):
            assert len(granted[(persona, f"{entity.replace('-', '_')}_rows")]) == 1


def test_a_masked_reader_gets_no_rows_of_the_clear_table_until_it_reads_every_tag_clear(
    access: dict[str, Any], columns: dict[str, Any]
) -> None:
    chicago = next(
        p for p in policies(access, columns) if (p.table, p.name) == ("payments", "chicago_rows")
    )
    assert "accountant-chicago" not in chicago.personas
    changed = copy.deepcopy(columns)
    for tag in changed["tags"]:
        tag["clear"].append("accountant-chicago")
        tag["masked"].remove("accountant-chicago")
    chicago = next(
        p for p in policies(access, changed) if (p.table, p.name) == ("payments", "chicago_rows")
    )
    assert "accountant-chicago" in chicago.personas


def test_the_owner_statements_grant_each_policy_to_its_accounts(
    access: dict[str, Any], columns: dict[str, Any]
) -> None:
    statements = plan(access, columns, PROJECT, AGENT)
    assert len(statements) == len(policies(access, columns))
    chicago = next(s for s in statements if "`chicago_rows` ON `the-lab.payments.payments`" in s)
    assert chicago.startswith(
        f"CREATE OR REPLACE ROW ACCESS POLICY `chicago_rows` ON `{PROJECT}.payments.payments`"
        " GRANT TO ("
    )
    assert chicago.endswith(" FILTER USING (entity_id = 'chicago');")
    for persona in readers(access, "payments", "chicago") & clear(columns, "payments"):
        assert f'"serviceAccount:{persona}@{PROJECT}.iam.gserviceaccount.com"' in chicago
    agent = next(s for s in statements if f"`{AGENT_POLICY}`" in s and ".payments`" in s)
    assert f'"serviceAccount:{AGENT}@{PROJECT}.iam.gserviceaccount.com"' in agent
    assert agent.endswith(" FILTER USING (TRUE);")


@pytest.mark.parametrize(
    ("project", "dataset", "table"),
    [
        ("Bad Project", "payments", "payments"),
        (PROJECT, "pay`ments", "payments"),
        (PROJECT, "payments", "x; drop"),
    ],
)
def test_a_name_outside_its_shape_is_refused(project: str, dataset: str, table: str) -> None:
    with pytest.raises(RowPolicyError):
        row_policies.publish_sql(project, dataset, table)


class Warehouse:
    """A stand-in for `bq`: the policies and columns it reports, and each statement it was given."""

    def __init__(
        self,
        found: list[dict[str, str]],
        source: list[dict[str, str]],
        copy_columns: list[dict[str, str]] | None = None,
    ) -> None:
        self.found = found
        self.source = source
        self.copy = source if copy_columns is None else copy_columns
        self.statements: list[str] = []

    def __call__(self, command: Sequence[str]) -> str:
        assert command[:2] == ["bq", f"--project_id={PROJECT}"]
        assert f"--maximum_bytes_billed={row_policies.MAX_BYTES_BILLED}" in command
        sql = command[-1]
        self.statements.append(sql)
        if "ROW_ACCESS_POLICIES" in sql:
            table = re.search(r"table_name = '([a-z_]+)'", sql)
            assert table
            return json.dumps([r for r in self.found if r["table"] == table.group(1)])
        if "INFORMATION_SCHEMA.COLUMNS" in sql:
            return json.dumps(self.source if ".intermediate." in sql else self.copy)
        return ""


def reported(found: Sequence[Policy]) -> list[dict[str, str]]:
    # BigQuery reports a filter as it normalises it; spacing and keyword case differ.
    return [
        {
            "table": p.table,
            "row_access_policy_name": p.name,
            "filter_predicate": p.condition.replace(" = ", "="),
        }
        for p in found
    ]


COLUMNS = [
    {"column_name": "payment_key", "data_type": "STRING"},
    {"column_name": "entity_id", "data_type": "STRING"},
]


def test_a_publish_checks_then_replaces_the_rows_in_one_transaction(
    access: dict[str, Any],
    columns: dict[str, Any],
) -> None:
    warehouse = Warehouse(reported(policies(access, columns)), COLUMNS)
    publish(access, columns, PROJECT, WHERE, warehouse)
    writes = [s for s in warehouse.statements if not s.startswith("SELECT ")]
    assert writes == [publish_sql(PROJECT, "payments", t) for t in ("payments", "payments_masked")]
    assert writes[0] == (
        f"BEGIN TRANSACTION; DELETE FROM `{PROJECT}.payments.payments` WHERE TRUE;"  # noqa: S608
        f" INSERT INTO `{PROJECT}.payments.payments` SELECT * FROM"
        f" `{PROJECT}.intermediate.payments`; COMMIT TRANSACTION;"
    )


def drop(found: list[dict[str, str]], name: str, table: str = "payments") -> list[dict[str, str]]:
    return [r for r in found if (r["table"], r["row_access_policy_name"]) != (table, name)]


EDITS: dict[str, Callable[[list[dict[str, str]]], list[dict[str, str]]]] = {
    "no policy": lambda found: [],
    "the masked copy's missing one": lambda found: drop(found, "chicago_rows", "payments_masked"),
    "the masked copy's with none": lambda found: [r for r in found if r["table"] == "payments"],
    "one missing": lambda found: drop(found, "chicago_rows"),
    "the agent's missing": lambda found: drop(found, AGENT_POLICY),
    "one not generated": lambda found: [
        *found,
        {"table": "payments", "row_access_policy_name": "everyone", "filter_predicate": "TRUE"},
    ],
    "one with another filter": lambda found: [
        {**r, "filter_predicate": "TRUE"} if r["row_access_policy_name"] == "chicago_rows" else r
        for r in found
    ],
}


@pytest.mark.parametrize("name", list(EDITS))
def test_a_copy_without_exactly_its_policies_gets_no_rows(
    access: dict[str, Any], columns: dict[str, Any], name: str
) -> None:
    warehouse = Warehouse(EDITS[name](reported(policies(access, columns))), COLUMNS)
    with pytest.raises(RowPolicyError, match="no rows are published"):
        publish(access, columns, PROJECT, WHERE, warehouse)
    assert not any("INSERT" in s or "DELETE" in s for s in warehouse.statements)


def test_a_copy_whose_columns_differ_from_the_mart_gets_no_rows(
    access: dict[str, Any], columns: dict[str, Any]
) -> None:
    warehouse = Warehouse(reported(policies(access, columns)), COLUMNS, COLUMNS[:1])
    with pytest.raises(RowPolicyError, match="columns differ"):
        publish(access, columns, PROJECT, WHERE, warehouse)
    assert not any("INSERT" in s or "DELETE" in s for s in warehouse.statements)


def test_the_check_compares_names_and_filters_only() -> None:
    policy = Policy("payments", "payments", "chicago_rows", "entity_id = 'chicago'", ("auditor",))
    check_policies(
        [policy],
        [{"row_access_policy_name": "chicago_rows", "filter_predicate": "entity_id='chicago'"}],
        "payments",
        "payments",
    )


@pytest.mark.parametrize(
    "env",
    [
        {},
        {
            "LEDGER_LAB_GCP_PROJECT": PROJECT,
            "GCP_PROJECT": "another-lab",
            "LEDGER_LAB_BQ_LOCATION": WHERE,
            "LEDGER_LAB_AGENT_ACCOUNT": AGENT,
        },
    ],
    ids=["no lab project", "another lab's session"],
)
def test_the_command_refuses_a_session_without_the_lab_project(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def no_gcp(command: Sequence[str]) -> str:
        raise AssertionError("a GCP call was made")

    monkeypatch.setattr(row_policies, "run_tool", no_gcp)
    for command in ("plan", "apply", "create", "publish"):
        assert main([command], env) == 1
    err = capsys.readouterr().err
    assert "another-lab" not in err


@pytest.mark.bigquery_persona
@pytest.mark.parametrize("table", ["payments", "payments_masked"])
def test_each_human_persona_reads_only_its_entities_rows_of_its_copy(
    as_persona_on_bigquery: Callable[[str], PersonaSession],
    access: dict[str, Any],
    human: str,
    table: str,
) -> None:
    from gcp_project import lab_project

    granted = identity(access, human).reads.get("payments")
    ours = (human in clear(load_columns(), "payments")) == (table == "payments")
    sql = f"SELECT DISTINCT entity_id FROM `{lab_project()}.payments.{table}`"  # noqa: S608
    session = as_persona_on_bigquery(human)
    if granted is None:
        with pytest.raises(PersonaQueryError) as refused:
            session.query(sql)
        if refused.value.status != 403:
            pytest.fail("a persona with no row grant on payments was not refused")
        return
    seen = {row[0] for row in session.query(sql).rows}
    if seen != (set(granted) if ours else set()):
        pytest.fail("the persona read other entities' rows of this copy than its grants give")
