"""Each persona is an identity a test acts as, enforced by what its connection can reach.

The local engine has no users or roles, so a persona's connection opens a database named after it
that holds only its purposes' tables of the warehouse tables it may read row by row
(`<purpose>.<table>`, `access/purposes.yaml`), with the entity rows of them, and the aggregate
tables of the products it reads in aggregate (`access/compile.py`, whose own tests prove the rows,
the masking and the minimum group, in `tests/access/test_compile.py`, and the purposes in
`tests/access/test_purposes.py`); the connection is read-only and cannot reach any file, so nothing
else is there to read. The persona's reach is read
through qualified names, which a temporary object cannot shadow. `persona` and `agent` are
parametrised in `tests/conftest.py` from the schema-checked catalogue. Nothing here calls GCP: the
BigQuery mode is only the service account it would impersonate.
"""

import copy
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import duckdb
import pytest
from gcp_project import lab_project_or_none

from access.compile import open_persona, schema_of
from access.identities import ReservedPersonaError, UnknownPersonaError, identity
from tests.conftest import load_purposes

AsPersona = Callable[[str], duckdb.DuckDBPyConnection]
Doc = dict[str, Any]
# What `access/personas.yaml` says each access lets a persona see row by row, stated here apart
# from the code under test: `read` rows in clear and `edit` the definitions it changes; `aggregate`
# allows only aggregates above the minimum group, so it reaches the product's aggregate tables and
# no rows; `draft` and `post` go through the approval workflow and read nothing.
ROW_ACCESS = {"read": True, "edit": True, "aggregate": False, "draft": False, "post": False}
SERVICE_ACCOUNT = re.compile(
    r"(?P<persona>[a-z][a-z0-9-]{4,28}[a-z0-9])"
    r"@(?P<project>[a-z][a-z0-9-]{4,28}[a-z0-9])\.iam\.gserviceaccount\.com"
)


def expected(access: Doc, persona: str) -> dict[str, set[str | None]]:
    """The products the persona sees rows of, each with the entities whose rows it sees."""
    seen: dict[str, set[str | None]] = {}
    for grant in access["grants"]:
        if grant["persona"] == persona and ROW_ACCESS[grant["access"]]:
            seen.setdefault(grant["product"], set()).add(grant.get("entity"))
    return seen


def tables_of(access: Doc) -> dict[str, list[str]]:
    return {p["id"]: p.get("tables", []) for p in access["data_products"]}


def expected_rows(access: Doc, persona: str) -> dict[str, set[str | None]]:
    """The tables the persona reads rows in, `main.` for none, each `<purpose>.<table>` of its
    purposes in `access/purposes.yaml` with the entities it sees of the table's product."""
    seen = expected(access, persona)
    found: dict[str, set[str | None]] = {}
    for purpose in load_purposes()["purposes"]:
        if persona not in purpose["personas"]:
            continue
        for product, entities in seen.items():
            for table in tables_of(access)[product]:
                if table in purpose["tables"]:
                    found[f"{schema_of(purpose['id'])}.{table}"] = entities
    return found


def expected_aggregates(access: Doc, aggregates: Doc, persona: str) -> set[str]:
    """The aggregate tables of the products the persona reads in aggregate, in its main schema."""
    return {
        f"main.{view['name']}"
        for grant in access["grants"]
        if grant["persona"] == persona and grant["access"] == "aggregate"
        if tables_of(access)[grant["product"]]
        for view in aggregates["products"][grant["product"]]["tables"]
    }


def expected_tables(access: Doc, aggregates: Doc, persona: str) -> set[str]:
    return set(expected_rows(access, persona)) | expected_aggregates(access, aggregates, persona)


def warehouse_tables(access: Doc) -> set[str]:
    """The product tables under the names a persona's main schema would give them."""
    return {f"main.{table}" for tables in tables_of(access).values() for table in tables}


def tables(db: duckdb.DuckDBPyConnection, persona: str) -> set[str]:
    """Every table of the persona's database, `<schema>.<table>`."""
    rows = db.execute(
        "select schema_name, table_name from system.main.duckdb_tables() where database_name = ?",
        [persona],
    ).fetchall()
    return {f"{schema}.{name}" for schema, name in rows}


def qualified(persona: str, table: str) -> str:
    """`"<persona>"."<schema>"."<table>"`, a name no temporary object shadows."""
    schema, _, name = table.partition(".")
    return f'"{persona}"."{schema}"."{name}"'


def entities_in(db: duckdb.DuckDBPyConnection, persona: str, table: str) -> set[str | None]:
    sql = f"select distinct entity_id from {qualified(persona, table)}"  # noqa: S608
    return {entity for (entity,) in db.execute(sql).fetchall()}


def present(warehouse: Path, table: str) -> set[str | None]:
    """The entities the warehouse holds rows of in the table, read as the builder."""
    with duckdb.connect(str(warehouse), read_only=True) as db:
        return entities_in(db, "ledger_lab", f"main.{table.partition('.')[2]}")


def test_each_persona_gets_its_own_identity(
    as_persona: AsPersona, access: Doc, aggregates: Doc, warehouse: Path, persona: str
) -> None:
    db = as_persona(persona)
    assert db.execute("select system.main.current_database()").fetchone() == (persona,)
    assert tables(db, persona) == expected_tables(access, aggregates, persona)
    for table, entities in expected_rows(access, persona).items():
        if entities != {None}:  # a group product: the table itself is the whole grant
            assert entities_in(db, persona, table) == entities & present(warehouse, table), table


