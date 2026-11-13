"""Each persona is an identity a test acts as, enforced by what its connection can reach.

The local engine has no users or roles, so a persona's connection opens a database that holds only
the data products, and the entity rows of them, its grants let it read; the connection is
read-only and cannot reach any file, so nothing else is there to read. Nothing here calls GCP: the
BigQuery mode is only the service account it would impersonate.
"""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml
from gcp_project import lab_project_or_none

from access.identities import READS, UnknownPersonaError, identity

CATALOGUE = yaml.safe_load(
    (Path(__file__).resolve().parents[2] / "access/personas.yaml").read_text()
)
PERSONAS = [p["id"] for p in CATALOGUE["personas"]]
PRODUCTS = [p["id"] for p in CATALOGUE["data_products"]]
# The acting agent's identity, the proposer, is the persona of kind agent.
AGENTS = [p["id"] for p in CATALOGUE["personas"] if p["kind"] == "agent"]

AsPersona = Callable[[str], duckdb.DuckDBPyConnection]


def granted(persona: str) -> dict[str, set[str | None]]:
    """The products the persona's grants let it read, each with the entities whose rows it sees."""
    seen: dict[str, set[str | None]] = {}
    for grant in CATALOGUE["grants"]:
        if grant["persona"] == persona and grant["access"] in READS:
            seen.setdefault(grant["product"], set()).add(grant.get("entity"))
    return seen


def tables(db: duckdb.DuckDBPyConnection) -> set[str]:
    return {name for (name,) in db.execute("select table_name from duckdb_tables()").fetchall()}


@pytest.mark.parametrize("persona", PERSONAS)
def test_each_persona_gets_its_own_identity(as_persona: AsPersona, persona: str) -> None:
    db = as_persona(persona)
    assert db.execute("select persona()").fetchone() == (persona,)
    assert tables(db) == set(granted(persona))
    for product, entities in granted(persona).items():
        if entities == {None}:
            continue  # a group product: the table itself is the whole grant
        rows = db.execute(f'select distinct entity from "{product}"').fetchall()  # noqa: S608
        assert {entity for (entity,) in rows} == entities, product


@pytest.mark.parametrize("persona", PERSONAS)
def test_a_persona_cannot_read_a_product_it_has_no_grant_for(
    as_persona: AsPersona, warehouse: Path, persona: str
) -> None:
    db = as_persona(persona)
    for product in set(PRODUCTS) - set(granted(persona)):
        with pytest.raises(duckdb.CatalogException):
            db.execute(f'select * from "{product}"')  # noqa: S608
    for reach in (
        f"attach '{warehouse}' as warehouse (read_only)",
        f"select * from '{warehouse}'",  # noqa: S608
        "set enable_external_access = true",
        "load httpfs",
    ):
        with pytest.raises((duckdb.PermissionException, duckdb.InvalidInputException)):
            db.execute(reach)


@pytest.mark.parametrize("persona", PERSONAS)
def test_a_persona_cannot_change_what_its_connection_holds(
    as_persona: AsPersona, persona: str
) -> None:
    db = as_persona(persona)
    for product in PRODUCTS:
        with pytest.raises(duckdb.InvalidInputException, match="read-only"):
            db.execute(f'create or replace table "{product}" as select 1 as forged')


def test_the_fixture_refuses_an_unknown_persona(as_persona: AsPersona) -> None:
    with pytest.raises(UnknownPersonaError, match="nobody"):
        as_persona("nobody")


@pytest.mark.parametrize("persona", AGENTS)
def test_the_proposer_holds_no_data_role(
    as_persona: AsPersona, access: dict[str, Any], persona: str
) -> None:
    assert identity(access, persona).reads == {}
    assert tables(as_persona(persona)) == set()


def test_the_bigquery_mode_impersonates_the_persona_service_account(
    access: dict[str, Any],
) -> None:
    persona = PERSONAS[0]
    account = identity(access, persona).service_account("the-lab")
    assert account == f"{persona}@the-lab.iam.gserviceaccount.com"


def test_the_bigquery_mode_runs_only_with_the_lab_project(access: dict[str, Any]) -> None:
    project = lab_project_or_none()
    if project is None:
        pytest.skip("LEDGER_LAB_GCP_PROJECT is unset: the lab has no BigQuery to act in")
    for persona in PERSONAS:
        account = identity(access, persona).service_account(project)
        assert account == f"{persona}@{project}.iam.gserviceaccount.com"
