"""The row access policies on the entity-scoped products (`access/row_policies.py`), generated from
the persona mapping, and the governed copy they sit on.

Offline: the policies follow the grants, and a grant change moves them; the statements the owner
runs; a publish that checks the copy's policies and columns before it writes a row, and refuses a
copy whose policies or columns differ; its write going as the publisher, minted through the agent;
the command refusing a session with no lab project. Live,
on the owner's machine (`bigquery_persona`, `make bigquery-row-policies`): each human persona reads
exactly its granted entities' rows of the published copy, and one with no row grant on it cannot
read it.
"""

import copy
import json
import re
from collections.abc import Callable, Sequence
from typing import Any

import httpx
import pytest

from access import row_policies
from access.bigquery import PersonaQueryError, PersonaSession
from access.identities import identity
from access.row_policies import (
    PUBLISHER,
    PUBLISHER_POLICY,
    Policy,
    RowPolicyError,
    check_policies,
    main,
    plan,
    policies,
    publish,
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
    found = policies(access, columns)
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
        assert not policy.publisher
    publisher = by_name[PUBLISHER_POLICY]
    assert (publisher.condition, publisher.personas, publisher.publisher) == ("TRUE", (), True)
    assert [p.name for p in found][-1] == PUBLISHER_POLICY


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
    nl = next(p for p in policies(changed, columns) if p.name == "nl_government_rows")
    assert "payables-clerk" in nl.personas
    before = next(p for p in policies(access, columns) if p.name == "nl_government_rows")
    assert "payables-clerk" not in before.personas


def test_a_masked_reader_gets_no_rows_of_the_clear_table_until_it_reads_every_tag_clear(
    access: dict[str, Any], columns: dict[str, Any]
) -> None:
    chicago = next(p for p in policies(access, columns) if p.name == "chicago_rows")
    assert "accountant-chicago" not in chicago.personas
    changed = copy.deepcopy(columns)
    for tag in changed["tags"]:
        tag["clear"].append("accountant-chicago")
        tag["masked"].remove("accountant-chicago")
    chicago = next(p for p in policies(access, changed) if p.name == "chicago_rows")
    assert "accountant-chicago" in chicago.personas


GRANT = re.compile(r" GRANT TO \(([^)]*)\) FILTER USING ")


def granted_to(statement: str) -> set[str]:
    """The accounts a statement's `GRANT TO (...)` list names, each one exactly as written."""
    found = GRANT.search(statement)
    assert found, statement
    names = [g.strip() for g in found.group(1).split(",")]
    assert all(n.startswith('"serviceAccount:') and n.endswith('"') for n in names), statement
    return {n[len('"serviceAccount:') : -1] for n in names}


def account(name: str) -> str:
    return f"{name}@{PROJECT}.iam.gserviceaccount.com"


def test_the_owner_statements_grant_each_policy_to_exactly_its_accounts(
    access: dict[str, Any], columns: dict[str, Any]
) -> None:
    statements = plan(access, columns, PROJECT)
    assert len(statements) == len(policies(access, columns))
    chicago = next(s for s in statements if "`chicago_rows`" in s)
    assert chicago.startswith(
        f"CREATE OR REPLACE ROW ACCESS POLICY `chicago_rows` ON `{PROJECT}.payments.payments`"
        " GRANT TO ("
    )
    assert chicago.endswith(" FILTER USING (entity_id = 'chicago');")
    entities = {f"`{e['id'].replace('-', '_')}_rows`": e["id"] for e in access["entities"]}
    named = 0
    for statement in statements:
        grantees = granted_to(statement)
        if f"`{PUBLISHER_POLICY}`" in statement:
            assert grantees == {account(PUBLISHER)}
            assert statement.endswith(" FILTER USING (TRUE);")
            continue
        entity = next(e for name, e in entities.items() if name in statement)
        want = readers(access, "payments", entity) & clear(columns, "payments")
        assert grantees == {account(p) for p in want}
        assert not grantees & {account(PUBLISHER), account(AGENT)}
        named += 1
    assert named == len(statements) - 1


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
        self.writes: list[str] = []

    def __call__(self, command: Sequence[str]) -> str:
        assert command[:2] == ["bq", f"--project_id={PROJECT}"]
        assert f"--maximum_bytes_billed={row_policies.MAX_BYTES_BILLED}" in command
        sql = command[-1]
        self.statements.append(sql)
        if "ROW_ACCESS_POLICIES" in sql:
            return json.dumps(self.found)
        if "INFORMATION_SCHEMA.COLUMNS" in sql:
            return json.dumps(self.source if ".intermediate." in sql else self.copy)
        raise AssertionError("a write went through the login's `bq`, not the publisher")

    def write(self, sql: str) -> None:
        self.statements.append(sql)
        self.writes.append(sql)


def reported(found: Sequence[Policy]) -> list[dict[str, str]]:
    # BigQuery reports a filter as it normalises it; spacing and keyword case differ.
    return [
        {"row_access_policy_name": p.name, "filter_predicate": p.condition.replace(" = ", "=")}
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
    publish(access, columns, PROJECT, WHERE, warehouse.write, warehouse)
    *checks, write = warehouse.statements
    assert all(s.startswith("SELECT ") for s in checks)
    assert warehouse.writes == [write]
    assert write == (
        f"BEGIN TRANSACTION; DELETE FROM `{PROJECT}.payments.payments` WHERE TRUE;"  # noqa: S608
        f" INSERT INTO `{PROJECT}.payments.payments` SELECT * FROM"
        f" `{PROJECT}.intermediate.payments`; COMMIT TRANSACTION;"
    )


def drop(found: list[dict[str, str]], name: str) -> list[dict[str, str]]:
    return [r for r in found if r["row_access_policy_name"] != name]


EDITS: dict[str, Callable[[list[dict[str, str]]], list[dict[str, str]]]] = {
    "no policy": lambda found: [],
    "one missing": lambda found: drop(found, "chicago_rows"),
    "the publisher's missing": lambda found: drop(found, PUBLISHER_POLICY),
    "one not generated": lambda found: [
        *found,
        {"row_access_policy_name": "everyone", "filter_predicate": "TRUE"},
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
        publish(access, columns, PROJECT, WHERE, warehouse.write, warehouse)
    assert not warehouse.writes


def test_a_copy_whose_columns_differ_from_the_mart_gets_no_rows(
    access: dict[str, Any], columns: dict[str, Any]
) -> None:
    warehouse = Warehouse(reported(policies(access, columns)), COLUMNS, COLUMNS[:1])
    with pytest.raises(RowPolicyError, match="columns differ"):
        publish(access, columns, PROJECT, WHERE, warehouse.write, warehouse)
    assert not warehouse.writes


def test_the_check_compares_names_and_filters_only() -> None:
    policy = Policy("payments", "payments", "chicago_rows", "entity_id = 'chicago'", ("auditor",))
    check_policies(
        [policy],
        [{"row_access_policy_name": "chicago_rows", "filter_predicate": "entity_id='chicago'"}],
        "payments",
        "payments",
    )


@pytest.mark.parametrize("named", [AGENT, account(AGENT)], ids=["by id", "by address"])
def test_a_publish_writes_as_the_publisher_through_the_agent(
    named: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    minted: list[list[str]] = []
    sent: list[httpx.Request] = []

    def mint(accounts: Sequence[str]) -> str:
        minted.append(list(accounts))
        return "publisher-token"

    def handle(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"jobComplete": True})

    real = httpx.Client
    monkeypatch.setattr(row_policies, "mint", mint)
    monkeypatch.setattr(
        row_policies.httpx,
        "Client",
        lambda **kw: real(transport=httpx.MockTransport(handle), **kw),
    )
    write = row_policies.publisher_write(PROJECT, {"LEDGER_LAB_AGENT_ACCOUNT": named})
    write("SELECT 1")
    assert minted == [[account(AGENT), account(PUBLISHER)]]
    (request,) = sent
    assert request.headers["Authorization"] == "Bearer publisher-token"
    body = json.loads(request.content)
    assert body["query"] == "SELECT 1"
    assert body["maximumBytesBilled"] == str(row_policies.MAX_BYTES_BILLED)


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
def test_each_human_persona_reads_only_its_entities_rows_of_payments(
    as_persona_on_bigquery: Callable[[str], PersonaSession], access: dict[str, Any], human: str
) -> None:
    from gcp_project import lab_project

    granted = identity(access, human).reads.get("payments")
    in_clear = human in clear(load_columns(), "payments")
    sql = f"SELECT DISTINCT entity_id FROM `{lab_project()}.payments.payments`"  # noqa: S608
    session = as_persona_on_bigquery(human)
    if granted is None:
        with pytest.raises(PersonaQueryError) as refused:
            session.query(sql)
        if refused.value.status != 403:
            pytest.fail("a persona with no row grant on payments was not refused")
        return
    seen = {row[0] for row in session.query(sql).rows}
    if seen != (set(granted) if in_clear else set()):
        pytest.fail("the persona read other entities' rows than its grants give")
