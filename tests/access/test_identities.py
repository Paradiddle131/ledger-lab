"""Each persona is an identity a test acts as, enforced by what its connection can reach.

The local engine has no users or roles, so a persona's connection opens a database named after it
that holds only the warehouse tables, and the entity rows of them, it may read row by row, and the
aggregate tables of the products it reads in aggregate (`access/compile.py`, whose own tests prove
the rows, the masking and the minimum group, in `tests/access/test_compile.py`); the connection is
read-only and cannot reach any file, so nothing else is there to read. The persona's reach is read
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

from access.compile import open_persona
from access.identities import ReservedPersonaError, UnknownPersonaError, identity

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
    """The warehouse tables the persona reads rows of, each with the entities it sees."""
    return {
        table: entities
        for product, entities in expected(access, persona).items()
        for table in tables_of(access)[product]
    }


def expected_aggregates(access: Doc, aggregates: Doc, persona: str) -> set[str]:
    """The aggregate tables of the products the persona reads in aggregate."""
    return {
        view["name"]
        for grant in access["grants"]
        if grant["persona"] == persona and grant["access"] == "aggregate"
        if tables_of(access)[grant["product"]]
        for view in aggregates["products"][grant["product"]]["tables"]
    }


def expected_tables(access: Doc, aggregates: Doc, persona: str) -> set[str]:
    return set(expected_rows(access, persona)) | expected_aggregates(access, aggregates, persona)


def warehouse_tables(access: Doc) -> set[str]:
    return {table for tables in tables_of(access).values() for table in tables}


def tables(db: duckdb.DuckDBPyConnection, persona: str) -> set[str]:
    rows = db.execute(
        "select table_name from system.main.duckdb_tables() where database_name = ?", [persona]
    ).fetchall()
    return {name for (name,) in rows}


def entities_in(db: duckdb.DuckDBPyConnection, persona: str, table: str) -> set[str | None]:
    sql = f'select distinct entity_id from "{persona}".main."{table}"'  # noqa: S608
    return {entity for (entity,) in db.execute(sql).fetchall()}


def present(warehouse: Path, table: str) -> set[str | None]:
    """The entities the warehouse holds rows of in the table, read as the builder."""
    with duckdb.connect(str(warehouse), read_only=True) as db:
        return entities_in(db, "ledger_lab", table)


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
        views = {v["name"] for v in aggregates["products"].get(product, {}).get("tables", [])}
        # The persona holds no table of the product but its aggregate tables, when it has them.
        assert held & ({*tables_of(access)[product]} | views) <= views, product


def test_a_persona_cannot_read_a_table_it_has_no_grant_for(
    as_persona: AsPersona, access: Doc, aggregates: Doc, warehouse: Path, persona: str
) -> None:
    db = as_persona(persona)
    for table in warehouse_tables(access) - expected_tables(access, aggregates, persona):
        with pytest.raises(duckdb.CatalogException):
            db.execute(f'select * from "{table}"')  # noqa: S608
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
        for write in (
            f'create or replace table "{persona}".main."{table}" as select 1 as forged',
            f'insert into "{persona}".main."{table}" select * from "{persona}".main."{table}"',  # noqa: S608
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
    for table in warehouse_tables(access):
        db.execute(f"create or replace temp table \"{table}\" as select 'forged' as entity_id")
        assert db.execute(f'select entity_id from "{table}"').fetchall() == [("forged",)]  # noqa: S608
    for table, entities in expected_rows(access, persona).items():
        if entities != {None}:
            assert entities_in(db, persona, table) == entities & present(warehouse, table), table
    for table in warehouse_tables(access) - held:
        with pytest.raises(duckdb.CatalogException):
            db.execute(f'select * from "{persona}".main."{table}"')  # noqa: S608
    assert tables(db, persona) == held
    assert tables(as_persona(persona), persona) == held


@pytest.mark.parametrize("reserved", ["system", "warehouse"])
def test_a_persona_id_duckdb_reserves_as_a_catalog_name_is_refused_at_build(
    access: Doc, columns: Doc, aggregates: Doc, warehouse: Path, tmp_path: Path, reserved: str
) -> None:
    catalogue = copy.deepcopy(access)
    catalogue["personas"][0]["id"] = reserved
    for grant in catalogue["grants"]:
        if grant["persona"] == access["personas"][0]["id"]:
            grant["persona"] = reserved
    with pytest.raises(ReservedPersonaError, match=reserved):
        open_persona(catalogue, columns, aggregates, reserved, warehouse, tmp_path)


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