def test_aggregate_draft_and_post_alone_reach_no_rows(
    as_persona: AsPersona, access: Doc, aggregates: Doc, persona: str
) -> None:
    rowless = {
        g["product"]
        for g in access["grants"]
        if g["persona"] == persona and g["access"] in {"aggregate", "draft", "post"}
    } - set(expected(access, persona))
    held = tables(as_persona(persona), persona)
    for product in rowless:
        views = {
            f"main.{v['name']}" for v in aggregates["products"].get(product, {}).get("tables", [])
        }
        rows = {t for t in held if t.partition(".")[2] in tables_of(access)[product]}
        # The persona holds no table of the product but its aggregate tables, when it has them.
        assert rows <= views, product


def test_a_persona_cannot_read_a_table_it_has_no_grant_for(
    as_persona: AsPersona, access: Doc, aggregates: Doc, warehouse: Path, persona: str
) -> None:
    db = as_persona(persona)
    for table in warehouse_tables(access) - expected_tables(access, aggregates, persona):
        with pytest.raises(duckdb.CatalogException):
            db.execute(f'select * from "{table.partition(".")[2]}"')  # noqa: S608
    for reach in (
        f"attach '{warehouse}' as warehouse (read_only)",
        f"select * from '{warehouse}'",  # noqa: S608
        "set enable_external_access = true",
        "load httpfs",
    ):
        with pytest.raises((duckdb.PermissionException, duckdb.InvalidInputException)):
            db.execute(reach)


def test_a_persona_cannot_write_to_its_database(
    as_persona: AsPersona, access: Doc, aggregates: Doc, persona: str
) -> None:
    db = as_persona(persona)
    for table in warehouse_tables(access) | expected_tables(access, aggregates, persona):
        named = qualified(persona, table)
        for write in (
            f"create or replace table {named} as select 1 as forged",
            f"insert into {named} select * from {named}",  # noqa: S608
        ):
            with pytest.raises((duckdb.InvalidInputException, duckdb.CatalogException)):
                db.execute(write)
    assert tables(db, persona) == expected_tables(access, aggregates, persona)


def test_a_temporary_shadow_forges_only_unqualified_names_in_its_own_connection(
    as_persona: AsPersona, access: Doc, aggregates: Doc, warehouse: Path, persona: str
) -> None:
    db = as_persona(persona)
    held = expected_tables(access, aggregates, persona)
    db.execute("create or replace temp macro current_database() as 'forged'")
    assert db.execute("select current_database()").fetchone() == ("forged",)
    assert db.execute("select system.main.current_database()").fetchone() == (persona,)
    for table in (t.partition(".")[2] for t in warehouse_tables(access)):
        db.execute(f"create or replace temp table \"{table}\" as select 'forged' as entity_id")
        assert db.execute(f'select entity_id from "{table}"').fetchall() == [("forged",)]  # noqa: S608
    for table, entities in expected_rows(access, persona).items():
        if entities != {None}:
            assert entities_in(db, persona, table) == entities & present(warehouse, table), table
    for table in warehouse_tables(access) - held:
        with pytest.raises(duckdb.CatalogException):
            db.execute(f"select * from {qualified(persona, table)}")  # noqa: S608
    assert tables(db, persona) == held
    assert tables(as_persona(persona), persona) == held


@pytest.mark.parametrize("reserved", ["system", "warehouse"])
def test_a_persona_id_duckdb_reserves_as_a_catalog_name_is_refused_at_build(
    access: Doc, columns: Doc, aggregates: Doc, warehouse: Path, tmp_path: Path, reserved: str
) -> None:
    catalogue = copy.deepcopy(access)
    old = access["personas"][0]["id"]
    catalogue["personas"][0]["id"] = reserved
    for grant in catalogue["grants"]:
        if grant["persona"] == old:
            grant["persona"] = reserved
    purposes, columns = load_purposes(), copy.deepcopy(columns)
    for purpose in purposes["purposes"]:
        purpose["personas"] = [reserved if p == old else p for p in purpose["personas"]]
    for tag in columns["tags"]:
        for kind in ("clear", "masked"):
            tag[kind] = [reserved if p == old else p for p in tag[kind]]
    with pytest.raises(ReservedPersonaError, match=reserved):
        open_persona(catalogue, columns, aggregates, purposes, reserved, warehouse, tmp_path)


def test_the_fixture_refuses_an_unknown_persona(as_persona: AsPersona) -> None:
    with pytest.raises(UnknownPersonaError, match="nobody"):
        as_persona("nobody")


@pytest.mark.parametrize("scope", ["entity", "group"])
def test_a_grant_whose_entity_does_not_match_its_product_scope_is_refused(
    access: Doc, scope: str
) -> None:
    catalogue = copy.deepcopy(access)
    scoped = {p["id"] for p in catalogue["data_products"] if p["scope"] == scope}
    grant = next(g for g in catalogue["grants"] if g["product"] in scoped)
    if scope == "entity":
        del grant["entity"]
    else:
        grant["entity"] = catalogue["entities"][0]["id"]
    with pytest.raises(ValueError, match="entity-scoped"):
        identity(catalogue, grant["persona"])


def test_the_proposer_holds_no_data_role(as_persona: AsPersona, access: Doc, agent: str) -> None:
    assert identity(access, agent).reads == {}
    assert tables(as_persona(agent), agent) == set()


def test_the_bigquery_mode_impersonates_the_persona_account_in_the_lab_project(
    access: Doc, persona: str
) -> None:
    project = lab_project_or_none()
    if project is None:
        pytest.skip("LEDGER_LAB_GCP_PROJECT is unset: the lab has no BigQuery to act in")
    account = SERVICE_ACCOUNT.fullmatch(identity(access, persona).service_account(project))
    assert account is not None, "not a service account address GCP can create"
    assert (account["persona"], account["project"]) == (persona, project)
